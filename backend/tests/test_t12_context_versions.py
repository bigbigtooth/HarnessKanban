"""T12 acceptance: project background edits and immutable context versions.

Asserts that every context change (background text or reference document list) increments
``content_version`` and appends a new ``ProjectContextVersion``, that older rows stay
readable with their original refs, that a later edit never rewrites an earlier snapshot,
and that ``current_context_for_claim`` returns the current version plus the refs frozen at
that moment.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, override_settings

from apps.audit.models import ActivityEvent
from apps.files.models import FileAsset
from apps.jobs.models import IdempotencyRecord
from apps.projects.models import Document, DocumentVersion, Project, ProjectContextVersion
from domain import context_service
from domain.contracts import ActorContext, ActorType, ProjectStatus
from domain.context_service import (
    REF_KEYS,
    build_context_snapshot,
    current_context_for_claim,
    current_document_version_refs,
    latest_context_version,
)
from domain.errors import (
    InvalidArgumentError,
    ResourceNotFoundError,
    RoleForbiddenError,
    VersionConflictError,
)
from domain.project_service import list_projects
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.projects, pytest.mark.files, pytest.mark.workflow]

WEB_BASE = "/api/web/v1"


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
def agent_client():
    from starlette.testclient import TestClient

    from agentapi.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def agent_actor_factory():
    def build(agent, key_version=None):
        return ActorContext(
            actor_type=ActorType.AGENT,
            request_id=str(uuid.uuid4()),
            agent_id=agent.pk,
            key_version=agent.key_version if key_version is None else key_version,
        )

    return build


def upload_markdown(name: str, content: bytes):
    return SimpleUploadedFile(name, content, content_type="text/markdown")


def post_document(client, project, *, filename: str, content: bytes, key=None, data=None, result=201):
    response = client.post(
        f"{WEB_BASE}/projects/{project.pk}/documents",
        {"file": upload_markdown(filename, content), **(data or {})},
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )
    assert response.status_code == result, response.content
    return response.json()["data"]


def context_numbers(project) -> list[int]:
    return list(
        ProjectContextVersion.objects.filter(project=project)
        .order_by("number")
        .values_list("number", flat=True)
    )


# --------------------------------------------------------------------------------------
# Background edits
# --------------------------------------------------------------------------------------


def test_background_update_appends_context_version_and_keeps_v1(world, agent_actor_factory):
    project = world.project_a  # background "A 背景", content_version 1
    actor = agent_actor_factory(world.s1)

    data = context_service.update_project_background(
        actor,
        project.pk,
        background="新背景 V2",
        expected_version=project.version,
        idem_key=uuid.uuid4().hex,
    )

    project.refresh_from_db()
    assert project.background == "新背景 V2"
    assert project.content_version == 2
    assert project.version == 2
    assert data["current_context_version"]["number"] == 2

    v1 = ProjectContextVersion.objects.get(project=project, number=1)
    v2 = ProjectContextVersion.objects.get(project=project, number=2)
    assert v1.background_snapshot == "A 背景"
    assert v2.background_snapshot == "新背景 V2"
    assert v1.document_version_refs == v2.document_version_refs == []
    # V1 stays readable and untouched after V2 exists.
    assert ProjectContextVersion.objects.filter(project=project, number=1).count() == 1
    assert [event.action for event in ActivityEvent.objects.all()] == ["project.update_background"]
    record = IdempotencyRecord.objects.get()
    assert record.state == "DONE" and record.resource_id == project.pk


def test_background_update_replay_does_not_create_a_second_version(world, agent_actor_factory):
    project = world.project_a
    actor = agent_actor_factory(world.s1)
    key = uuid.uuid4().hex
    expected_version = project.version

    first = context_service.update_project_background(
        actor, project.pk, background="同一次编辑", expected_version=expected_version, idem_key=key
    )
    replay = context_service.update_project_background(
        actor, project.pk, background="同一次编辑", expected_version=expected_version, idem_key=key
    )

    assert replay["idempotent_replay"] is True
    assert replay["current_context_version"]["id"] == first["current_context_version"]["id"]
    assert context_numbers(project) == [1, 2]
    assert Project.objects.get(pk=project.pk).content_version == 2
    assert ActivityEvent.objects.count() == 1


def test_background_update_rejects_stale_version_and_wrong_role(world, agent_actor_factory):
    project = world.project_a
    supervisor = agent_actor_factory(world.s1)
    worker = agent_actor_factory(world.w1)
    outside_supervisor, _key = factories.create_agent(
        factories.unique_name("外部主管"), __import__("domain.contracts", fromlist=["AgentRole"]).AgentRole.SUPERVISOR
    )

    with pytest.raises(VersionConflictError):
        context_service.update_project_background(
            supervisor,
            project.pk,
            background="x",
            expected_version=project.version + 4,
            idem_key=uuid.uuid4().hex,
        )
    # Workers never edit project context, even when they are project members.
    with pytest.raises(RoleForbiddenError):
        context_service.update_project_background(
            worker,
            project.pk,
            background="x",
            expected_version=project.version,
            idem_key=uuid.uuid4().hex,
        )
    # A supervisor without membership cannot see the project at all.
    with pytest.raises(ResourceNotFoundError):
        context_service.update_project_background(
            agent_actor_factory(outside_supervisor),
            project.pk,
            background="x",
            expected_version=project.version,
            idem_key=uuid.uuid4().hex,
        )

    project.refresh_from_db()
    assert project.background == "A 背景"
    assert project.content_version == 1
    assert context_numbers(project) == [1]
    assert ActivityEvent.objects.count() == 0


def test_background_update_rejects_archived_project(world, agent_actor_factory):
    project = world.archived_project
    actor = agent_actor_factory(world.s1)
    factories.add_member(project, world.s1)

    with pytest.raises(ResourceNotFoundError):
        context_service.update_project_background(
            actor,
            project.pk,
            background="不应写入",
            expected_version=project.version,
            idem_key=uuid.uuid4().hex,
        )
    project.refresh_from_db()
    assert project.background == ""
    assert project.content_version == 1


def test_background_length_limit_is_enforced(world, agent_actor_factory):
    project = world.project_a
    with pytest.raises(InvalidArgumentError):
        context_service.update_project_background(
            agent_actor_factory(world.s1),
            project.pk,
            background="x" * (context_service.MAX_BACKGROUND_LENGTH + 1),
            expected_version=project.version,
            idem_key=uuid.uuid4().hex,
        )


# --------------------------------------------------------------------------------------
# Document changes and snapshot immutability
# --------------------------------------------------------------------------------------


def test_document_changes_append_context_versions_and_freeze_previous_refs(
    world, web_client, storage
):
    project = world.project_a
    created = post_document(web_client, project, filename="设计.md", content=b"# v1\n")
    document = Document.objects.get(pk=created["id"])
    v1 = DocumentVersion.objects.get(pk=created["current_version"]["id"])

    assert context_numbers(project) == [1, 2]
    v2_context = ProjectContextVersion.objects.get(project=project, number=2)
    assert v2_context.background_snapshot == "A 背景"
    assert v2_context.document_version_refs == [
        {
            "document_id": str(document.pk),
            "document_version_id": str(v1.pk),
            "number": 1,
            "name": "设计.md",
        }
    ]

    version_response = web_client.post(
        f"{WEB_BASE}/documents/{document.pk}/versions",
        {"file": upload_markdown("设计.md", b"# v2\n")},
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert version_response.status_code == 201, version_response.content
    v2_version = DocumentVersion.objects.get(pk=version_response.json()["data"]["current_version"]["id"])

    assert context_numbers(project) == [1, 2, 3]
    v3_context = ProjectContextVersion.objects.get(project=project, number=3)
    assert [ref["document_version_id"] for ref in v3_context.document_version_refs] == [str(v2_version.pk)]

    # V2 is immutable evidence: its refs still point at version 1.
    v2_context.refresh_from_db()
    assert [ref["document_version_id"] for ref in v2_context.document_version_refs] == [str(v1.pk)]
    assert [ref["number"] for ref in v2_context.document_version_refs] == [1]


def test_removing_a_document_changes_the_list_without_breaking_old_snapshots(
    world, web_client, storage
):
    project = world.project_a
    created = post_document(web_client, project, filename="移除.md", content=b"# remove me\n")
    document = Document.objects.get(pk=created["id"])
    version = DocumentVersion.objects.get(pk=created["current_version"]["id"])
    frozen = ProjectContextVersion.objects.get(project=project, number=2)

    response = web_client.delete(
        f"{WEB_BASE}/documents/{document.pk}",
        data=json.dumps({}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert response.status_code == 200, response.content

    assert context_numbers(project) == [1, 2, 3]
    newest = ProjectContextVersion.objects.get(project=project, number=3)
    assert newest.document_version_refs == []
    # The older snapshot keeps referencing the moved-out document version...
    frozen.refresh_from_db()
    assert [ref["document_version_id"] for ref in frozen.document_version_refs] == [str(version.pk)]
    # ...and the file it references is still downloadable by project members.
    download = web_client.get(f"{WEB_BASE}/files/{version.file_asset_id}/download")
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == b"# remove me\n"


def test_context_version_numbers_are_monotonic_and_match_content_version(world, web_client, storage):
    project = world.project_a
    post_document(web_client, project, filename="a.md", content=b"# a\n")
    post_document(web_client, project, filename="b.md", content=b"# b\n")

    assert context_numbers(project) == [1, 2, 3]
    project.refresh_from_db()
    assert project.content_version == 3
    assert latest_context_version(project).number == project.content_version
    assert latest_context_version(project).document_version_refs != []


def test_restore_does_not_rewrite_context_versions(world, web_client, storage):
    project = world.project_a
    created = post_document(web_client, project, filename="恢复.md", content=b"# r\n")

    archived = web_client.post(
        f"{WEB_BASE}/projects/{project.pk}/archive",
        data=json.dumps({"expected_version": Project.objects.get(pk=project.pk).version, "reason": "冻结"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert archived.status_code == 200
    snapshots_before = list(
        ProjectContextVersion.objects.filter(project=project)
        .order_by("number")
        .values_list("number", "background_snapshot", "document_version_refs")
    )

    restored = web_client.post(
        f"{WEB_BASE}/projects/{project.pk}/restore",
        data=json.dumps({"expected_version": Project.objects.get(pk=project.pk).version}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert restored.status_code == 200

    snapshots_after = list(
        ProjectContextVersion.objects.filter(project=project)
        .order_by("number")
        .values_list("number", "background_snapshot", "document_version_refs")
    )
    assert snapshots_after == snapshots_before
    project.refresh_from_db()
    assert project.content_version == 2
    assert Document.objects.filter(pk=created["id"]).count() == 1


# --------------------------------------------------------------------------------------
# current_context_for_claim / build_context_snapshot
# --------------------------------------------------------------------------------------


def test_current_context_for_claim_returns_latest_version_with_frozen_refs(world, web_client, storage):
    project = world.project_a
    created = post_document(web_client, project, filename="快照.md", content=b"# snapshot\n")
    version_id = created["current_version"]["id"]

    context, refs = current_context_for_claim(project)

    assert isinstance(context, ProjectContextVersion)
    assert context.number == 2
    assert refs == context.document_version_refs
    assert refs[0]["document_version_id"] == version_id
    assert set(refs[0]) == set(REF_KEYS)


def test_current_context_for_claim_falls_back_to_live_refs_without_a_context_row(world, web_client, storage):
    project = world.project_a
    post_document(web_client, project, filename="无行.md", content=b"# x\n")
    ProjectContextVersion.objects.filter(project=project).delete()

    context, refs = current_context_for_claim(project)

    assert context is None
    assert [ref["name"] for ref in refs] == ["无行.md"]
    assert refs == current_document_version_refs(project)


def test_build_context_snapshot_works_without_and_with_an_existing_project_lock(world, web_client, storage):
    project = world.project_a
    post_document(web_client, project, filename="锁.md", content=b"# lock\n")

    def unlocked_read():
        return build_context_snapshot(project, using_locked_project=False)

    def locked_read():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        return build_context_snapshot(locked, using_locked_project=True)

    with transaction.atomic():
        context_unlocked, refs_unlocked = unlocked_read()
    assert context_unlocked.number == 2
    assert [ref["name"] for ref in refs_unlocked] == ["锁.md"]

    with transaction.atomic():
        context_locked, refs_locked = locked_read()
    assert context_locked.pk == context_unlocked.pk
    assert refs_locked == refs_unlocked


def test_ref_shape_is_exactly_the_frozen_fields(world, web_client, storage):
    project = world.project_a
    post_document(web_client, project, filename="字段.md", content=b"# fields\n")
    refs = current_document_version_refs(project)

    assert len(refs) == 1
    assert tuple(sorted(refs[0])) == tuple(sorted(REF_KEYS))
    assert isinstance(refs[0]["number"], int)


# --------------------------------------------------------------------------------------
# AgentAPI surface: /projects read, background PATCH, documents
# --------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_agent_project_list_and_detail_only_show_authorised_projects(world, agent_client):
    listed = agent_client.get("/api/agent/v1/projects", headers={"AGENT_TOKEN": world.w1_key})
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["data"]["items"]}
    assert str(world.project_a.pk) in ids
    assert str(world.project_b.pk) not in ids  # W1 is not a member of project B

    detail = agent_client.get(
        f"/api/agent/v1/projects/{world.project_a.pk}", headers={"AGENT_TOKEN": world.w1_key}
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["background"] == "A 背景"
    assert detail.json()["data"]["current_context_version_number"] == 1

    outsider = agent_client.get(
        f"/api/agent/v1/projects/{world.project_a.pk}", headers={"AGENT_TOKEN": world.u_key}
    )
    assert outsider.status_code == 404
    assert outsider.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.django_db(transaction=True)
def test_agent_patch_background_creates_context_version_and_never_touches_lifecycle(world, storage, agent_client):
    project = world.project_a
    before_members = set(
        __import__("apps.projects.models", fromlist=["ProjectMember"])
        .ProjectMember.objects.filter(project=project)
        .values_list("agent_id", flat=True)
    )

    response = agent_client.patch(
        f"/api/agent/v1/projects/{project.pk}",
        json={"background": "Agent 更新的背景", "expected_version": project.version},
        headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": uuid.uuid4().hex},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["background"] == "Agent 更新的背景"
    assert body["status"] == ProjectStatus.ACTIVE.value
    assert body["current_context_version"]["number"] == 2

    project.refresh_from_db()
    assert project.background == "Agent 更新的背景"
    assert project.status == ProjectStatus.ACTIVE.value
    assert project.archived_from_status is None
    assert ProjectContextVersion.objects.filter(project=project, number=2).exists()
    assert ProjectContextVersion.objects.get(project=project, number=1).background_snapshot == "A 背景"
    from apps.projects.models import ProjectMember

    assert set(ProjectMember.objects.filter(project=project).values_list("agent_id", flat=True)) == before_members

    # Lifecycle and member fields are not writable through this endpoint.
    for payload in (
        {"status": "ARCHIVED", "expected_version": project.version},
        {"members": [], "expected_version": project.version},
        {"title": "改名", "expected_version": project.version},
        {"agent_id": str(world.s2.pk), "expected_version": project.version},
        {"unexpected": 1, "expected_version": project.version},
    ):
        rejected = agent_client.patch(
            f"/api/agent/v1/projects/{project.pk}",
            json=payload,
            headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": uuid.uuid4().hex},
        )
        assert rejected.status_code == 400, (payload, rejected.text)
        assert rejected.json()["error"]["code"] in ("INVALID_ARGUMENT", "UNSUPPORTED_PARAMETER")

    project.refresh_from_db()
    assert project.status == ProjectStatus.ACTIVE.value
    assert project.content_version == 2
    assert context_numbers(project) == [1, 2]


@pytest.mark.django_db(transaction=True)
def test_agent_patch_background_requires_key_supervisor_and_current_version(world, storage, agent_client):
    project = world.project_a

    missing_key = agent_client.patch(
        f"/api/agent/v1/projects/{project.pk}",
        json={"background": "x", "expected_version": project.version},
        headers={"AGENT_TOKEN": world.s1_key},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    worker = agent_client.patch(
        f"/api/agent/v1/projects/{project.pk}",
        json={"background": "x", "expected_version": project.version},
        headers={"AGENT_TOKEN": world.w1_key, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert worker.status_code == 403
    assert worker.json()["error"]["code"] == "ROLE_FORBIDDEN"

    stale = agent_client.patch(
        f"/api/agent/v1/projects/{project.pk}",
        json={"background": "x", "expected_version": project.version + 9},
        headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    project.refresh_from_db()
    assert project.background == "A 背景"
    assert context_numbers(project) == [1]


@pytest.mark.django_db(transaction=True)
def test_agent_document_upload_requires_supervisor_and_membership(world, storage, agent_client):
    project = world.project_a
    content = b"# agent doc\n"

    worker = agent_client.post(
        f"/api/agent/v1/projects/{project.pk}/documents",
        params={"filename": "worker.md"},
        content=content,
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.w1_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )
    assert worker.status_code == 403
    assert Document.objects.count() == 0
    assert FileAsset.objects.count() == 0

    outsider = agent_client.post(
        f"/api/agent/v1/projects/{project.pk}/documents",
        params={"filename": "outsider.md"},
        content=content,
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.u_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )
    assert outsider.status_code == 404
    assert Document.objects.count() == 0

    supervisor = agent_client.post(
        f"/api/agent/v1/projects/{project.pk}/documents",
        params={"filename": "supervisor.md"},
        content=content,
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.s1_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )
    assert supervisor.status_code == 201, supervisor.text
    assert Document.objects.count() == 1
    assert context_numbers(project) == [1, 2]


@pytest.mark.django_db(transaction=True)
def test_agent_raw_body_upload_of_an_over_long_document_is_rejected(world, storage, agent_client, settings):
    settings.HARNESSTASK = {**settings.HARNESSTASK, "max_upload_bytes": 256}
    project = world.project_a

    response = agent_client.post(
        f"/api/agent/v1/projects/{project.pk}/documents",
        params={"filename": "big.md"},
        content=b"x" * 1024,
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.s1_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert FileAsset.objects.count() == 0
    assert Document.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_cross_project_document_and_file_access_is_not_found(world, web_client, storage, agent_client):
    project_a = world.project_a  # W1 is a member; U is not
    created = post_document(web_client, project_a, filename="cross.md", content=b"# cross\n")
    document = Document.objects.get(pk=created["id"])
    file_id = created["current_version"]["file_id"]

    listing = agent_client.get(
        f"/api/agent/v1/projects/{project_a.pk}/documents", headers={"AGENT_TOKEN": world.u_key}
    )
    assert listing.status_code == 404

    download = agent_client.get(
        f"/api/agent/v1/files/{file_id}/download", headers={"AGENT_TOKEN": world.u_key}
    )
    assert download.status_code == 404
    assert download.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    version = agent_client.post(
        f"/api/agent/v1/documents/{document.pk}/versions",
        params={"filename": "cross-v2.md"},
        content=b"# nope\n",
        headers={
            "content-type": "application/octet-stream",
            "AGENT_TOKEN": world.u_key,
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )
    assert version.status_code == 404
    assert DocumentVersion.objects.filter(document=document).count() == 1

    # A project B member sees project A documents as not found as well (no enumeration).
    other_project = world.project_b
    post_document(web_client, other_project, filename="b-only.md", content=b"# b\n")
    b_document = Document.objects.get(project=other_project)
    b_listing = agent_client.get(
        f"/api/agent/v1/projects/{other_project.pk}/documents", headers={"AGENT_TOKEN": world.w1_key}
    )
    assert b_listing.status_code == 404
    b_download = agent_client.get(
        f"/api/agent/v1/files/{b_document.current_version.file_asset_id}/download",
        headers={"AGENT_TOKEN": world.w1_key},
    )
    assert b_download.status_code == 404
    assert b_document.current_version.file_asset.sha256


def _actor_for(agent):
    return ActorContext(
        actor_type=ActorType.AGENT,
        request_id=str(uuid.uuid4()),
        agent_id=agent.pk,
        key_version=agent.key_version,
    )


def test_list_projects_service_scopes_to_membership(world):
    w1_projects = list_projects(_actor_for(world.w1), page=1, page_size=20)
    assert {item["id"] for item in w1_projects["items"]} == {str(world.project_a.pk)}

    u_projects = list_projects(_actor_for(world.u), page=1, page_size=20)
    assert {item["id"] for item in u_projects["items"]} == {str(world.project_b.pk)}

    archived_only = list_projects(
        _actor_for(world.u), status_filter=ProjectStatus.ACTIVE.value, page=1, page_size=20
    )
    assert archived_only["total"] == 1

    with pytest.raises(InvalidArgumentError):
        list_projects(_actor_for(world.u), status_filter="NOT_A_STATUS")
