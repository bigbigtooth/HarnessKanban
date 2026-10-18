"""删除与批量删除的验收（Web 管理面新增能力）。

被测契约：
* Agent 软删除复用禁用的全部前置校验（有效执行处置 WAIT/NOW、最后一名主管保护、幂等回执），
  额外落 ``deleted_at``/``deleted_reason``；恢复 = 启用 + 清空删除标记；历史一行不删。
* 项目软删除复用归档的前置校验（进行中/待审核任务拒绝），冻结为只读并打删除标记；
  恢复回到删除前的生命周期。
* 批量动作逐项独立处理：一项失败不影响其他项，返回逐项成功/失败原因。
* 软删除后的对象从列表、可读范围与看板 Agent 行中消失；已删除 Agent 的 Key 立即失效（401）。
* ``status=ALL`` 让 Web 任务列表一次返回全部五态（任务管理面板依赖它）。

断言读数据库行与真实 HTTP 响应，不使用业务 mock。
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.utils import timezone
from fastapi.testclient import TestClient

from agentapi.main import app as agent_app
from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import IdempotencyRecord, OutboxEvent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskExecution
from domain.contracts import AgentRole, EndReason, ProjectStatus, TaskStatus
from domain.permissions import accessible_project_ids
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.projects]

WEB = "/api/web/v1"
AGENT = "/api/agent/v1"
ALLOWED_HOST = "127.0.0.1:8000"


def _idem() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def web(client):
    client.defaults["HTTP_HOST"] = ALLOWED_HOST
    return client


def _agent_client() -> TestClient:
    return TestClient(agent_app)


def post_json(web, url: str, payload: dict | None = None, *, key: str | None = None):
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return web.post(url, data=json.dumps(payload or {}), content_type="application/json", **headers)


def get_json(web, url: str):
    return web.get(url)


def _body(response) -> dict:
    return json.loads(response.content)


def _active_task(project: Project, title: str, worker: Agent) -> Task:
    """进行中任务：带当前执行与实际执行者（不变量 11.2-1）。

    先以待办落库（满足 ``ck_task_in_progress_shape``），再补执行行并推进到进行中。
    """
    task = factories.create_task(project, title=title, status=TaskStatus.TODO)
    execution = factories.create_execution(task, worker, claimed_at=timezone.now())
    Task.objects.filter(pk=task.pk).update(
        status=TaskStatus.IN_PROGRESS.value,
        assignee_agent=worker,
        active_execution=execution,
    )
    return Task.objects.get(pk=task.pk)


# --------------------------------------------------------------------------------------
# Agent 删除
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_agent_delete_requires_reason_and_hides_from_list(web):
    agent, _ = factories.create_agent("删除-无原因", AgentRole.WORKER)

    missing = post_json(web, f"{WEB}/agents/{agent.pk}/delete", {}, key=_idem())
    assert missing.status_code == 400
    assert _body(missing)["error"]["code"] == "INVALID_ARGUMENT"

    ok = post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理测试数据"}, key=_idem())
    assert ok.status_code == 200
    payload = _body(ok)["data"]
    assert payload["deleted_at"] is not None
    assert payload["deleted_reason"] == "清理测试数据"
    assert payload["enabled"] is False

    agent.refresh_from_db()
    assert agent.deleted_at is not None
    assert agent.enabled is False

    # 默认列表不可见；deleted=true 只看已删除
    visible = _body(get_json(web, f"{WEB}/agents?name=删除-无原因"))["data"]["items"]
    assert visible == []
    deleted_view = _body(get_json(web, f"{WEB}/agents?deleted=true&name=删除-无原因"))["data"]["items"]
    assert [item["id"] for item in deleted_view] == [str(agent.pk)]

    # 详情仍可读（历史可追溯），并带删除字段
    detail = _body(get_json(web, f"{WEB}/agents/{agent.pk}"))["data"]
    assert detail["deleted_at"] is not None

    # 恰好一对活动事件与派发事件
    assert ActivityEvent.objects.filter(action="agent.delete").count() == 1
    assert OutboxEvent.objects.filter(event_type="agent.deleted", entity_id=agent.pk).count() == 1


@pytest.mark.django_db(transaction=True)
def test_agent_delete_reuses_disable_guards_for_open_executions(web):
    """持有有效执行时必须选择 WAIT/NOW；NOW 在同一事务内回收并释放容量。

    真实 AgentAPI 领取跑在独立线程里，所以这里用真实提交（transaction=True），
    名称与项目都必须唯一，避免污染后续用例。
    """
    factories.ensure_claim_lock()
    suffix = factories.unique_name("删除")
    supervisor, _ = factories.create_agent(f"删除-主管-{suffix}", AgentRole.SUPERVISOR)
    worker, worker_key = factories.create_agent(
        factories.unique_name("删除-执行者"), AgentRole.WORKER, max_concurrency=1
    )
    project = factories.create_project(factories.unique_name("删除-项目"))
    factories.add_member(project, supervisor)
    factories.add_member(project, worker)
    factories.create_task(project, title=factories.unique_name("删除-任务"), difficulty=1)

    with _agent_client() as agent_client:
        claimed = agent_client.post(
            f"{AGENT}/get_task/?l1=1",
            json={},
            headers={"AGENT_TOKEN": worker_key, "Idempotency-Key": _idem()},
        )
    assert claimed.status_code == 200, claimed.content
    execution_id = claimed.json()["data"]["execution_id"]

    refused = post_json(web, f"{WEB}/agents/{worker.pk}/delete", {"reason": "清理"}, key=_idem())
    assert refused.status_code == 400
    assert _body(refused)["error"]["code"] == "INVALID_ARGUMENT"

    reclaimed = post_json(
        web,
        f"{WEB}/agents/{worker.pk}/delete",
        {"reason": "清理", "reclaim": "NOW"},
        key=_idem(),
    )
    assert reclaimed.status_code == 200
    body = _body(reclaimed)["data"]
    assert body["disposition"] == "NOW"
    assert body["reclaimed_execution_ids"] == [execution_id]

    task = Task.objects.get(active_execution__isnull=True, project=project)
    assert task.status == TaskStatus.TODO.value
    worker.refresh_from_db()
    assert worker.reserved_execution_count == 0
    assert worker.deleted_at is not None
    execution = TaskExecution.objects.get(pk=execution_id)
    assert execution.ended_at is not None
    assert execution.end_reason == EndReason.RECLAIM.value


@pytest.mark.django_db
def test_agent_delete_respects_last_supervisor_guard(web):
    """进行中项目的最后一名主管不能被删除。"""
    supervisor, _ = factories.create_agent("唯一主管", AgentRole.SUPERVISOR)
    project = factories.create_project("唯一主管-项目")
    factories.add_member(project, supervisor)

    refused = post_json(web, f"{WEB}/agents/{supervisor.pk}/delete", {"reason": "清理"}, key=_idem())
    assert refused.status_code == 409
    assert _body(refused)["error"]["code"] == "LAST_SUPERVISOR_REQUIRED"
    supervisor.refresh_from_db()
    assert supervisor.deleted_at is None


@pytest.mark.django_db(transaction=True)
def test_deleted_agent_key_immediately_rejected(web):
    """删除后 Key 立即失效：AgentAPI 返回 401 AGENT_DISABLED。"""
    agent, raw_key = factories.create_agent("删除-Key失效", AgentRole.WORKER)
    assert post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理"}, key=_idem()).status_code == 200

    with _agent_client() as agent_client:
        response = agent_client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": raw_key})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AGENT_DISABLED"


@pytest.mark.django_db
def test_deleted_project_leaves_agent_access_scope(web):
    """项目被软删除后，成员关系行保留，但项目离开 Agent 的可访问范围。"""
    agent, _ = factories.create_agent("删除-成员", AgentRole.WORKER)
    project = factories.create_project("删除-成员-项目")
    factories.add_member(project, agent)
    assert str(project.pk) in {str(pk) for pk in accessible_project_ids(agent.pk)}

    response = post_json(
        web,
        f"{WEB}/projects/{project.pk}/delete",
        {"reason": "清理", "expected_version": project.version},
        key=_idem(),
    )
    assert response.status_code == 200
    assert ProjectMember.objects.filter(project=project, agent=agent).exists()
    assert str(project.pk) not in {str(pk) for pk in accessible_project_ids(agent.pk)}


@pytest.mark.django_db
def test_deleted_agent_keeps_membership_row_for_history(web):
    """删除 Agent 不删成员关系行（历史可追溯）；Key 失效由认证层挡住。"""
    agent, _ = factories.create_agent("删除-成员-历史", AgentRole.WORKER)
    project = factories.create_project("删除-成员-历史-项目")
    factories.add_member(project, agent)

    assert post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理"}, key=_idem()).status_code == 200
    assert ProjectMember.objects.filter(project=project, agent=agent).exists()


@pytest.mark.django_db
def test_agent_restore_clears_deletion_and_reenables(web):
    agent, _ = factories.create_agent("删除-恢复", AgentRole.WORKER)
    assert post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理"}, key=_idem()).status_code == 200

    restored = post_json(web, f"{WEB}/agents/{agent.pk}/enable", {"rotate_key": False}, key=_idem())
    assert restored.status_code == 200
    payload = _body(restored)["data"]
    assert payload["deleted_at"] is None
    assert payload["enabled"] is True

    agent.refresh_from_db()
    assert agent.deleted_at is None
    assert agent.enabled is True
    visible = _body(get_json(web, f"{WEB}/agents?name=删除-恢复"))["data"]["items"]
    assert [item["id"] for item in visible] == [str(agent.pk)]


@pytest.mark.django_db
def test_agent_delete_is_idempotent_on_replay(web):
    agent, _ = factories.create_agent("删除-幂等", AgentRole.WORKER)
    key = _idem()
    assert post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理"}, key=key).status_code == 200
    replay = post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理"}, key=key)
    assert replay.status_code == 200
    assert _body(replay)["data"]["idempotent_replay"] is True
    assert IdempotencyRecord.objects.filter(key=key).count() == 1


# --------------------------------------------------------------------------------------
# Agent 批量删除 / 批量恢复
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_agent_bulk_delete_reports_per_item_failures(web):
    """一项失败（最后主管保护）不影响其他项，逐项原因可见。"""
    supervisor, _ = factories.create_agent("批量-主管", AgentRole.SUPERVISOR)
    project = factories.create_project("批量-项目")
    factories.add_member(project, supervisor)
    worker, _ = factories.create_agent("批量-员工", AgentRole.WORKER)

    response = post_json(
        web,
        f"{WEB}/agents/bulk-delete",
        {"agent_ids": [str(worker.pk), str(supervisor.pk)], "reason": "清理"},
        key=_idem(),
    )
    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["requested"] == 2
    assert [item["id"] for item in data["succeeded"]] == [str(worker.pk)]
    assert [item["id"] for item in data["failed"]] == [str(supervisor.pk)]
    assert data["failed"][0]["code"]
    assert data["failed"][0]["message"]

    worker.refresh_from_db()
    supervisor.refresh_from_db()
    assert worker.deleted_at is not None
    assert supervisor.deleted_at is None


@pytest.mark.django_db
def test_agent_bulk_delete_validates_payload(web):
    worker, _ = factories.create_agent("批量-校验", AgentRole.WORKER)

    empty = post_json(web, f"{WEB}/agents/bulk-delete", {"agent_ids": [], "reason": "清理"}, key=_idem())
    assert empty.status_code == 400
    no_reason = post_json(web, f"{WEB}/agents/bulk-delete", {"agent_ids": [str(worker.pk)]}, key=_idem())
    assert no_reason.status_code == 400
    bad_uuid = post_json(
        web, f"{WEB}/agents/bulk-delete", {"agent_ids": ["not-a-uuid"], "reason": "清理"}, key=_idem()
    )
    assert bad_uuid.status_code == 400

    worker.refresh_from_db()
    assert worker.deleted_at is None


@pytest.mark.django_db
def test_agent_bulk_delete_is_idempotent_per_item(web):
    worker, _ = factories.create_agent("批量-幂等", AgentRole.WORKER)
    key = _idem()
    assert (
        post_json(
            web, f"{WEB}/agents/bulk-delete", {"agent_ids": [str(worker.pk)], "reason": "清理"}, key=key
        ).status_code
        == 200
    )
    replay = post_json(
        web, f"{WEB}/agents/bulk-delete", {"agent_ids": [str(worker.pk)], "reason": "清理"}, key=key
    )
    assert replay.status_code == 200
    data = _body(replay)["data"]
    assert data["requested"] == 1
    assert data["succeeded"][0]["idempotent_replay"] is True


@pytest.mark.django_db
def test_agent_bulk_restore(web):
    first, _ = factories.create_agent("批量恢复-甲", AgentRole.WORKER)
    second, _ = factories.create_agent("批量恢复-乙", AgentRole.WORKER)
    for agent in (first, second):
        assert post_json(web, f"{WEB}/agents/{agent.pk}/delete", {"reason": "清理"}, key=_idem()).status_code == 200

    response = post_json(
        web, f"{WEB}/agents/bulk-restore", {"agent_ids": [str(first.pk), str(second.pk)]}, key=_idem()
    )
    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["requested"] == 2
    assert len(data["succeeded"]) == 2
    assert data["failed"] == []
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.deleted_at is None and first.enabled
    assert second.deleted_at is None and second.enabled


# --------------------------------------------------------------------------------------
# 项目删除 / 恢复 / 批量
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_project_delete_freezes_and_hides(web):
    project = factories.create_project("项目删除-甲")
    factories.create_task(project, title="项目删除-任务", status=TaskStatus.TODO)

    refused = post_json(web, f"{WEB}/projects/{project.pk}/delete", {}, key=_idem())
    assert refused.status_code == 400

    ok = post_json(
        web,
        f"{WEB}/projects/{project.pk}/delete",
        {"reason": "清理", "expected_version": project.version},
        key=_idem(),
    )
    assert ok.status_code == 200
    payload = _body(ok)["data"]
    assert payload["deleted_at"] is not None
    assert payload["status"] == ProjectStatus.ARCHIVED.value
    assert payload["archived_from_status"] == ProjectStatus.ACTIVE.value

    project.refresh_from_db()
    assert project.deleted_at is not None

    default_list = _body(get_json(web, f"{WEB}/projects?page_size=100"))["data"]["items"]
    assert str(project.pk) not in {item["id"] for item in default_list}
    deleted_list = _body(get_json(web, f"{WEB}/projects?deleted=true&page_size=100"))["data"]["items"]
    assert str(project.pk) in {item["id"] for item in deleted_list}

    # 任务、上下文版本与历史事件一行不删
    assert Task.objects.filter(project=project).count() == 1
    assert ActivityEvent.objects.filter(project_id=project.pk, action="project.delete").count() == 1
    assert OutboxEvent.objects.filter(event_type="project.deleted", entity_id=project.pk).count() == 1


@pytest.mark.django_db
def test_project_delete_refused_with_active_tasks(web):
    worker, _ = factories.create_agent("项目删除-执行者", AgentRole.WORKER)
    project = factories.create_project("项目删除-进行中")
    factories.add_member(project, worker)
    task = _active_task(project, "项目删除-进行中任务", worker)

    refused = post_json(
        web,
        f"{WEB}/projects/{project.pk}/delete",
        {"reason": "清理", "expected_version": project.version},
        key=_idem(),
    )
    assert refused.status_code == 409
    assert _body(refused)["error"]["code"] == "INVALID_TRANSITION"
    project.refresh_from_db()
    assert project.deleted_at is None
    task.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value


@pytest.mark.django_db
def test_project_restore_clears_deletion_and_returns_lifecycle(web):
    project = factories.create_project("项目删除-恢复")
    assert (
        post_json(
            web,
            f"{WEB}/projects/{project.pk}/delete",
            {"reason": "清理", "expected_version": project.version},
            key=_idem(),
        ).status_code
        == 200
    )

    restored = post_json(
        web,
        f"{WEB}/projects/{project.pk}/restore",
        {"expected_version": project.version + 1},
        key=_idem(),
    )
    assert restored.status_code == 200
    payload = _body(restored)["data"]
    assert payload["status"] == ProjectStatus.ACTIVE.value
    assert payload["deleted_at"] is None
    project.refresh_from_db()
    assert project.deleted_at is None


@pytest.mark.django_db
def test_project_bulk_delete_and_restore(web):
    first = factories.create_project("项目批量-甲", status=ProjectStatus.DRAFT)
    second = factories.create_project("项目批量-乙", status=ProjectStatus.DRAFT)
    blocked = factories.create_project("项目批量-进行中")
    worker, _ = factories.create_agent("项目批量-执行者", AgentRole.WORKER)
    factories.add_member(blocked, worker)
    _active_task(blocked, "项目批量-进行中任务", worker)

    response = post_json(
        web,
        f"{WEB}/projects/bulk-delete",
        {"project_ids": [str(first.pk), str(blocked.pk), str(second.pk)], "reason": "清理"},
        key=_idem(),
    )
    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["requested"] == 3
    assert [item["id"] for item in data["succeeded"]] == [str(first.pk), str(second.pk)]
    assert [item["id"] for item in data["failed"]] == [str(blocked.pk)]
    assert data["failed"][0]["code"] == "INVALID_TRANSITION"

    restore = post_json(
        web,
        f"{WEB}/projects/bulk-restore",
        {"project_ids": [str(first.pk), str(second.pk)]},
        key=_idem(),
    )
    assert restore.status_code == 200
    assert len(_body(restore)["data"]["succeeded"]) == 2
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.deleted_at is None and first.status == ProjectStatus.DRAFT.value
    assert second.deleted_at is None and second.status == ProjectStatus.DRAFT.value


# --------------------------------------------------------------------------------------
# 任务 status=ALL 与批量归档 / 恢复
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_list_status_all_returns_every_status(web):
    project = factories.create_project("任务ALL-项目")
    factories.create_task(project, title="任务ALL-准备", status=TaskStatus.PREPARING)
    factories.create_task(project, title="任务ALL-待办", status=TaskStatus.TODO)
    factories.create_task(project, title="任务ALL-待办二", status=TaskStatus.TODO)

    todo_only = _body(get_json(web, f"{WEB}/tasks?project_id={project.pk}&page_size=50"))["data"]
    assert todo_only["total"] == 2

    everything = _body(get_json(web, f"{WEB}/tasks?project_id={project.pk}&status=ALL&page_size=50"))["data"]
    assert everything["total"] == 3
    assert {item["status"] for item in everything["items"]} == {
        TaskStatus.PREPARING.value,
        TaskStatus.TODO.value,
    }


@pytest.mark.django_db
def test_task_bulk_archive_and_restore(web):
    project = factories.create_project("任务批量-项目")
    first = factories.create_task(project, title="任务批量-甲", status=TaskStatus.TODO)
    second = factories.create_task(project, title="任务批量-乙", status=TaskStatus.TODO)
    worker, _ = factories.create_agent("任务批量-执行者", AgentRole.WORKER)
    factories.add_member(project, worker)
    active = _active_task(project, "任务批量-进行中", worker)

    response = post_json(
        web,
        f"{WEB}/tasks/bulk-archive",
        {"task_ids": [str(first.pk), str(active.pk), str(second.pk)], "reason": "清理"},
        key=_idem(),
    )
    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["requested"] == 3
    assert {item["id"] for item in data["succeeded"]} == {str(first.pk), str(second.pk)}
    assert [item["id"] for item in data["failed"]] == [str(active.pk)]
    assert data["failed"][0]["code"] == "INVALID_TRANSITION"

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_archived and second.is_archived
    # 五态状态不变：归档只是隐藏标记
    assert first.status == TaskStatus.TODO.value

    default_list = _body(get_json(web, f"{WEB}/tasks?project_id={project.pk}&status=ALL&page_size=50"))["data"]
    assert {item["id"] for item in default_list["items"]} & {str(first.pk), str(second.pk)} == set()

    restore = post_json(web, f"{WEB}/tasks/bulk-restore", {"task_ids": [str(first.pk), str(second.pk)]}, key=_idem())
    assert restore.status_code == 200
    assert len(_body(restore)["data"]["succeeded"]) == 2
    first.refresh_from_db()
    second.refresh_from_db()
    assert not first.is_archived and not second.is_archived


@pytest.mark.django_db
def test_soft_delete_never_removes_history_rows(web):
    """删除与批量删除都不产生物理删除：历史行保留（product 11.1「保护历史」）。"""
    supervisor, _ = factories.create_agent("历史-主管", AgentRole.SUPERVISOR)
    worker, _ = factories.create_agent("历史-员工", AgentRole.WORKER)
    project = factories.create_project("历史-项目")
    factories.add_member(project, supervisor)
    factories.add_member(project, worker)
    task = factories.create_task(project, title="历史-任务", status=TaskStatus.TODO)

    assert post_json(web, f"{WEB}/agents/{worker.pk}/delete", {"reason": "清理"}, key=_idem()).status_code == 200
    assert (
        post_json(
            web,
            f"{WEB}/projects/{project.pk}/delete",
            {"reason": "清理", "expected_version": project.version},
            key=_idem(),
        ).status_code
        == 200
    )

    assert Agent.objects.filter(pk=worker.pk).exists()
    assert Project.objects.filter(pk=project.pk).exists()
    assert ProjectMember.objects.filter(project=project, agent=worker).exists()
    assert Task.objects.filter(pk=task.pk).exists()
