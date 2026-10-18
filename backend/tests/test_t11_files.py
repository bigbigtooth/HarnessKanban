"""T11 acceptance: controlled uploads, private storage, download authorisation, attachments.

The cases assert real database rows, real files on disk and real authorisation outcomes:
server-generated storage keys, real-size limits, append-only versions, an archived document
that stays downloadable, unbound staging material after a failed bind, and 404 for another
worker reading an execution artifact.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.audit.models import ActivityEvent
from apps.files.models import AttachmentLink, FileAsset
from apps.jobs.models import OutboxEvent
from apps.projects.models import Document, DocumentVersion, ProjectContextVersion
from apps.tasks.models import Task, TaskExecution
from domain import file_service
from domain.clock import db_now, override_db_clock
from domain.contracts import AttachmentPurpose, EndReason, ProjectStatus, TaskStatus
from domain.errors import (
    AttachmentLimitExceededError,
    ExecutionNotCurrentError,
    ExecutionTimeoutError,
    InvalidArgumentError,
    ResourceNotFoundError,
    RoleForbiddenError,
)
from domain.execution import finish_execution_locked, reset_in_progress_task_locked
from domain.transactions import run_write_service
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.files]

BASE = "/api/web/v1"


# --------------------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def web_client(db):
    return Client(SERVER_NAME="127.0.0.1")


@pytest.fixture
def storage(tmp_path):
    root = tmp_path / "private-files"
    root.mkdir()
    with override_settings(FILE_STORAGE_ROOT=str(root)):
        yield root


@pytest.fixture
def small_limits(settings):
    settings.HARNESSTASK = {
        **settings.HARNESSTASK,
        "max_upload_bytes": 512,
        "max_project_documents": 2,
        "max_task_reference_attachments": 2,
        "max_submission_artifacts": 1,
    }


def text_upload(name: str = "说明.md", content: bytes = b"# hello\n"):
    return SimpleUploadedFile(name, content, content_type="text/markdown")


def post_document(
    client,
    project,
    *,
    filename: str = "说明.md",
    content: bytes = b"# hello\n",
    upload=None,
    key=None,
    data=None,
    query="",
):
    """Every call streams a fresh file object (an uploaded file is consumed once)."""
    payload = {"file": upload if upload is not None else text_upload(filename, content)}
    payload.update(data or {})
    return client.post(
        f"{BASE}/projects/{project.pk}/documents{query}",
        payload,
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def post_version(client, document, *, filename: str = "v2.md", content: bytes = b"# v2\n", upload=None, key=None):
    return client.post(
        f"{BASE}/documents/{document.pk}/versions",
        {"file": upload if upload is not None else text_upload(filename, content)},
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def delete_document(client, document, *, key=None):
    """A bodyless DELETE is rejected by the request-boundary middleware, so send JSON."""
    return client.delete(
        f"{BASE}/documents/{document.pk}",
        data=json.dumps({}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def download_body(response) -> bytes:
    return b"".join(response.streaming_content)

def make_in_progress_task(project, agent, *, difficulty: int = 2, timeout_seconds: int | None = None):
    """Create a real in-progress task with a current execution and one reserved slot."""
    agent.reserved_execution_count = agent.reserved_execution_count + 1
    agent.save(update_fields=["reserved_execution_count"])
    task = factories.create_task(project, title="产物任务", difficulty=difficulty)
    now = db_now()
    execution = factories.create_execution(
        task, agent, claimed_at=now, timeout_seconds=timeout_seconds
    )
    task.status = TaskStatus.IN_PROGRESS.value
    task.assignee_agent = agent
    task.active_execution = execution
    task.claimed_at = now
    task.version = task.version + 1
    task.save(
        update_fields=["status", "assignee_agent", "active_execution", "claimed_at", "version"]
    )
    return task, execution


def reclaim_execution(task, execution):
    """Real reclaim through the T04 primitives (not a mock)."""

    def service():
        locked_task = Task.objects.select_for_update().get(pk=task.pk)
        locked_execution = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        finish_execution_locked(
            locked_execution, ended_at=db_now(), end_reason=EndReason.RECLAIM.value
        )
        reset_in_progress_task_locked(
            locked_task, expected_execution_id=execution.pk, now=db_now()
        )

    run_write_service(service)


def files_under(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


# --------------------------------------------------------------------------------------
# A. Document upload, storage and validation
# --------------------------------------------------------------------------------------


def test_upload_document_creates_version_one_asset_and_context_version(world, web_client, storage):
    project = world.project_a
    content = b"# \xe8\x83\x8c\xe6\x99\xaf\n" * 10
    response = post_document(
        web_client, project, upload=text_upload("项目说明.md", content), data={"name": "项目说明"}
    )

    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["name"] == "项目说明"
    assert payload["current_version"]["number"] == 1
    assert payload["current_version"]["media_type"] == "text/markdown"
    assert payload["current_version"]["size"] == len(content)

    document = Document.objects.get(pk=payload["id"])
    version = DocumentVersion.objects.get(pk=payload["current_version"]["id"])
    asset = FileAsset.objects.get(pk=version.file_asset_id)
    assert document.current_version_id == version.pk
    assert version.number == 1
    assert asset.bound_at is not None
    assert asset.sha256 == __import__("hashlib").sha256(content).hexdigest()
    assert asset.uploader_type == "web_operator"
    assert asset.project_id == project.pk

    blob = storage / "objects" / asset.storage_key
    assert blob.is_file()
    assert blob.read_bytes() == content

    project.refresh_from_db()
    assert project.content_version == 2
    assert project.version == 2
    context = ProjectContextVersion.objects.get(project=project, number=2)
    assert context.document_version_refs == [
        {
            "document_id": str(document.pk),
            "document_version_id": str(version.pk),
            "number": 1,
            "name": "项目说明",
        }
    ]
    assert [event.action for event in ActivityEvent.objects.all()] == ["document.create"]
    assert [event.event_type for event in OutboxEvent.objects.all()] == ["document.created"]


def test_storage_path_is_server_generated_and_never_uses_the_uploaded_name(world, web_client, storage):
    response = post_document(
        web_client,
        world.project_a,
        upload=SimpleUploadedFile("root-etc-secret.md", b"# keep\n", content_type="text/markdown"),
    )

    assert response.status_code == 201
    asset = FileAsset.objects.get()
    assert asset.original_name == "root-etc-secret.md"
    assert "secret" not in asset.storage_key
    assert "root" not in asset.storage_key
    assert "/" not in asset.storage_key and ".." not in asset.storage_key
    assert asset.storage_key.endswith(".md")
    # Every written file lives under the configured storage root only.
    written = files_under(storage)
    assert written == [f"objects/{asset.storage_key}"]
    assert list(storage.rglob("root-etc-secret.md")) == []


def test_service_rejects_path_separators_and_dotdot_names(world, web_client, storage):
    """Defence in depth: a crafted display name can never participate in a disk path."""
    from dataclasses import dataclass

    @dataclass
    class RawUpload:
        name: str
        payload: bytes = b"# x\n"

        def chunks(self, chunk_size: int = 1024):
            yield self.payload

    for raw_name in ("../../../etc/passwd.md", "..\\..\\windows.md", "..", ".", "bad\x00name.md"):
        with pytest.raises(InvalidArgumentError):
            file_service.stage_upload(RawUpload(name=raw_name))

    assert FileAsset.objects.count() == 0
    assert Document.objects.count() == 0
    assert files_under(storage) == []


def test_traversal_filename_from_the_wire_cannot_escape_the_storage_root(world, web_client, storage):
    """The multipart wire name is hostile input: the stored key stays server-generated."""
    boundary = "----traversal"
    raw_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="../../../../etc/passwd.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
        "# payload\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    response = web_client.post(
        f"{BASE}/projects/{world.project_a.pk}/documents",
        data=raw_body,
        content_type=f"multipart/form-data; boundary={boundary}",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )

    assert response.status_code == 201, response.content
    asset = FileAsset.objects.get()
    assert "/" not in asset.original_name and "\\" not in asset.original_name
    assert asset.original_name == "passwd.md"
    assert "passwd" not in asset.storage_key
    import re as _re

    assert _re.match(r"^[0-9a-f]{32}\.md$", asset.storage_key)
    assert files_under(storage) == [f"objects/{asset.storage_key}"]
    assert list(storage.parent.rglob("passwd.md")) == []
    assert not (storage.parent / "etc").exists()


@pytest.mark.parametrize(
    ("name", "content", "content_type"),
    [
        ("payload.exe", b"MZ\x90\x00fake executable", "application/octet-stream"),
        ("page.html", b"<html><body>hi</body></html>", "text/html"),
        ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"),
        ("fake.pdf", b"<html><script>alert(1)</script></html>", "application/pdf"),
        ("notes.md", b"# title\n<script>alert(1)</script>\n", "text/markdown"),
        ("script.txt", b"#!/bin/sh\nrm -rf /\n", "text/plain"),
        ("binary.txt", b"text\x00with\x00nul\x00bytes", "text/plain"),
        ("broken.txt", b"\xff\xfe\x00invalid utf8", "text/plain"),
    ],
)
def test_upload_rejects_wrong_type_or_markup_content(world, web_client, storage, name, content, content_type):
    response = post_document(
        web_client, world.project_a, upload=SimpleUploadedFile(name, content, content_type=content_type)
    )

    assert response.status_code == 415, (name, response.content)
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert FileAsset.objects.count() == 0
    assert files_under(storage) == []


def test_upload_over_the_real_limit_leaves_no_rows_and_no_files(world, web_client, storage, small_limits):
    oversized = b"# " + b"x" * 5000
    response = post_document(
        web_client, world.project_a, upload=text_upload("big.md", oversized)
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert response.json()["error"]["details"]["max_upload_bytes"] == 512
    assert FileAsset.objects.count() == 0
    assert AttachmentLink.objects.count() == 0
    assert files_under(storage) == []


def test_empty_upload_is_rejected(world, web_client, storage):
    response = post_document(web_client, world.project_a, upload=text_upload("empty.md", b""))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert FileAsset.objects.count() == 0
    assert files_under(storage) == []


def test_upload_requires_idempotency_key(world, web_client, storage):
    response = web_client.post(
        f"{BASE}/projects/{world.project_a.pk}/documents", {"file": text_upload()}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert FileAsset.objects.count() == 0


def test_replaying_an_upload_returns_the_original_document_without_a_second_version(
    world, web_client, storage
):
    project = world.project_a
    key = uuid.uuid4().hex
    upload_kwargs = {"filename": "重放.md", "content": b"# same\n", "key": key}

    first = post_document(web_client, project, **upload_kwargs)
    assert first.status_code == 201
    second = post_document(web_client, project, **upload_kwargs)

    assert second.status_code == 201
    assert second.json()["data"]["idempotent_replay"] is True
    assert second.json()["data"]["current_version"]["id"] == first.json()["data"]["current_version"]["id"]
    assert Document.objects.filter(project=project).count() == 1
    assert DocumentVersion.objects.count() == 1
    project.refresh_from_db()
    assert project.content_version == 2
    assert ActivityEvent.objects.count() == 1
    # The duplicate upload stays unbound staging material (never a fake attachment).
    assert AttachmentLink.objects.count() == 0
    assert FileAsset.objects.filter(bound_at__isnull=True).count() == 1
    assert len(FileAsset.objects.filter(bound_at__isnull=False)) == 1


def test_allowed_binary_types_are_accepted(world, web_client, storage):
    png = SimpleUploadedFile(
        "shot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, content_type="image/png"
    )
    pdf = SimpleUploadedFile("doc.pdf", b"%PDF-1.4\n%%EOF\n", content_type="application/pdf")
    archive = SimpleUploadedFile("bundle.zip", b"PK\x03\x04" + b"\x00" * 16, content_type="application/zip")

    for upload in (png, pdf, archive):
        response = post_document(web_client, world.project_a, upload=upload)
        assert response.status_code == 201, (upload.name, response.content)

    assert sorted(DocumentVersion.objects.values_list("number", flat=True)) == [1, 1, 1]
    assert Document.objects.count() == 3


def test_same_name_requires_explicit_new_document_or_new_version(world, web_client, storage):
    project = world.project_a
    first = post_document(web_client, project, upload=text_upload("说明.md", b"# v1\n"))
    assert first.status_code == 201
    document_id = first.json()["data"]["id"]

    conflict = post_document(web_client, project, upload=text_upload("说明.md", b"# v1b\n"))

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "DOCUMENT_CONFLICT"
    assert Document.objects.filter(project=project).count() == 1

    override = post_document(
        web_client,
        project,
        upload=text_upload("说明.md", b"# duplicate\n"),
        data={"allow_duplicate_name": "true"},
    )
    assert override.status_code == 201
    assert Document.objects.filter(project=project).count() == 2

    version = post_version(web_client, Document.objects.get(pk=document_id))
    assert version.status_code == 201
    assert version.json()["data"]["current_version"]["number"] == 2
    assert Document.objects.filter(project=project).count() == 2


def test_project_document_limit_returns_409_and_creates_nothing(world, web_client, storage, small_limits):
    project = world.project_a
    assert post_document(web_client, project, upload=text_upload("a.md", b"# a\n")).status_code == 201
    assert post_document(web_client, project, upload=text_upload("b.md", b"# b\n")).status_code == 201

    response = post_document(web_client, project, upload=text_upload("c.md", b"# c\n"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ATTACHMENT_LIMIT_EXCEEDED"
    assert response.json()["error"]["details"]["limit"] == 2
    assert Document.objects.filter(project=project).count() == 2
    assert FileAsset.objects.count() == 2

    # A new version of an existing document is not affected by the document count limit.
    document = Document.objects.filter(project=project).order_by("created_at").first()
    assert post_version(web_client, document).status_code == 201


def test_archived_project_rejects_document_uploads(world, web_client, storage):
    project = world.archived_project
    response = post_document(web_client, project, upload=text_upload())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert Document.objects.filter(project=project).count() == 0
    assert FileAsset.objects.count() == 0
    assert files_under(storage) == []


# --------------------------------------------------------------------------------------
# B. Versions, listing and removal
# --------------------------------------------------------------------------------------


def test_add_version_appends_and_keeps_previous_version_readable(world, web_client, storage):
    project = world.project_a
    first = post_document(web_client, project, upload=text_upload("handbook.md", b"# v1\n"))
    document = Document.objects.get(pk=first.json()["data"]["id"])
    first_version = DocumentVersion.objects.get(pk=first.json()["data"]["current_version"]["id"])
    first_blob = storage / "objects" / first_version.file_asset.storage_key

    second = post_version(web_client, document, upload=text_upload("handbook.md", b"# v2\n"))

    assert second.status_code == 201
    assert second.json()["data"]["current_version"]["number"] == 2
    document.refresh_from_db()
    assert document.current_version.number == 2
    assert DocumentVersion.objects.filter(document=document).count() == 2
    first_version.refresh_from_db()
    assert first_version.number == 1
    assert first_blob.read_bytes() == b"# v1\n"

    project.refresh_from_db()
    assert project.content_version == 3
    context = ProjectContextVersion.objects.get(project=project, number=3)
    assert [ref["number"] for ref in context.document_version_refs] == [2]
    older = ProjectContextVersion.objects.get(project=project, number=2)
    assert [ref["document_version_id"] for ref in older.document_version_refs] == [
        str(first_version.pk)
    ]

    detail = web_client.get(f"{BASE}/documents/{document.pk}")
    assert detail.status_code == 200
    assert [version["number"] for version in detail.json()["data"]["versions"]] == [2, 1]


def test_document_list_only_shows_current_documents(world, web_client, storage):
    project = world.project_a
    post_document(web_client, project, upload=text_upload("one.md", b"# one\n"))
    second = post_document(web_client, project, upload=text_upload("two.md", b"# two\n"))
    document = Document.objects.get(pk=second.json()["data"]["id"])

    listed = web_client.get(f"{BASE}/projects/{project.pk}/documents")
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 2

    removed = delete_document(web_client, document)
    assert removed.status_code == 200, removed.content
    assert removed.json()["data"]["is_archived"] is True

    after = web_client.get(f"{BASE}/projects/{project.pk}/documents")
    assert after.json()["data"]["total"] == 1

    project.refresh_from_db()
    assert project.content_version == 4
    latest = ProjectContextVersion.objects.get(project=project, number=4)
    assert [ref["name"] for ref in latest.document_version_refs] == ["one.md"]


def test_removing_a_document_keeps_rows_files_and_downloads(world, web_client, storage):
    project = world.project_a
    created = post_document(web_client, project, upload=text_upload("keep.md", b"# keep me\n"))
    document = Document.objects.get(pk=created.json()["data"]["id"])
    version = DocumentVersion.objects.get(pk=created.json()["data"]["current_version"]["id"])
    asset = version.file_asset
    blob = storage / "objects" / asset.storage_key

    removed = delete_document(web_client, document)
    assert removed.status_code == 200

    # Rows and blobs survive: older context versions and submissions keep reading them.
    assert Document.objects.filter(pk=document.pk).exists()
    assert DocumentVersion.objects.filter(pk=version.pk).exists()
    assert FileAsset.objects.filter(pk=asset.pk).exists()
    assert blob.is_file()

    download = web_client.get(f"{BASE}/files/{asset.pk}/download")
    assert download.status_code == 200
    assert download_body(download) == b"# keep me\n"

    detail = web_client.get(f"{BASE}/documents/{document.pk}")
    assert detail.status_code == 200
    assert detail.json()["data"]["is_archived"] is True

    # Removing it twice with a new key is an idempotent no-op, never a new context version.
    contexts_before = ProjectContextVersion.objects.filter(project=project).count()
    again = delete_document(web_client, document)
    assert again.status_code == 200
    assert again.json()["data"]["already_archived"] is True
    assert ProjectContextVersion.objects.filter(project=project).count() == contexts_before
    assert ActivityEvent.objects.filter(action="document.archive").count() == 1


def test_removing_an_outsider_document_is_not_found(world, web_client, storage):
    project = factories.create_project("隔离项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, world.u)
    created = post_document(web_client, project, upload=text_upload("secret.md", b"# s\n"))
    document = Document.objects.get(pk=created.json()["data"]["id"])

    actor = _agent_actor(world.w1)
    with pytest.raises(ResourceNotFoundError):
        file_service.get_document_detail(actor, document.pk)
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(actor, document.current_version.file_asset_id)
    with pytest.raises(ResourceNotFoundError):
        file_service.archive_document(actor, document.pk, idem_key=uuid.uuid4().hex)


def _agent_actor(agent):
    from domain.contracts import ActorContext, ActorType

    return ActorContext(
        actor_type=ActorType.AGENT,
        request_id=str(uuid.uuid4()),
        agent_id=agent.pk,
        key_version=agent.key_version,
    )


# --------------------------------------------------------------------------------------
# C. Download headers and authorisation matrix
# --------------------------------------------------------------------------------------


def test_download_headers_are_attachment_and_never_leak_the_storage_path(world, web_client, storage):
    project = world.project_a
    created = post_document(web_client, project, upload=text_upload("报告 v1.md", b"# body\n"))
    asset_id = created.json()["data"]["current_version"]["file_id"]

    response = web_client.get(f"{BASE}/files/{asset_id}/download")

    assert response.status_code == 200
    assert download_body(response) == b"# body\n"
    disposition = response["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=utf-8''" in disposition.lower()
    assert response["Content-Type"] == "text/markdown"
    asset = FileAsset.objects.get(pk=asset_id)
    body = download_body(response).decode("utf-8")
    for leaked in (asset.storage_key, str(storage), "objects/"):
        assert leaked not in body
        assert leaked not in disposition


def test_download_authorisation_matrix_by_purpose(world, web_client, storage):
    project = world.project_a  # members: S1, S2, W1, W2
    supervisor = _agent_actor(world.s1)
    other_worker = _agent_actor(world.w2)
    outsider = _agent_actor(world.u)

    # PROJECT_DOCUMENT: every member may read, an outsider may not.
    created = post_document(web_client, project, upload=text_upload("shared.md", b"# shared\n"))
    document_file_id = created.json()["data"]["current_version"]["file_id"]
    assert file_service.authorise_download(supervisor, document_file_id).media_type == "text/markdown"
    assert file_service.authorise_download(other_worker, document_file_id).media_type == "text/markdown"
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(outsider, document_file_id)

    # Revocation takes effect immediately.
    from apps.projects.models import ProjectMember

    ProjectMember.objects.filter(project=project, agent=world.w2).delete()
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(other_worker, document_file_id)

    # TASK_REFERENCE on a PREPARING task: supervisors only, workers get 404.
    preparing = factories.create_task(project, title="准备中参考", status=TaskStatus.PREPARING)
    reference = file_service.attach_to_task(
        _agent_actor(world.s1),
        preparing.pk,
        upload=text_upload("reference.md", b"# ref\n"),
        purpose=AttachmentPurpose.TASK_REFERENCE.value,
        idem_key=uuid.uuid4().hex,
    )
    reference_file_id = reference["file_id"]
    assert file_service.authorise_download(supervisor, reference_file_id).size == 6
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(_agent_actor(world.w2), reference_file_id)
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(outsider, reference_file_id)

    # EXECUTION_ARTIFACT: the owning execution's agent, a supervisor, the Web operator —
    # another worker in the same project gets 404.
    task, execution = make_in_progress_task(project, world.w1)
    artifact = file_service.attach_to_task(
        _agent_actor(world.w1),
        task.pk,
        upload=text_upload("artifact.md", b"# artifact\n"),
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        execution_id=execution.pk,
        idem_key=uuid.uuid4().hex,
    )
    artifact_file_id = artifact["file_id"]
    assert file_service.authorise_download(_agent_actor(world.w1), artifact_file_id).size > 0
    assert file_service.authorise_download(supervisor, artifact_file_id).size > 0
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(other_worker, artifact_file_id)
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(outsider, artifact_file_id)


def test_missing_blob_is_not_downloadable(world, web_client, storage):
    created = post_document(web_client, world.project_a, upload=text_upload("gone.md", b"# gone\n"))
    asset = FileAsset.objects.get(pk=created.json()["data"]["current_version"]["file_id"])
    (storage / "objects" / asset.storage_key).unlink()

    response = web_client.get(f"{BASE}/files/{asset.pk}/download")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert asset.storage_key not in response.content.decode("utf-8")


# --------------------------------------------------------------------------------------
# D. Task attachments and execution artifacts
# --------------------------------------------------------------------------------------


def test_attachment_endpoint_validates_purpose_and_execution_id(world, web_client, storage):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1)

    def post(payload):
        return web_client.post(
            f"{BASE}/tasks/{task.pk}/attachments",
            {"file": text_upload("x.md", b"# x\n"), **payload},
            HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
        )

    missing_purpose = post({})
    missing_execution = post({"purpose": "EXECUTION_ARTIFACT"})
    privileged_purpose = post({"purpose": "SUBMISSION_ARTIFACT", "execution_id": str(execution.pk)})
    reference_with_execution = post(
        {"purpose": "TASK_REFERENCE", "execution_id": str(execution.pk)}
    )
    unknown_purpose = post({"purpose": "SOMETHING_ELSE"})

    for response in (
        missing_purpose,
        missing_execution,
        privileged_purpose,
        reference_with_execution,
        unknown_purpose,
    ):
        assert response.status_code == 400, response.content
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    assert AttachmentLink.objects.count() == 0
    assert FileAsset.objects.count() == 0
    assert files_under(storage) == []


def test_staging_upload_is_readable_only_by_its_uploader(world, web_client, storage):
    project = world.project_a
    uploader = _agent_actor(world.w1)
    orphan = file_service.persist_staging_asset(
        uploader,
        file_service.stage_upload(text_upload("staged.md", b"# staged\n")),
        project=project,
    )

    assert file_service.authorise_download(uploader, orphan.pk).size == 9
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(_agent_actor(world.w2), orphan.pk)
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(_agent_actor(world.s1), orphan.pk)


def test_worker_cannot_bind_a_task_reference(world, web_client, storage):
    project = world.project_a
    task = factories.create_task(project, title="参考任务", status=TaskStatus.PREPARING)

    with pytest.raises(RoleForbiddenError):
        file_service.attach_to_task(
            _agent_actor(world.w2),
            task.pk,
            upload=text_upload("nope.md", b"# nope\n"),
            purpose=AttachmentPurpose.TASK_REFERENCE.value,
            idem_key=uuid.uuid4().hex,
        )
    assert AttachmentLink.objects.filter(task=task).count() == 0
    # Rejected before any bytes are stored: nothing staged at all.
    assert FileAsset.objects.count() == 0
    assert files_under(storage) == []


def test_task_reference_limit_returns_409_and_stores_nothing(world, web_client, storage, small_limits):
    project = world.project_a
    task = factories.create_task(project, title="参考上限", status=TaskStatus.TODO)
    actor = _agent_actor(world.s1)

    for index in range(2):
        file_service.attach_to_task(
            actor,
            task.pk,
            upload=text_upload(f"ref{index}.md", f"# ref{index}\n".encode()),
            purpose=AttachmentPurpose.TASK_REFERENCE.value,
            idem_key=uuid.uuid4().hex,
        )

    with pytest.raises(AttachmentLimitExceededError):
        file_service.attach_to_task(
            actor,
            task.pk,
            upload=text_upload("ref-over.md", b"# over\n"),
            purpose=AttachmentPurpose.TASK_REFERENCE.value,
            idem_key=uuid.uuid4().hex,
        )

    assert AttachmentLink.objects.filter(task=task).count() == 2
    assert FileAsset.objects.count() == 2


def test_execution_artifact_requires_the_current_execution_owner(world, web_client, storage):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1)

    # Another worker cannot address this execution at all (404, no enumeration).
    with pytest.raises(ResourceNotFoundError):
        file_service.attach_to_task(
            _agent_actor(world.w2),
            task.pk,
            upload=text_upload("steal.md", b"# steal\n"),
            purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
            execution_id=execution.pk,
            idem_key=uuid.uuid4().hex,
        )

    # A stale execution id is not current.
    stale_task, stale_execution = make_in_progress_task(project, world.s2)
    reclaim_execution(stale_task, stale_execution)
    with pytest.raises(ExecutionNotCurrentError):
        file_service.attach_to_task(
            _agent_actor(world.s2),
            stale_task.pk,
            upload=text_upload("stale.md", b"# stale\n"),
            purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
            execution_id=stale_execution.pk,
            idem_key=uuid.uuid4().hex,
        )

    assert AttachmentLink.objects.count() == 0
    assert FileAsset.objects.count() == 0


@pytest.mark.timeout
def test_upload_past_the_deadline_never_starts_and_creates_no_file(world, web_client, storage):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1, difficulty=1, timeout_seconds=900)

    with override_db_clock(execution.execution_deadline_at):
        response = web_client.post(
            f"{BASE}/tasks/{task.pk}/attachments",
            {
                "file": text_upload("late.md", b"# late\n"),
                "purpose": AttachmentPurpose.EXECUTION_ARTIFACT.value,
                "execution_id": str(execution.pk),
            },
            HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXECUTION_TIMEOUT"
    assert FileAsset.objects.count() == 0
    assert AttachmentLink.objects.count() == 0
    assert files_under(storage) == []


@pytest.mark.fault
@pytest.mark.timeout
def test_reclaimed_execution_cannot_bind_a_staged_upload(world, web_client, storage):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1)
    actor = _agent_actor(world.w1)
    payload = {
        "purpose": AttachmentPurpose.EXECUTION_ARTIFACT.value,
        "task_id": str(task.pk),
        "execution_id": str(execution.pk),
    }

    # Real two-phase upload: stage + persist, then the execution is reclaimed.
    staged = file_service.stage_upload(text_upload("artifact.md", b"# artifact\n"))
    asset = file_service.persist_staging_asset(actor, staged, project=project)
    assert asset.bound_at is None
    reclaim_execution(task, execution)

    with pytest.raises(ExecutionNotCurrentError):
        file_service.bind_staged_asset(actor, payload, asset, uuid.uuid4().hex)

    asset.refresh_from_db()
    assert asset.bound_at is None
    assert AttachmentLink.objects.filter(file_asset=asset).count() == 0
    assert (storage / "objects" / asset.storage_key).is_file()
    assert file_service.is_staging_asset(asset) is True
    found = file_service.find_unreferenced_staging_assets(older_than=db_now() + timedelta(minutes=1))
    assert [candidate.pk for candidate in found] == [asset.pk]


@pytest.mark.timeout
def test_deadline_passing_mid_upload_blocks_the_bind_only(world, web_client, storage):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1, timeout_seconds=900)
    actor = _agent_actor(world.w1)
    payload = {
        "purpose": AttachmentPurpose.EXECUTION_ARTIFACT.value,
        "task_id": str(task.pk),
        "execution_id": str(execution.pk),
    }

    with override_db_clock(execution.execution_deadline_at - timedelta(seconds=5)):
        staged = file_service.stage_upload(text_upload("mid.md", b"# mid\n"))
        asset = file_service.persist_staging_asset(actor, staged, project=project)

    with override_db_clock(execution.execution_deadline_at):
        with pytest.raises(ExecutionTimeoutError):
            file_service.bind_staged_asset(actor, payload, asset, uuid.uuid4().hex)

    asset.refresh_from_db()
    assert asset.bound_at is None
    assert AttachmentLink.objects.count() == 0
    assert ActivityEvent.objects.count() == 0


@pytest.mark.fault
def test_rebinding_an_already_bound_file_is_rejected(world, web_client, storage):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1)
    actor = _agent_actor(world.w1)
    payload = {
        "purpose": AttachmentPurpose.EXECUTION_ARTIFACT.value,
        "task_id": str(task.pk),
        "execution_id": str(execution.pk),
    }
    staged = file_service.stage_upload(text_upload("once.md", b"# once\n"))
    asset = file_service.persist_staging_asset(actor, staged, project=project)
    file_service.bind_staged_asset(actor, payload, asset, uuid.uuid4().hex)
    assert AttachmentLink.objects.count() == 1

    from domain.errors import VersionConflictError

    with pytest.raises(VersionConflictError):
        file_service.bind_staged_asset(actor, payload, asset, uuid.uuid4().hex)
    assert AttachmentLink.objects.count() == 1


def test_web_operator_can_bind_and_list_attachments_and_workers_do_not_see_artifacts(world, web_client, storage):
    project = world.project_a
    task = factories.create_task(project, title="附件列表", status=TaskStatus.TODO)
    task, execution = make_in_progress_task(project, world.w1)

    web_upload = web_client.post(
        f"{BASE}/tasks/{task.pk}/attachments",
        {
            "file": text_upload("artifact.md", b"# artifact\n"),
            "purpose": AttachmentPurpose.EXECUTION_ARTIFACT.value,
            "execution_id": str(execution.pk),
        },
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert web_upload.status_code == 201, web_upload.content

    listed = web_client.get(f"{BASE}/tasks/{task.pk}/attachments")
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1

    # A supervisor sees every link; another worker only sees its own execution artifacts.
    supervisor_view = file_service.list_task_attachments(_agent_actor(world.s1), task.pk)
    assert supervisor_view["total"] == 1
    other_worker_view = file_service.list_task_attachments(
        _agent_actor(world.w2), task.pk, page=1, page_size=20
    )
    assert other_worker_view["total"] == 0
    owner_view = file_service.list_task_attachments(_agent_actor(world.w1), task.pk)
    assert owner_view["total"] == 1


def test_submission_artifact_is_readable_by_task_members_and_outsiders_are_404(world, web_client, storage):
    project = world.project_a
    from apps.tasks.models import TaskSubmission

    task, execution = make_in_progress_task(project, world.w1)
    upload = file_service.attach_to_task(
        _agent_actor(world.w1),
        task.pk,
        upload=text_upload("result.md", b"# result\n"),
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        execution_id=execution.pk,
        idem_key=uuid.uuid4().hex,
    )
    asset = FileAsset.objects.get(pk=upload["file_id"])

    def bind():
        submission = TaskSubmission.objects.create(
            execution=execution,
            task=task,
            summary="结果",
            content="正文",
            requirements_version=task.requirements_version,
            submitted_by_agent=world.w1,
            submitted_at=db_now(),
        )
        file_service.bind_submission_artifact_locked(
            asset, submission=submission, execution=execution, now=db_now()
        )

    run_write_service(bind)

    # Once submitted, any project member who can read the task may read the artifact.
    assert file_service.authorise_download(_agent_actor(world.w2), asset.pk).size > 0
    assert file_service.authorise_download(_agent_actor(world.s2), asset.pk).size > 0
    with pytest.raises(ResourceNotFoundError):
        file_service.authorise_download(_agent_actor(world.u), asset.pk)

    assert set(
        AttachmentLink.objects.filter(file_asset=asset).values_list("purpose", flat=True)
    ) == {AttachmentPurpose.EXECUTION_ARTIFACT.value, AttachmentPurpose.SUBMISSION_ARTIFACT.value}


def test_submission_artifact_limit_is_enforced(world, web_client, storage, small_limits):
    project = world.project_a
    from apps.tasks.models import TaskSubmission

    task, execution = make_in_progress_task(project, world.w1)
    first = file_service.attach_to_task(
        _agent_actor(world.w1),
        task.pk,
        upload=text_upload("one.md", b"# one\n"),
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        execution_id=execution.pk,
        idem_key=uuid.uuid4().hex,
    )
    second = file_service.attach_to_task(
        _agent_actor(world.w1),
        task.pk,
        upload=text_upload("two.md", b"# two\n"),
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        execution_id=execution.pk,
        idem_key=uuid.uuid4().hex,
    )

    def bind():
        submission = TaskSubmission.objects.create(
            execution=execution,
            task=task,
            summary="结果",
            content="正文",
            requirements_version=task.requirements_version,
            submitted_by_agent=world.w1,
            submitted_at=db_now(),
        )
        file_service.bind_submission_artifact_locked(
            FileAsset.objects.get(pk=first["file_id"]),
            submission=submission,
            execution=execution,
            now=db_now(),
        )
        with pytest.raises(AttachmentLimitExceededError):
            file_service.bind_submission_artifact_locked(
                FileAsset.objects.get(pk=second["file_id"]),
                submission=submission,
                execution=execution,
                now=db_now(),
            )

    run_write_service(bind)
    assert (
        AttachmentLink.objects.filter(purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value).count()
        == 1
    )


@pytest.mark.fault
def test_cleanup_helper_deletes_only_unreferenced_staging_files(world, web_client, storage):
    project = world.project_a
    created = post_document(web_client, project, upload=text_upload("doc.md", b"# doc\n"))
    bound_asset = FileAsset.objects.get(pk=created.json()["data"]["current_version"]["file_id"])

    orphan = file_service.persist_staging_asset(
        _agent_actor(world.s1),
        file_service.stage_upload(text_upload("orphan.md", b"# orphan\n")),
        project=project,
    )
    assert file_service.is_staging_asset(orphan) is True
    assert file_service.is_staging_asset(bound_asset) is False

    candidates = file_service.find_unreferenced_staging_assets(
        older_than=db_now() + timedelta(minutes=1)
    )
    assert [candidate.pk for candidate in candidates] == [orphan.pk]

    run_write_service(lambda: file_service.delete_staging_asset(orphan))
    assert FileAsset.objects.filter(pk=orphan.pk).count() == 0
    assert not (storage / "objects" / orphan.storage_key).exists()
    # The bound document is never touched by cleanup.
    assert FileAsset.objects.filter(pk=bound_asset.pk).count() == 1
    assert (storage / "objects" / bound_asset.storage_key).is_file()


# --------------------------------------------------------------------------------------
# E. AgentAPI surface
# --------------------------------------------------------------------------------------


@pytest.fixture
def agent_client():
    from starlette.testclient import TestClient

    from agentapi.main import app

    with TestClient(app) as client:
        yield client


@pytest.mark.django_db(transaction=True)
def test_agent_api_multipart_upload_and_download_roundtrip(world, storage, agent_client):
    project = world.project_a
    raw_key = world.s1_key
    boundary = "----harnessmultipart"
    content = b"# agent reference\n"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n\r\n'
        "Agent 参考\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="agent-ref.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
        + content.decode()
        + f"\r\n--{boundary}--\r\n"
    ).encode()

    uploaded = agent_client.post(
        f"/api/agent/v1/projects/{project.pk}/documents",
        content=body,
        headers={
            "content-type": f"multipart/form-data; boundary={boundary}",
            "AGENT_TOKEN": raw_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )

    assert uploaded.status_code == 201, uploaded.text
    data = uploaded.json()["data"]
    assert data["name"] == "Agent 参考"
    assert data["current_version"]["size"] == len(content)

    listed = agent_client.get(
        f"/api/agent/v1/projects/{project.pk}/documents", headers={"AGENT_TOKEN": raw_key}
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1

    download = agent_client.get(
        f"/api/agent/v1/files/{data['current_version']['file_id']}/download",
        headers={"AGENT_TOKEN": raw_key},
    )
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-disposition"].startswith("attachment;")

    # No token, no file. No project membership, no file either.
    assert (
        agent_client.get(f"/api/agent/v1/files/{data['current_version']['file_id']}/download").status_code
        == 401
    )
    assert (
        agent_client.get(
            f"/api/agent/v1/files/{data['current_version']['file_id']}/download",
            headers={"AGENT_TOKEN": world.u_key},
        ).status_code
        == 404
    )


@pytest.mark.django_db(transaction=True)
def test_agent_api_raw_body_upload_with_query_metadata(world, storage, agent_client):
    project = world.project_a
    task = factories.create_task(project, title="Agent 参考任务", status=TaskStatus.PREPARING)
    content = b"# raw body reference\n"

    uploaded = agent_client.post(
        f"/api/agent/v1/tasks/{task.pk}/attachments",
        params={"purpose": "TASK_REFERENCE", "filename": "raw-body.md"},
        content=content,
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.s1_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )

    assert uploaded.status_code == 201, uploaded.text
    link = AttachmentLink.objects.get()
    assert link.purpose == AttachmentPurpose.TASK_REFERENCE.value
    assert FileAsset.objects.get(pk=link.file_asset_id).size == len(content)

    # A worker member cannot bind a reference on the same task.
    forbidden = agent_client.post(
        f"/api/agent/v1/tasks/{task.pk}/attachments",
        params={"purpose": "TASK_REFERENCE", "filename": "raw-body.md"},
        content=content,
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.w2_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ROLE_FORBIDDEN"
    assert AttachmentLink.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.timeout
def test_agent_api_rejects_execution_artifact_without_deadline(world, storage, agent_client):
    project = world.project_a
    task, execution = make_in_progress_task(project, world.w1)

    with override_db_clock(execution.execution_deadline_at):
        response = agent_client.post(
            f"/api/agent/v1/tasks/{task.pk}/attachments",
            params={
                "purpose": "EXECUTION_ARTIFACT",
                "execution_id": str(execution.pk),
                "filename": "late.md",
            },
            content=b"# late\n",
            headers={
                "content-type": "application/octet-stream",
                "AGENT_TOKEN": world.w1_key,
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXECUTION_TIMEOUT"
    assert FileAsset.objects.count() == 0
