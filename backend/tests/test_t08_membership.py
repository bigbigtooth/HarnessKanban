"""T08 acceptance: global role and enable/disable, project membership authorisation, the
last-supervisor guard (including two real concurrent revocations) and the execution
disposition (WAIT keeps the slot, NOW reclaims it in the same transaction through the frozen
T04 primitives).

Assertions read the database rows: ``ProjectMember``, ``Agent.role/enabled/version``,
``TaskExecution.ended_at/end_reason``, ``Task.status/active_execution_id/version``,
``Agent.reserved_execution_count``, ``ActivityEvent`` and ``OutboxEvent``.
"""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from django.db import connections
from fastapi.testclient import TestClient

from agentapi.main import app as agent_app
from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import IdempotencyRecord, OutboxEvent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now
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


def post_json(web, url: str, payload: dict | None = None, *, key: str | None = None, **extra):
    headers = dict(extra)
    if key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    return web.post(url, data=json.dumps(payload or {}), content_type="application/json", **headers)


def put_json(web, url: str, payload: dict | None = None, *, key: str | None = None, **extra):
    headers = dict(extra)
    if key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    return web.put(url, data=json.dumps(payload or {}), content_type="application/json", **headers)


def delete_json(web, url: str, payload: dict | None = None, *, key: str | None = None, **extra):
    headers = dict(extra)
    if key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    return web.delete(url, data=json.dumps(payload or {}), content_type="application/json", **headers)


def create_agent_api(web, **overrides) -> tuple[dict, str]:
    body = {"name": factories.unique_name("T08"), "role": "WORKER", "max_concurrency": 2}
    body.update(overrides)
    response = post_json(web, f"{WEB}/agents", body, key=_idem())
    assert response.status_code == 201, response.content
    return response.json()["data"], response.json()["data"]["agent_key"]


def hold_execution(agent: Agent, project, *, difficulty: int = 2, timeout_seconds: int = 1800):
    """One IN_PROGRESS task with an open execution and one reserved capacity slot."""
    agent.reserved_execution_count = agent.reserved_execution_count + 1
    if agent.max_concurrency < agent.reserved_execution_count:
        agent.max_concurrency = agent.reserved_execution_count
    agent.save(update_fields=["reserved_execution_count", "max_concurrency"])

    task = factories.create_task(project, title="执行中的任务", difficulty=difficulty)
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


def _side_effect_baseline() -> tuple[int, int, int]:
    """Row counts before a request; the create call itself already wrote one of each."""
    return (
        ActivityEvent.objects.count(),
        OutboxEvent.objects.count(),
        IdempotencyRecord.objects.count(),
    )


def _assert_no_side_effects(baseline: tuple[int, int, int]) -> None:
    assert ActivityEvent.objects.count() == baseline[0], "unexpected activity event"
    assert OutboxEvent.objects.count() == baseline[1], "unexpected outbox event"
    assert IdempotencyRecord.objects.count() == baseline[2], "receipt survived the rollback"


def _member_url(project_id, agent_id) -> str:
    return f"{WEB}/projects/{project_id}/members/{agent_id}"


def _assert_reclaimed(task: Task, execution: TaskExecution, *, version_before: int):
    """Immediate reclaim: ended execution, TODO task, cleared ownership, one version bump."""
    execution.refresh_from_db()
    task.refresh_from_db()
    assert execution.ended_at is not None
    assert execution.end_reason == EndReason.RECLAIM.value
    assert task.status == TaskStatus.TODO.value
    assert task.active_execution_id is None
    assert task.assignee_agent_id is None
    assert task.claimed_at is None
    assert task.submitted_at is None
    assert task.version == version_before + 1  # exactly one CAS bump, never two


def _reclaim_events(execution: TaskExecution) -> tuple[list[ActivityEvent], list[OutboxEvent]]:
    activities = list(ActivityEvent.objects.filter(execution_id=execution.pk, action="reclaim"))
    outbox = list(
        OutboxEvent.objects.filter(
            entity_id=execution.pk, event_type="task.execution.reclaimed"
        )
    )
    return activities, outbox


# --------------------------------------------------------------------------------------
# Global role
# --------------------------------------------------------------------------------------


def test_role_change_promotes_and_demotes_the_global_role(web, world):
    data, _key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    url = f"{WEB}/agents/{agent.pk}/role"

    promoted = post_json(web, url, {"role": "SUPERVISOR", "expected_version": 1}, key=_idem())
    assert promoted.status_code == 200
    assert promoted.json()["data"]["role"] == AgentRole.SUPERVISOR.value
    agent.refresh_from_db()
    assert agent.role == AgentRole.SUPERVISOR.value
    assert agent.version == 2
    assert ProjectMember.objects.filter(agent_id=agent.pk).count() == 1  # authorisation intact

    demoted = post_json(web, url, {"role": "WORKER", "expected_version": 2}, key=_idem())
    assert demoted.status_code == 200
    agent.refresh_from_db()
    assert agent.role == AgentRole.WORKER.value
    assert agent.version == 3

    events = ActivityEvent.objects.filter(action="agent.role.update").order_by("created_at")
    assert [event.after["role"] for event in events] == ["SUPERVISOR", "WORKER"]
    assert OutboxEvent.objects.filter(event_type="agent.role.updated").count() == 2

    for body in ({"role": "ADMIN", "expected_version": 3}, {"expected_version": 3}, {"role": "WORKER"}):
        bad = post_json(web, url, body, key=_idem())
        assert bad.status_code == 400, (body, bad.content)
        assert bad.json()["error"]["code"] == "INVALID_ARGUMENT"

    stale = post_json(web, url, {"role": "SUPERVISOR", "expected_version": 1}, key=_idem())
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    agent.refresh_from_db()
    assert agent.role == AgentRole.WORKER.value
    assert agent.version == 3


def test_role_change_cannot_drop_the_last_supervisor_of_any_active_project(web, world):
    """The guard spans *all* affected ACTIVE projects, not only the project in the URL."""
    data, _key = create_agent_api(web, role="SUPERVISOR")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)  # S1/S2 stay supervisors here
    only_supervisor_project = factories.create_project(
        "只有一名主管的项目", status=ProjectStatus.ACTIVE
    )
    factories.add_member(only_supervisor_project, agent)

    response = post_json(
        web,
        f"{WEB}/agents/{agent.pk}/role",
        {"role": "WORKER", "expected_version": agent.version},
        key=_idem(),
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "LAST_SUPERVISOR_REQUIRED"
    assert payload["error"]["details"]["project_id"] == str(only_supervisor_project.pk)
    agent.refresh_from_db()
    assert agent.role == AgentRole.SUPERVISOR.value
    assert agent.version == 1
    assert ActivityEvent.objects.filter(action="agent.role.update").count() == 0

    # With a second enabled supervisor in that project the same change is allowed.
    replacement = Agent.objects.get(pk=world.s2.pk)
    factories.add_member(only_supervisor_project, replacement)
    allowed = post_json(
        web,
        f"{WEB}/agents/{agent.pk}/role",
        {"role": "WORKER", "expected_version": 1},
        key=_idem(),
    )
    assert allowed.status_code == 200
    agent.refresh_from_db()
    assert agent.role == AgentRole.WORKER.value
    assert agent.version == 2


# --------------------------------------------------------------------------------------
# Disable / enable
# --------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_disable_without_executions_revokes_the_key_and_keeps_authorisation(web, world):
    data, raw_key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)

    response = post_json(
        web,
        f"{WEB}/agents/{agent.pk}/disable",
        {"reason": "计划维护"},
        key=_idem(),
    )

    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert body["enabled"] is False
    assert body["disposition"] == "WAIT"
    agent.refresh_from_db()
    assert agent.enabled is False
    assert agent.version == 2
    assert agent.key_version == 1
    # Membership rows are authorisation, not status: disabling never deletes them.
    assert ProjectMember.objects.filter(agent_id=agent.pk).count() == 1

    disabled_call = _agent_client().get(f"{AGENT}/me", headers={"AGENT_TOKEN": raw_key})
    assert disabled_call.status_code == 401
    assert disabled_call.json()["error"]["code"] == "AGENT_DISABLED"

    events = list(ActivityEvent.objects.filter(action="agent.disable"))
    assert len(events) == 1
    assert events[0].actor_type == "web_operator"
    assert events[0].reason == "计划维护"
    assert events[0].after["enabled"] is False
    assert ActivityEvent.objects.filter(action="reclaim").count() == 0
    assert OutboxEvent.objects.filter(event_type="agent.disabled").count() == 1

    unknown = post_json(web, f"{WEB}/agents/{uuid.uuid4()}/disable", {}, key=_idem())
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    without_key = web.post(
        f"{WEB}/agents/{agent.pk}/disable",
        data=json.dumps({"reason": "x"}),
        content_type="application/json",
    )
    assert without_key.status_code == 400
    assert without_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_disable_requires_an_explicit_disposition_when_executions_are_open(web, world):
    data, _key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    url = f"{WEB}/agents/{agent.pk}/disable"
    baseline = _side_effect_baseline()

    missing = post_json(web, url, {"reason": "x"}, key=_idem())
    assert missing.status_code == 400
    payload = missing.json()
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert payload["error"]["details"]["open_executions"] == 1

    no_reason = post_json(web, url, {"reclaim": "NOW"}, key=_idem())
    assert no_reason.status_code == 400
    assert no_reason.json()["error"]["details"]["field"] == "reason"

    bad_value = post_json(web, url, {"reclaim": "LATER", "reason": "x"}, key=_idem())
    assert bad_value.status_code == 400

    agent.refresh_from_db()
    execution.refresh_from_db()
    task.refresh_from_db()
    assert agent.enabled is True
    assert agent.version == 1
    assert execution.ended_at is None
    assert task.status == TaskStatus.IN_PROGRESS.value
    _assert_no_side_effects(baseline)  # the whole request rolled back


@pytest.mark.django_db(transaction=True)
@pytest.mark.workflow
def test_disable_wait_keeps_the_execution_and_the_slot(web, world):
    data, raw_key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    deadline = execution.execution_deadline_at

    response = post_json(
        web,
        f"{WEB}/agents/{agent.pk}/disable",
        {"reason": "等待自然到期", "reclaim": "WAIT"},
        key=_idem(),
    )

    assert response.status_code == 200
    assert response.json()["data"]["reclaimed_execution_ids"] == []
    execution.refresh_from_db()
    task.refresh_from_db()
    agent.refresh_from_db()
    assert execution.ended_at is None
    assert execution.execution_deadline_at == deadline
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk
    assert task.version == 2
    assert agent.reserved_execution_count == 1
    assert agent.enabled is False
    assert ActivityEvent.objects.filter(action="reclaim").count() == 0
    assert OutboxEvent.objects.filter(event_type="task.execution.reclaimed").count() == 0

    # The key is dead immediately even though the execution keeps running.
    assert _agent_client().get(f"{AGENT}/me", headers={"AGENT_TOKEN": raw_key}).status_code == 401


@pytest.mark.workflow
def test_disable_now_reclaims_every_open_execution_in_one_transaction(web, world):
    data, _key = create_agent_api(web, role="WORKER", max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    factories.add_member(world.project_b, agent)
    task_a, execution_a = hold_execution(agent, world.project_a)
    task_b, execution_b = hold_execution(agent, world.project_b)
    task_a_version = task_a.version
    task_b_version = task_b.version
    agent.refresh_from_db()
    assert agent.reserved_execution_count == 2

    response = post_json(
        web,
        f"{WEB}/agents/{agent.pk}/disable",
        {"reason": "紧急下线回收", "reclaim": "NOW"},
        key=_idem(),
    )

    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert sorted(body["reclaimed_execution_ids"]) == sorted([str(execution_a.pk), str(execution_b.pk)])
    assert body["disposition"] == "NOW"

    _assert_reclaimed(task_a, execution_a, version_before=task_a_version)
    _assert_reclaimed(task_b, execution_b, version_before=task_b_version)

    agent.refresh_from_db()
    assert agent.enabled is False
    assert agent.reserved_execution_count == 0
    assert agent.available_capacity == agent.max_concurrency
    assert ProjectMember.objects.filter(agent_id=agent.pk).count() == 2

    # Exactly one activity event plus one outbox event per ended execution.
    for execution in (execution_a, execution_b):
        activities, outbox = _reclaim_events(execution)
        assert len(activities) == 1
        assert activities[0].actor_type == "web_operator"
        assert activities[0].reason == "紧急下线回收"
        assert activities[0].after["task_status"] == TaskStatus.TODO.value
        assert len(outbox) == 1
        assert outbox[0].payload["agent_id"] == str(agent.pk)
    assert ActivityEvent.objects.filter(action="reclaim").count() == 2
    assert OutboxEvent.objects.filter(event_type="task.execution.reclaimed").count() == 2
    assert ActivityEvent.objects.filter(action="agent.disable").count() == 1

    # The ended executions can no longer be written by their old owner: the task no longer
    # points at them, they carry an end timestamp and no reason to be extended. (The write
    # endpoints themselves arrive with the progress/submission tasks.)
    assert TaskExecution.objects.filter(task_id=task_a.pk, ended_at__isnull=True).count() == 0
    assert Task.objects.get(pk=task_a.pk).active_execution_id is None


@pytest.mark.workflow
@pytest.mark.fault
def test_disable_now_rolls_back_completely_when_the_slot_release_fails(web, world):
    data, _key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    # Inconsistent capacity: the execution is open but no slot is reserved.
    Agent.objects.filter(pk=agent.pk).update(reserved_execution_count=0)
    baseline = _side_effect_baseline()

    response = post_json(
        web,
        f"{WEB}/agents/{agent.pk}/disable",
        {"reason": "回收", "reclaim": "NOW"},
        key=_idem(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_BUSY"

    agent.refresh_from_db()
    execution.refresh_from_db()
    task.refresh_from_db()
    assert agent.enabled is True  # the disable rolled back with the failed reclaim
    assert agent.version == 1
    assert execution.ended_at is None
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.version == 2
    assert ProjectMember.objects.filter(agent_id=agent.pk).count() == 1
    _assert_no_side_effects(baseline)


@pytest.mark.django_db(transaction=True)
def test_enable_restores_the_same_identity_and_authorisation(web, world):
    data, raw_key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    disable = post_json(web, f"{WEB}/agents/{agent.pk}/disable", {"reason": "维护"}, key=_idem())
    assert disable.status_code == 200
    before = Agent.objects.get(pk=agent.pk)
    assert before.enabled is False

    response = post_json(web, f"{WEB}/agents/{agent.pk}/enable", {}, key=_idem())

    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert body["id"] == str(agent.pk)
    assert body["enabled"] is True
    assert "agent_key" not in body
    assert body["project_ids"] == [str(world.project_a.pk)]

    after = Agent.objects.get(pk=agent.pk)
    assert after.enabled is True
    assert after.version == before.version + 1
    assert after.key_version == 1  # no rotation => the same key keeps working
    assert after.key_hash == before.key_hash
    assert Agent.objects.filter(pk=agent.pk).count() == 1  # no new identity
    assert ProjectMember.objects.filter(agent_id=agent.pk).count() == 1

    reused = _agent_client().get(f"{AGENT}/me", headers={"AGENT_TOKEN": raw_key})
    assert reused.status_code == 200
    assert reused.json()["data"]["enabled"] is True
    assert reused.json()["data"]["project_ids"] == [str(world.project_a.pk)]

    # Enabling an already enabled agent is a documented no-op (no version bump, no event).
    version_now = after.version
    again = post_json(web, f"{WEB}/agents/{agent.pk}/enable", {}, key=_idem())
    assert again.status_code == 200
    assert Agent.objects.get(pk=agent.pk).version == version_now
    assert ActivityEvent.objects.filter(action="agent.enable").count() == 1


@pytest.mark.django_db(transaction=True)
def test_enable_with_rotate_key_delivers_the_new_key_once(web, world):
    data, old_key = create_agent_api(web, role="WORKER")
    agent = Agent.objects.get(pk=data["id"])
    assert post_json(web, f"{WEB}/agents/{agent.pk}/disable", {"reason": "m"}, key=_idem()).status_code == 200
    idem_key = _idem()

    response = post_json(
        web, f"{WEB}/agents/{agent.pk}/enable", {"rotate_key": True}, key=idem_key
    )

    assert response.status_code == 200
    new_key = response.json()["data"]["agent_key"]
    assert new_key != old_key
    agent.refresh_from_db()
    assert agent.key_version == 2
    assert agent.enabled is True

    client = _agent_client()
    assert client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": new_key}).status_code == 200
    stale = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": old_key})
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "AGENT_TOKEN_INVALID"

    replay = post_json(
        web, f"{WEB}/agents/{agent.pk}/enable", {"rotate_key": True}, key=idem_key
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["key_delivery_unavailable"] is True
    assert "agent_key" not in replay.json()["data"]
    agent.refresh_from_db()
    assert agent.key_version == 2  # no second rotation
    assert ActivityEvent.objects.filter(action="agent.enable").count() == 1
    assert IdempotencyRecord.objects.filter(operation="POST /api/web/v1/agents/{id}/enable").count() == 1


def test_disable_refuses_to_strip_the_last_supervisor_of_an_active_project(web, world):
    data, _key = create_agent_api(web, role="SUPERVISOR")
    supervisor = Agent.objects.get(pk=data["id"])
    project = factories.create_project("唯一主管项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, supervisor)
    other = Agent.objects.get(pk=world.s2.pk)
    assert ProjectMember.objects.filter(project=project).count() == 1

    response = post_json(
        web, f"{WEB}/agents/{supervisor.pk}/disable", {"reason": "下线"}, key=_idem()
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "LAST_SUPERVISOR_REQUIRED"
    assert payload["error"]["details"]["project_id"] == str(project.pk)
    supervisor.refresh_from_db()
    assert supervisor.enabled is True
    assert supervisor.version == 1

    # Adding a second enabled supervisor unblocks the same operation.
    factories.add_member(project, other)
    allowed = post_json(
        web,
        f"{WEB}/agents/{supervisor.pk}/disable",
        {"reason": "下线", "reclaim": "WAIT"},
        key=_idem(),
    )
    assert allowed.status_code == 200
    supervisor.refresh_from_db()
    assert supervisor.enabled is False


# --------------------------------------------------------------------------------------
# Membership: add / list
# --------------------------------------------------------------------------------------


def test_membership_add_is_conflict_on_an_existing_member(web, world):
    data, _key = create_agent_api(web)
    agent = Agent.objects.get(pk=data["id"])
    url = _member_url(world.project_a.pk, agent.pk)
    idem_key = _idem()

    first = put_json(web, url, {}, key=idem_key)
    assert first.status_code == 201, first.content
    assert first.json()["data"]["agent_id"] == str(agent.pk)
    assert first.json()["data"]["role"] == AgentRole.WORKER.value
    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 1

    replay = put_json(web, url, {}, key=idem_key)
    assert replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True
    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 1

    duplicate = put_json(web, url, {}, key=_idem())
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "MEMBERSHIP_CONFLICT"
    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 1

    without_key = web.put(url, data="{}", content_type="application/json")
    assert without_key.status_code == 400
    assert without_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    unknown_field = put_json(web, url, {"role": "SUPERVISOR"}, key=_idem())
    assert unknown_field.status_code == 400
    assert unknown_field.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_membership_add_requires_an_existing_enabled_agent_and_editable_project(web, world):
    # The create endpoint always produces enabled agents, so a disabled one comes from the
    # domain fixture: adding it to a project must be refused (it could never work the task).
    disabled_agent, _raw = factories.create_agent(
        factories.unique_name("T08OFF"), AgentRole.WORKER, enabled=False
    )

    disabled_response = put_json(
        web, _member_url(world.project_a.pk, disabled_agent.pk), {}, key=_idem()
    )
    assert disabled_response.status_code == 409
    assert disabled_response.json()["error"]["code"] == "INVALID_TRANSITION"
    assert ProjectMember.objects.filter(agent_id=disabled_agent.pk).count() == 0

    unknown_agent = put_json(web, _member_url(world.project_a.pk, uuid.uuid4()), {}, key=_idem())
    assert unknown_agent.status_code == 404
    assert unknown_agent.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    active, _key2 = create_agent_api(web)
    unknown_project = put_json(web, _member_url(uuid.uuid4(), active["id"]), {}, key=_idem())
    assert unknown_project.status_code == 404

    archived = put_json(
        web, _member_url(world.archived_project.pk, active["id"]), {}, key=_idem()
    )
    assert archived.status_code == 404
    assert archived.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert ProjectMember.objects.filter(project=world.archived_project).count() == 0


def test_members_list_reports_role_and_connectivity_state(web, world):
    response = web.get(f"{WEB}/projects/{world.project_a.pk}/members")

    assert response.status_code == 200
    body = response.json()["data"]
    assert set(body.keys()) == {"items", "total", "page", "page_size"}
    assert body["total"] == 4
    by_name = {item["agent_name"]: item for item in body["items"]}
    assert set(by_name) == {"S1", "S2", "W1", "W2"}
    assert by_name["S1"]["role"] == AgentRole.SUPERVISOR.value
    assert by_name["W1"]["role"] == AgentRole.WORKER.value
    assert by_name["W1"]["enabled"] is True
    assert by_name["W1"]["online_state"] == "NEVER_CONNECTED"
    assert by_name["W1"]["project_id"] == str(world.project_a.pk)

    missing = web.get(f"{WEB}/projects/{uuid.uuid4()}/members")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    unsupported = web.get(f"{WEB}/projects/{world.project_a.pk}/members?nonsense=1")
    assert unsupported.status_code == 400


# --------------------------------------------------------------------------------------
# Membership: removal with disposition
# --------------------------------------------------------------------------------------


def test_membership_remove_requires_a_disposition_and_a_reason(web, world):
    data, _key = create_agent_api(web)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    url = _member_url(world.project_a.pk, agent.pk)
    baseline = _side_effect_baseline()

    missing = delete_json(web, url, {"reason": "x"}, key=_idem())
    assert missing.status_code == 400
    assert missing.json()["error"]["details"]["open_executions"] == 1

    no_reason = delete_json(web, url, {"disposition": "NOW"}, key=_idem())
    assert no_reason.status_code == 400
    assert no_reason.json()["error"]["details"]["field"] == "reason"

    execution.refresh_from_db()
    task.refresh_from_db()
    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 1
    assert execution.ended_at is None
    assert task.status == TaskStatus.IN_PROGRESS.value
    _assert_no_side_effects(baseline)


@pytest.mark.django_db(transaction=True)
@pytest.mark.workflow
def test_membership_remove_wait_revokes_access_but_keeps_the_execution(web, world):
    data, raw_key = create_agent_api(web)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    deadline = execution.execution_deadline_at

    response = delete_json(
        web,
        _member_url(world.project_a.pk, agent.pk),
        {"disposition": "WAIT", "reason": "项目轮换"},
        key=_idem(),
    )

    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert body["disposition"] == "WAIT"
    assert body["reclaimed_execution_ids"] == []
    assert body["agent_id"] == str(agent.pk)

    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 0
    assert project_not_authorised(agent, world.project_a)

    execution.refresh_from_db()
    task.refresh_from_db()
    agent.refresh_from_db()
    assert execution.ended_at is None
    assert execution.execution_deadline_at == deadline
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk
    assert agent.reserved_execution_count == 1
    assert agent.enabled is True
    assert ActivityEvent.objects.filter(action="reclaim").count() == 0
    assert ActivityEvent.objects.filter(action="membership.remove").count() == 1

    # Authorisation is revoked on the very next read: /me no longer lists the project.
    me = _agent_client().get(f"{AGENT}/me", headers={"AGENT_TOKEN": raw_key})
    assert me.status_code == 200
    assert me.json()["data"]["project_ids"] == []


def project_not_authorised(agent: Agent, project: Project) -> bool:
    return project.pk not in accessible_project_ids(agent.pk)


@pytest.mark.workflow
def test_membership_remove_now_reclaims_in_the_same_transaction(web, world):
    data, _key = create_agent_api(web, max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    factories.add_member(world.project_b, agent)
    task_a, execution_a = hold_execution(agent, world.project_a)
    other_task, other_execution = hold_execution(agent, world.project_b)
    version_before = task_a.version

    response = delete_json(
        web,
        _member_url(world.project_a.pk, agent.pk),
        {"disposition": "NOW", "reason": "立即撤权"},
        key=_idem(),
    )

    assert response.status_code == 200, response.content
    assert response.json()["data"]["reclaimed_execution_ids"] == [str(execution_a.pk)]
    assert response.json()["data"]["disposition"] == "NOW"

    _assert_reclaimed(task_a, execution_a, version_before=version_before)
    agent.refresh_from_db()
    assert agent.reserved_execution_count == 1  # only project_a's slot was released
    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 0
    assert ProjectMember.objects.filter(project=world.project_b, agent=agent).count() == 1

    activities, outbox = _reclaim_events(execution_a)
    assert len(activities) == 1
    assert activities[0].actor_type == "web_operator"
    assert activities[0].reason == "立即撤权"
    assert activities[0].project_id == world.project_a.pk
    assert len(outbox) == 1
    assert outbox[0].payload["reason"] == "立即撤权"

    # The untouched project's execution keeps running.
    other_execution.refresh_from_db()
    other_task.refresh_from_db()
    assert other_execution.ended_at is None
    assert other_task.status == TaskStatus.IN_PROGRESS.value
    assert _reclaim_events(other_execution) == ([], [])


@pytest.mark.workflow
@pytest.mark.fault
def test_membership_remove_now_rolls_back_when_the_release_fails(web, world):
    data, _key = create_agent_api(web)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    Agent.objects.filter(pk=agent.pk).update(reserved_execution_count=0)
    baseline = _side_effect_baseline()

    response = delete_json(
        web,
        _member_url(world.project_a.pk, agent.pk),
        {"disposition": "NOW", "reason": "回收"},
        key=_idem(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_BUSY"

    assert ProjectMember.objects.filter(project=world.project_a, agent=agent).count() == 1
    execution.refresh_from_db()
    task.refresh_from_db()
    assert execution.ended_at is None
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.version == 2
    _assert_no_side_effects(baseline)


def test_membership_remove_keeps_the_last_supervisor(web, world):
    supervisor = Agent.objects.get(pk=world.s1.pk)
    project = factories.create_project("唯一主管项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, supervisor)

    refused = delete_json(
        web,
        _member_url(project.pk, supervisor.pk),
        {"disposition": "WAIT", "reason": "移除"},
        key=_idem(),
    )

    assert refused.status_code == 409
    payload = refused.json()
    assert payload["error"]["code"] == "LAST_SUPERVISOR_REQUIRED"
    assert payload["error"]["details"]["project_id"] == str(project.pk)
    assert ProjectMember.objects.filter(project=project, agent=supervisor).count() == 1
    assert ActivityEvent.objects.filter(action="membership.remove").count() == 0

    replacement = Agent.objects.get(pk=world.s2.pk)
    factories.add_member(project, replacement)
    allowed = delete_json(
        web,
        _member_url(project.pk, supervisor.pk),
        {"disposition": "WAIT", "reason": "移除"},
        key=_idem(),
    )
    assert allowed.status_code == 200
    assert ProjectMember.objects.filter(project=project, agent=supervisor).count() == 0
    assert ProjectMember.objects.filter(project=project, agent=replacement).count() == 1

    # A disabled supervisor is not an effective supervisor: the guard ignores them.
    factories.add_member(project, supervisor)
    Agent.objects.filter(pk=replacement.pk).update(enabled=False)
    blocked = delete_json(
        web,
        _member_url(project.pk, supervisor.pk),
        {"disposition": "WAIT", "reason": "移除"},
        key=_idem(),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "LAST_SUPERVISOR_REQUIRED"


def test_membership_remove_unknown_member_or_project_is_404(web, world):
    data, _key = create_agent_api(web)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)

    unknown_agent = delete_json(
        web,
        _member_url(world.project_a.pk, uuid.uuid4()),
        {"disposition": "WAIT"},
        key=_idem(),
    )
    assert unknown_agent.status_code == 404

    outsider = Agent.objects.get(pk=world.u.pk)  # member of project_b only
    not_a_member = delete_json(
        web,
        _member_url(world.project_a.pk, outsider.pk),
        {"disposition": "WAIT"},
        key=_idem(),
    )
    assert not_a_member.status_code == 404
    assert not_a_member.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    unknown_project = delete_json(
        web, _member_url(uuid.uuid4(), agent.pk), {"disposition": "WAIT"}, key=_idem()
    )
    assert unknown_project.status_code == 404

    archived = delete_json(
        web,
        _member_url(world.archived_project.pk, agent.pk),
        {"disposition": "WAIT"},
        key=_idem(),
    )
    assert archived.status_code == 404


# --------------------------------------------------------------------------------------
# Two real concurrent supervisor revocations
# --------------------------------------------------------------------------------------


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_supervisor_revocations_only_one_succeeds(world):
    """Two supervisors revoked at the same time must never leave the project without one."""
    left = Agent.objects.get(pk=world.s1.pk)
    right = Agent.objects.get(pk=world.s2.pk)
    project = factories.create_project("并发撤权项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, left)
    factories.add_member(project, right)
    assert ProjectMember.objects.filter(project=project).count() == 2

    barrier = threading.Barrier(2)
    statuses: list[tuple[str, int, str]] = []
    errors: list[BaseException] = []

    def worker(agent_id, idem_key: str) -> None:
        from django.test import Client

        try:
            barrier.wait(timeout=10)
            client = Client()
            response = client.delete(
                _member_url(project.pk, agent_id),
                data=json.dumps({"disposition": "WAIT", "reason": "并发撤权"}),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY=idem_key,
                HTTP_HOST=ALLOWED_HOST,
            )
            body = response.json()
            code = body["error"]["code"] if "error" in body else "OK"
            statuses.append((str(agent_id), response.status_code, code))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertions below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=worker, args=(left.pk, _idem())),
        threading.Thread(target=worker, args=(right.pk, _idem())),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(statuses) == 2, statuses
    succeeded = [item for item in statuses if item[1] == 200]
    refused = [item for item in statuses if item[1] == 409]
    assert len(succeeded) == 1, statuses
    assert len(refused) == 1, statuses
    assert refused[0][2] == "LAST_SUPERVISOR_REQUIRED"

    remaining = list(
        ProjectMember.objects.filter(project=project)
        .select_related("agent")
        .order_by("created_at")
    )
    assert len(remaining) == 1
    assert remaining[0].agent.enabled is True
    assert remaining[0].agent.role == AgentRole.SUPERVISOR.value
    effective = ProjectMember.objects.filter(
        project=project, agent__enabled=True, agent__role=AgentRole.SUPERVISOR.value
    ).count()
    assert effective == 1
    assert ActivityEvent.objects.filter(action="membership.remove").count() == 1
