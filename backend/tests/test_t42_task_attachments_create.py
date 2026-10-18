"""T42 acceptance: task creation with ``attachment_file_ids`` and OSS URLs in the prompt.

The prompt handed to agents is ``requirements_snapshot.description`` frozen at claim
time; when a task is created with staged uploads, their signed OSS links must already
be part of that text. OSS is faked (monkeypatched signer) — the real-bucket flow runs
in the manual acceptance scripts.
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client, override_settings

from apps.files.models import AttachmentLink, FileAsset
from apps.audit.models import ActivityEvent
from apps.jobs.models import OutboxEvent
from domain import file_service, task_service
from domain.contracts import ActorContext, ActorType, AgentRole
from domain.errors import AttachmentLimitExceededError, InvalidArgumentError, ResourceNotFoundError
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.workflow]

BASE = "/api/web/v1"

OSS_ON = {
    "enabled": True,
    "endpoint": "oss-cn-fake.aliyuncs.com",
    "bucket": "harvesstask-fake",
    "access_key_id": "fake-id",
    "access_key_secret": "fake-secret",
    "url_ttl_seconds": 3600,
}


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
def world(db):
    factories.ensure_claim_lock()
    return factories.World()


@pytest.fixture
def web_actor():
    return ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))


def stage_file(web_actor, name: str = "参考.md", content: bytes = b"# ref\n") -> FileAsset:
    """A real unbound staging asset (streamed + validated through the service)."""
    data = file_service.upload_staging_file(
        web_actor,
        upload=file_service.InMemoryUpload(name=name, content=content),
        idem_key=uuid.uuid4().hex,
    )
    return FileAsset.objects.get(pk=data["file_id"])


def create_payload(project, **overrides):
    payload = {
        "project_id": str(project.pk),
        "title": "带附件的任务",
        "description": "请阅读附件后完成工作。",
        "acceptance_criteria": "产出 deliverable.md",
        "difficulty": 2,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# Service level: binding + prompt composition
# --------------------------------------------------------------------------------------


def test_create_task_binds_attachments_and_writes_urls_into_prompt(db, storage, web_actor, world, monkeypatch):
    project = factories.create_project("提示词项目", status="ACTIVE")
    factories.add_member(project, world.s1)
    asset_a = stage_file(web_actor, "规格.md", b"# spec\n")
    asset_b = stage_file(web_actor, "数据.txt", b"data")
    monkeypatch.setattr(
        task_service.oss_storage,
        "asset_url",
        lambda asset: f"https://signed.example.com/{asset.storage_key}",
    )

    data = task_service.create_task(
        web_actor,
        create_payload(project, attachment_file_ids=[str(asset_a.pk), str(asset_b.pk)]),
        uuid.uuid4().hex,
    )

    task = data
    assert "## 参考附件（OSS 下载链接，可直接下载）" in task["description"]
    assert f"- 规格.md: https://signed.example.com/{asset_a.storage_key}" in task["description"]
    assert f"- 数据.txt: https://signed.example.com/{asset_b.storage_key}" in task["description"]
    assert task["description"].startswith("请阅读附件后完成工作。")

    links = AttachmentLink.objects.filter(task_id=task["id"], purpose="TASK_REFERENCE")
    assert set(links.values_list("file_asset_id", flat=True)) == {asset_a.pk, asset_b.pk}
    asset_a.refresh_from_db()
    assert asset_a.bound_at is not None
    assert asset_a.project_id == project.pk
    assert ActivityEvent.objects.filter(
        task_id=task["id"], action="attachment.upload"
    ).count() == 2
    assert OutboxEvent.objects.filter(event_type="attachment.bound").count() == 2


def test_create_task_without_oss_urls_still_binds_attachments(db, storage, web_actor, world):
    project = factories.create_project("离线项目", status="ACTIVE")
    factories.add_member(project, world.s1)
    asset = stage_file(web_actor, "离线.md")

    data = task_service.create_task(
        web_actor, create_payload(project, attachment_file_ids=[str(asset.pk)]), uuid.uuid4().hex
    )
    assert "## 参考附件" not in data["description"]
    assert AttachmentLink.objects.filter(task_id=data["id"], file_asset=asset).exists()


def test_create_task_attachment_validation(db, storage, web_actor, world):
    project = factories.create_project("校验项目", status="ACTIVE")
    factories.add_member(project, world.s1)
    asset = stage_file(web_actor, "已绑定.md")

    with pytest.raises(InvalidArgumentError):
        task_service.create_task(
            web_actor, create_payload(project, attachment_file_ids="not-a-list"), uuid.uuid4().hex
        )
    with pytest.raises(InvalidArgumentError):
        task_service.create_task(
            web_actor, create_payload(project, attachment_file_ids=[str(asset.pk), str(asset.pk)]),
            uuid.uuid4().hex,
        )
    with pytest.raises(InvalidArgumentError):
        task_service.create_task(
            web_actor, create_payload(project, attachment_file_ids=["not-a-uuid"]), uuid.uuid4().hex
        )
    with pytest.raises(ResourceNotFoundError):
        task_service.create_task(
            web_actor, create_payload(project, attachment_file_ids=[str(uuid.uuid4())]),
            uuid.uuid4().hex,
        )

    # 先绑定一次，再引用同一文件必须被拒绝
    task_service.create_task(
        web_actor, create_payload(project, attachment_file_ids=[str(asset.pk)]), uuid.uuid4().hex
    )
    with pytest.raises(ResourceNotFoundError):
        task_service.create_task(
            web_actor, create_payload(project, attachment_file_ids=[str(asset.pk)]), uuid.uuid4().hex
        )


def test_create_task_attachment_limit(db, storage, web_actor, world, settings):
    project = factories.create_project("上限项目", status="ACTIVE")
    factories.add_member(project, world.s1)
    settings.HARNESSTASK = {**settings.HARNESSTASK, "max_task_reference_attachments": 2}
    ids = [str(stage_file(web_actor, f"文件{i}.md").pk) for i in range(3)]
    with pytest.raises(AttachmentLimitExceededError):
        task_service.create_task(
            web_actor, create_payload(project, attachment_file_ids=ids), uuid.uuid4().hex
        )


def test_agent_cannot_reference_foreign_staging_file(db, storage, world):
    project = factories.create_project("他人文件项目", status="ACTIVE")
    factories.add_member(project, world.s1)
    other_supervisor_actor = ActorContext(
        actor_type=ActorType.AGENT, agent_id=world.s2.pk, request_id="r"
    )
    foreign_asset = stage_file(other_supervisor_actor)
    supervisor_actor = ActorContext(actor_type=ActorType.AGENT, agent_id=world.s1.pk, request_id="r2")
    with pytest.raises(ResourceNotFoundError):
        task_service.create_task(
            supervisor_actor,
            create_payload(project, attachment_file_ids=[str(foreign_asset.pk)]),
            uuid.uuid4().hex,
        )


def test_prompt_with_urls_flows_into_requirements_snapshot(db, storage, web_actor, world, monkeypatch):
    from domain.claim_service import ClaimRequest, claim_task

    project = factories.create_project("快照项目", status="ACTIVE")
    factories.add_member(project, world.w1)
    asset = stage_file(web_actor, "快照.md")
    monkeypatch.setattr(
        task_service.oss_storage,
        "asset_url",
        lambda a: f"https://signed.example.com/{a.storage_key}",
    )
    task = task_service.create_task(
        web_actor,
        create_payload(project, attachment_file_ids=[str(asset.pk)]),
        uuid.uuid4().hex,
    )
    task_service.publish_task(
        web_actor, task["id"], task["version"], uuid.uuid4().hex
    )

    worker_actor = ActorContext(actor_type=ActorType.AGENT, agent_id=world.w1.pk, request_id="claim")
    claim = claim_task(
        worker_actor,
        ClaimRequest(levels=[2], target_agent_id=world.w1.pk),
        uuid.uuid4().hex,
    )
    snapshot = claim["data"]["requirements_snapshot"]
    assert f"- 快照.md: https://signed.example.com/{asset.storage_key}" in snapshot["description"]


# --------------------------------------------------------------------------------------
# HTTP level (webapi passthrough)
# --------------------------------------------------------------------------------------


def test_webapi_create_task_accepts_attachment_file_ids(web_client, storage, web_actor, world, monkeypatch):
    project = factories.create_project("HTTP 项目", status="ACTIVE")
    factories.add_member(project, world.s1)
    asset = stage_file(web_actor, "http.md")
    monkeypatch.setattr(
        task_service.oss_storage,
        "asset_url",
        lambda a: f"https://signed.example.com/{a.storage_key}",
    )
    response = web_client.post(
        f"{BASE}/tasks",
        data=create_payload(project, attachment_file_ids=[str(asset.pk)]),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert f"https://signed.example.com/{asset.storage_key}" in data["description"]
    assert AttachmentLink.objects.filter(task_id=data["id"], file_asset=asset).exists()
