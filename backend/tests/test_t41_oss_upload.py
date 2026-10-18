"""T41 acceptance: OSS-mirrored uploads, ``oss_url`` in views, staging upload endpoints.

The suite never touches the network: OSS is either disabled (the pytest default set in
``tests/__init__.py``) or faked by monkeypatching :mod:`domain.oss_storage` entry
points. The real-bucket flow is covered by the manual acceptance scripts against the
running services.
"""

from __future__ import annotations

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.files.models import AttachmentLink, FileAsset
from domain import file_service, oss_storage
from domain.errors import OssUnavailableError
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.files]

BASE = "/api/web/v1"
AGENT_BASE = "/api/agent/v1"

OSS_ON = {
    "enabled": True,
    "endpoint": "oss-cn-fake.aliyuncs.com",
    "bucket": "harvesstask-fake",
    "access_key_id": "fake-id",
    "access_key_secret": "fake-secret",
    "url_ttl_seconds": 3600,
}


class FakeOss:
    """Records calls; failures can be injected per method."""

    def __init__(self):
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.fail_upload = False
        self.fail_delete = False

    def put_object_from_file(self, key, filename, headers=None):
        if self.fail_upload:
            raise RuntimeError("oss down")
        self.uploaded.append(key)

    def delete_object(self, key):
        if self.fail_delete:
            raise RuntimeError("oss down")
        self.deleted.append(key)

    def sign_url(self, method, key, ttl, **kwargs):
        return f"https://signed.example.com/{key}?ttl={ttl}"


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
def project(db):
    return factories.create_project("OSS 验收项目")


@pytest.fixture
def fake_oss(monkeypatch):
    fake = FakeOss()
    monkeypatch.setattr(oss_storage, "_bucket_client", fake)
    return fake


def text_upload(name: str = "说明.md", content: bytes = b"# hello\n"):
    return SimpleUploadedFile(name, content, content_type="text/markdown")


def enable_oss():
    return override_settings(OSS=dict(OSS_ON))


# --------------------------------------------------------------------------------------
# oss_storage unit level
# --------------------------------------------------------------------------------------


def test_oss_storage_disabled_is_inert(storage):
    assert oss_storage.is_enabled() is False
    assert oss_storage.sign_get_url("a" * 32 + ".md") is None
    oss_storage.upload_local_file("a" * 32 + ".md", storage / "whatever.md")  # no-op, no raise
    oss_storage.delete_object("a" * 32 + ".md")


def test_oss_storage_failure_wrapped_as_unavailable(storage, fake_oss, tmp_path):
    fake_oss.fail_upload = True
    probe = tmp_path / "p.md"
    probe.write_text("x")
    with enable_oss():
        with pytest.raises(OssUnavailableError):
            oss_storage.upload_local_file("a" * 32 + ".md", probe)


# --------------------------------------------------------------------------------------
# persist path: OSS mirror, oss_url in views, failure leaves nothing behind
# --------------------------------------------------------------------------------------


def test_task_reference_upload_mirrors_to_oss_and_exposes_oss_url(
    db, storage, project, fake_oss, web_actor
):
    task = factories.create_task(project, title="附件任务")
    with enable_oss():
        data = file_service.attach_to_task(
            web_actor,
            task.pk,
            upload=file_service.InMemoryUpload(name="参考.md", content=b"# ref\n"),
            purpose="TASK_REFERENCE",
            idem_key=uuid.uuid4().hex,
        )
    asset = FileAsset.objects.get(pk=data["file_id"])
    assert asset.oss_uploaded_at is not None
    assert fake_oss.uploaded == [asset.storage_key]
    assert data["oss_url"] == f"https://signed.example.com/{asset.storage_key}?ttl=3600"
    assert AttachmentLink.objects.filter(
        task=task, file_asset=asset, purpose="TASK_REFERENCE"
    ).exists()


def test_oss_upload_failure_fails_whole_request(db, storage, project, fake_oss, web_actor):
    from domain.errors import OssUnavailableError as _Err

    fake_oss.fail_upload = True
    task = factories.create_task(project, title="失败任务")
    with enable_oss():
        with pytest.raises(_Err):
            file_service.attach_to_task(
                web_actor,
                task.pk,
                upload=file_service.InMemoryUpload(name="参考.md", content=b"# ref\n"),
                purpose="TASK_REFERENCE",
                idem_key=uuid.uuid4().hex,
            )
    assert FileAsset.objects.count() == 0
    objects = storage / "objects"
    assert not any(objects.iterdir()) if objects.exists() else True


def test_delete_staging_asset_also_removes_oss_object(db, storage, project, fake_oss, web_actor):
    task = factories.create_task(project, title="清理任务")
    with enable_oss():
        data = file_service.attach_to_task(
            web_actor,
            task.pk,
            upload=file_service.InMemoryUpload(name="待清.md", content=b"# tmp\n"),
            purpose="TASK_REFERENCE",
            idem_key=uuid.uuid4().hex,
        )
    asset = FileAsset.objects.get(pk=data["file_id"])

    from domain.transactions import run_write_service

    # 解绑后再走暂存清理（模拟 24h 回收路径）
    AttachmentLink.objects.filter(file_asset=asset).delete()
    FileAsset.objects.filter(pk=asset.pk).update(bound_at=None)
    asset.refresh_from_db()
    with enable_oss():
        run_write_service(lambda: file_service.delete_staging_asset(asset))
    assert fake_oss.deleted == [asset.storage_key]
    assert not FileAsset.objects.filter(pk=asset.pk).exists()


# --------------------------------------------------------------------------------------
# POST /api/web/v1/files (staging upload for task authoring)
# --------------------------------------------------------------------------------------


def post_staging_file(web_client, *, content=b"# staged\n", name="暂存.md", key=None, project_id=None):
    payload = {"file": text_upload(name, content)}
    if project_id is not None:
        payload["project_id"] = str(project_id)
    return web_client.post(
        f"{BASE}/files",
        payload,
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def test_web_staging_upload_returns_file_id_and_null_oss_url_when_disabled(web_client, storage, project):
    response = post_staging_file(web_client, project_id=project.pk)
    assert response.status_code == 201
    body = response.json()
    data = body["data"]
    assert data["file_id"] == data["id"]
    assert data["oss_url"] is None
    asset = FileAsset.objects.get(pk=data["file_id"])
    assert asset.bound_at is None
    assert asset.oss_uploaded_at is None
    assert str(asset.project_id) == str(project.pk)


def test_web_staging_upload_with_oss_returns_signed_url(web_client, storage, project, fake_oss):
    with enable_oss():
        response = post_staging_file(web_client, project_id=project.pk)
    assert response.status_code == 201
    data = response.json()["data"]
    asset = FileAsset.objects.get(pk=data["file_id"])
    assert asset.oss_uploaded_at is not None
    assert data["oss_url"].startswith("https://signed.example.com/")


def test_web_staging_upload_requires_idempotency_key(web_client, storage):
    response = web_client.post(f"{BASE}/files", {"file": text_upload()})
    assert response.status_code == 400


def test_web_staging_upload_replays_same_key_same_content(web_client, storage, project):
    key = uuid.uuid4().hex
    first = post_staging_file(web_client, key=key, project_id=project.pk)
    second = post_staging_file(web_client, key=key, project_id=project.pk)
    assert first.status_code == second.status_code == 201
    assert second.json()["data"]["file_id"] == first.json()["data"]["file_id"]
    assert second.json()["data"]["idempotent_replay"] is True
    assert FileAsset.objects.count() == 1


def test_web_staging_upload_rejects_same_key_different_content(web_client, storage, project):
    key = uuid.uuid4().hex
    post_staging_file(web_client, key=key, content=b"# one\n", project_id=project.pk)
    response = post_staging_file(web_client, key=key, content=b"# two\n", project_id=project.pk)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_web_staging_upload_rejects_unknown_project(web_client, storage):
    response = post_staging_file(web_client, project_id=uuid.uuid4())
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# POST /api/agent/v1/files (supervisors only)
# --------------------------------------------------------------------------------------

pytestmark_agentapi = pytest.mark.django_db(transaction=True)


@pytest.fixture
def agentapi_client():
    from fastapi.testclient import TestClient

    from agentapi.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def committed_world(db):
    factories.ensure_claim_lock()
    return factories.World()


@pytestmark_agentapi
def test_agent_staging_upload_allowed_for_supervisor(agentapi_client, committed_world, storage):
    world = committed_world
    response = agentapi_client.post(
        f"{AGENT_BASE}/files",
        params={"filename": "agent.md"},
        content=b"# from agent\n",
        headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["file_id"]
    assert data["oss_url"] is None  # OSS disabled in the suite
    assert FileAsset.objects.filter(pk=data["file_id"], bound_at__isnull=True).exists()


@pytestmark_agentapi
def test_agent_staging_upload_forbidden_for_worker(agentapi_client, committed_world, storage):
    world = committed_world
    response = agentapi_client.post(
        f"{AGENT_BASE}/files",
        params={"filename": "worker.md"},
        content=b"# nope\n",
        headers={"AGENT_TOKEN": world.w1_key, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"
