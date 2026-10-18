"""T14 acceptance: OPEN/ASSIGNED management and the shared claim-eligibility rule.

Assignment is never a claim: it must not create an execution, reserve a capacity slot or
start the difficulty timer, and it must not overwrite an in-progress execution's ownership.
``domain.read_task_query.claim_eligibility`` is the single rule both the list hint and the
future claim service use, so it is table-tested here directly.
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import OutboxEvent
from apps.projects.models import ProjectMember
from apps.tasks.models import Task, TaskExecution, TaskSubmission
from domain.clock import db_now
from domain.contracts import ClaimMode, ProjectStatus, TaskStatus
from domain.errors import InvalidArgumentError, ResourceNotFoundError
from domain.read_task_query import (
    REASON_AGENT_DISABLED,
    REASON_CAPACITY_EXCEEDED,
    REASON_NOT_A_MEMBER,
    REASON_NOT_DESIGNATED,
    REASON_PROJECT_NOT_ACTIVE,
    REASON_TASK_ARCHIVED,
    REASON_TASK_NOT_TODO,
    claim_eligibility,
)
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.claim]

WEB_TASKS = "/api/web/v1/tasks"


def _key() -> str:
    return uuid.uuid4().hex


def _member_projects(agent) -> set:
    """Live membership set, exactly what the list/claim paths pass into the rule."""
    from domain.permissions import accessible_project_ids

    return accessible_project_ids(agent.pk)


def _post(client, url: str, payload: dict, key: str | None = None):
    return client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Idempotency-Key": key or _key()},
    )


def _create_task(client, world, **overrides) -> dict:
    payload = {
        "project_id": str(world.project_a.pk),
        "title": "指派任务",
        "description": "要求",
        "acceptance_criteria": "验收",
        "difficulty": 2,
        "claim_mode": ClaimMode.OPEN.value,
    }
    payload.update(overrides)
    response = _post(client, WEB_TASKS, payload)
    assert response.status_code == 200, response.content
    return response.json()["data"]


def _publish(client, task_id, version):
    return _post(client, f"{WEB_TASKS}/{task_id}/publish", {"expected_version": version})


def _assign(client, task_id, version, **overrides):
    payload = {"expected_version": version}
    payload.update(overrides)
    return _post(client, f"{WEB_TASKS}/{task_id}/assign", payload)


# --------------------------------------------------------------------------------------
# WebAPI assignment
# --------------------------------------------------------------------------------------


def test_assign_switches_mode_without_claiming_reserving_or_timing(client, world):
    prepared = _create_task(client, world)
    task_id = prepared["id"]

    assigned = _assign(
        client,
        task_id,
        prepared["version"],
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w1.pk),
    )
    assert assigned.status_code == 200, assigned.content
    data = assigned.json()["data"]
    assert data["claim_mode"] == ClaimMode.ASSIGNED.value
    assert data["designated_agent_id"] == str(world.w1.pk)
    assert data["version"] == prepared["version"] + 1
    assert data["status"] == TaskStatus.PREPARING.value

    task = Task.objects.get(pk=task_id)
    assert task.claim_mode == ClaimMode.ASSIGNED.value
    assert task.designated_agent_id == world.w1.pk
    assert task.assignee_agent_id is None
    assert task.active_execution_id is None
    assert task.claimed_at is None

    # No execution, no capacity slot, no timer.
    assert TaskExecution.objects.count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    assert Agent.objects.get(pk=world.w1.pk).available_capacity == 1

    event = ActivityEvent.objects.get(task=task, action="assigned")
    assert event.before["claim_mode"] == ClaimMode.OPEN.value
    assert event.after["claim_mode"] == ClaimMode.ASSIGNED.value
    assert event.after["designated_agent_id"] == str(world.w1.pk)
    assert OutboxEvent.objects.filter(event_type="task.assigned", entity_id=task.pk).count() == 1

    # Switching back to OPEN clears the designation and never hands the task to anyone.
    opened = _assign(client, task_id, task.version, claim_mode=ClaimMode.OPEN.value)
    assert opened.status_code == 200, opened.content
    data = opened.json()["data"]
    assert data["claim_mode"] == ClaimMode.OPEN.value
    assert data["designated_agent_id"] is None
    assert data["version"] == prepared["version"] + 2
    assert Task.objects.get(pk=task_id).assignee_agent_id is None
    assert TaskExecution.objects.count() == 0


def test_assign_allowed_from_preparing_and_todo_only(client, world):
    prepared = _create_task(client, world)
    todo_mode = _assign(
        client,
        prepared["id"],
        prepared["version"],
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w2.pk),
    )
    assert todo_mode.status_code == 200
    published = _publish(client, prepared["id"], prepared["version"] + 1)
    assert published.status_code == 200

    from_todo = _assign(
        client,
        prepared["id"],
        published.json()["data"]["version"],
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w1.pk),
    )
    assert from_todo.status_code == 200, from_todo.content
    assert from_todo.json()["data"]["designated_agent_id"] == str(world.w1.pk)


def test_assign_rejects_invalid_designations(client, world):
    prepared = _create_task(client, world)
    task_id = prepared["id"]
    version = prepared["version"]

    open_with_agent = _assign(
        client,
        task_id,
        version,
        claim_mode=ClaimMode.OPEN.value,
        designated_agent_id=str(world.w1.pk),
    )
    assert open_with_agent.status_code == 400, open_with_agent.content
    assert open_with_agent.json()["error"]["code"] == "INVALID_ARGUMENT"

    missing_mode = _assign(client, task_id, version, designated_agent_id=str(world.w1.pk))
    assert missing_mode.status_code == 400

    assigned_without_agent = _assign(
        client, task_id, version, claim_mode=ClaimMode.ASSIGNED.value, designated_agent_id=None
    )
    assert assigned_without_agent.status_code == 400

    unknown_agent = _assign(
        client,
        task_id,
        version,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(uuid.uuid4()),
    )
    assert unknown_agent.status_code == 404, unknown_agent.content
    assert unknown_agent.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    Agent.objects.filter(pk=world.w2.pk).update(enabled=False)
    disabled = _assign(
        client,
        task_id,
        version,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w2.pk),
    )
    assert disabled.status_code == 400, disabled.content
    assert disabled.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert disabled.json()["error"]["details"]["agent_id"] == str(world.w2.pk)

    # U is a member of project B only.
    non_member = _assign(
        client,
        task_id,
        version,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.u.pk),
    )
    assert non_member.status_code == 400, non_member.content
    assert non_member.json()["error"]["code"] == "INVALID_ARGUMENT"

    task = Task.objects.get(pk=task_id)
    assert task.claim_mode == ClaimMode.OPEN.value
    assert task.designated_agent_id is None
    assert task.version == version
    assert ActivityEvent.objects.filter(task=task, action="assigned").count() == 0


def test_assign_in_progress_is_refused_until_reclaimed(client, world):
    agent = world.w1
    agent.reserved_execution_count = 1
    agent.save(update_fields=["reserved_execution_count"])
    task = factories.create_task(world.project_a, title="进行中", difficulty=2)
    now = db_now()
    execution = factories.create_execution(task, agent, claimed_at=now)
    task.status = TaskStatus.IN_PROGRESS.value
    task.assignee_agent = agent
    task.active_execution = execution
    task.claimed_at = now
    task.version = task.version + 1
    task.save(
        update_fields=["status", "assignee_agent", "active_execution", "claimed_at", "version"]
    )

    response = _assign(
        client,
        task.pk,
        task.version,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w2.pk),
    )
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"

    fresh = Task.objects.get(pk=task.pk)
    assert fresh.assignee_agent_id == agent.pk
    assert fresh.active_execution_id == execution.pk
    assert fresh.designated_agent_id is None
    assert fresh.version == task.version
    assert Agent.objects.get(pk=agent.pk).reserved_execution_count == 1


def test_assign_requires_the_current_version(client, world):
    prepared = _create_task(client, world)
    stale = _assign(
        client,
        prepared["id"],
        prepared["version"] + 5,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w1.pk),
    )
    assert stale.status_code == 409, stale.content
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert Task.objects.get(pk=prepared["id"]).designated_agent_id is None


def test_create_validates_the_designated_agent(client, world):
    ok = _create_task(
        client,
        world,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w1.pk),
    )
    assert ok["designated_agent_id"] == str(world.w1.pk)
    assert ok["claim_mode"] == ClaimMode.ASSIGNED.value

    # U is a member of project B only, so it can never be designated in project A.
    outsider = _post(
        client,
        WEB_TASKS,
        {
            "project_id": str(world.project_a.pk),
            "title": "指定非成员",
            "difficulty": 2,
            "claim_mode": ClaimMode.ASSIGNED.value,
            "designated_agent_id": str(world.u.pk),
        },
    )
    assert outsider.status_code == 400, outsider.content
    assert outsider.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert Task.objects.count() == 1


@pytest.fixture
def agent_api():
    from fastapi.testclient import TestClient

    from agentapi.main import app

    return TestClient(app)


@pytest.mark.django_db(transaction=True)
def test_agentapi_supervisor_can_assign_and_worker_cannot(world, agent_api):
    created = agent_api.post(
        "/api/agent/v1/tasks",
        json={
            "project_id": str(world.project_a.pk),
            "title": "Agent 指派",
            "description": "要求",
            "acceptance_criteria": "验收",
            "difficulty": 1,
            "claim_mode": ClaimMode.OPEN.value,
        },
        headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": _key()},
    )
    assert created.status_code == 200, created.content
    task_id = created.json()["data"]["id"]

    assigned = agent_api.post(
        f"/api/agent/v1/tasks/{task_id}/assign",
        json={
            "expected_version": 1,
            "claim_mode": ClaimMode.ASSIGNED.value,
            "designated_agent_id": str(world.w1.pk),
        },
        headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": _key()},
    )
    assert assigned.status_code == 200, assigned.content
    assert assigned.json()["data"]["designated_agent_id"] == str(world.w1.pk)

    refused = agent_api.post(
        f"/api/agent/v1/tasks/{task_id}/assign",
        json={"expected_version": 2, "claim_mode": ClaimMode.OPEN.value},
        headers={"AGENT_TOKEN": world.w1_key, "Idempotency-Key": _key()},
    )
    assert refused.status_code == 403, refused.content
    assert refused.json()["error"]["code"] == "ROLE_FORBIDDEN"


# --------------------------------------------------------------------------------------
# claim_eligibility: the shared rule
# --------------------------------------------------------------------------------------


def test_claim_eligibility_signature_is_stable_for_the_claim_service():
    signature = inspect.signature(claim_eligibility)
    assert list(signature.parameters) == [
        "task",
        "agent",
        "member_project_ids",
        "available_capacity",
        "project_status",
    ]
    assert signature.parameters["member_project_ids"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["available_capacity"].kind is inspect.Parameter.KEYWORD_ONLY


def test_claim_eligibility_matrix(db, world):
    task_project = world.project_a
    members = {task_project.pk}
    capacity = 1

    def eligibility(task, agent, *, members_override=None, capacity_override=capacity):
        return claim_eligibility(
            task,
            agent,
            member_project_ids=members if members_override is None else members_override,
            available_capacity=capacity_override,
        )

    open_todo = factories.create_task(task_project, title="开放待办")
    assert eligibility(open_todo, world.w1) == {
        "eligible": True,
        "reason": None,
        "message": "",
    }

    assigned = factories.create_task(
        task_project,
        title="指定待办",
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent=world.w2,
    )
    assert eligibility(assigned, world.w2)["eligible"] is True
    assert eligibility(assigned, world.w1)["reason"] == REASON_NOT_DESIGNATED

    preparing = factories.create_task(task_project, title="准备中", status=TaskStatus.PREPARING)
    assert eligibility(preparing, world.w1)["reason"] == REASON_TASK_NOT_TODO

    archived = factories.create_task(task_project, title="已归档", is_archived=True)
    assert eligibility(archived, world.w1)["reason"] == REASON_TASK_ARCHIVED

    in_review = _in_review_task(world)
    assert eligibility(in_review, world.w1)["reason"] == REASON_TASK_NOT_TODO

    archived_project = factories.create_project("归档项目", status=ProjectStatus.ARCHIVED)
    factories.add_member(archived_project, world.w1)
    in_archived = factories.create_task(archived_project, title="归档项目待办")
    result = eligibility(in_archived, world.w1, members_override={archived_project.pk})
    assert result["reason"] == REASON_PROJECT_NOT_ACTIVE

    disabled = factories.create_agent("DISABLED", max_concurrency=1)[0]
    Agent.objects.filter(pk=disabled.pk).update(enabled=False)
    factories.add_member(task_project, disabled)
    disabled.refresh_from_db()
    assert eligibility(open_todo, disabled)["reason"] == REASON_AGENT_DISABLED
    # A disabled non-member is reported as disabled first: the rule order is fixed.
    assert eligibility(open_todo, disabled, members_override=set())["reason"] == REASON_AGENT_DISABLED

    assert eligibility(open_todo, world.u, members_override=set())["reason"] == REASON_NOT_A_MEMBER
    assert eligibility(open_todo, world.w1, capacity_override=0)["reason"] == REASON_CAPACITY_EXCEEDED
    # Capacity is unknown -> the hint is not gated (the claim service always passes a number).
    assert eligibility(open_todo, world.w1, capacity_override=None)["eligible"] is True


def _in_review_task(world):
    agent = world.w1
    task = factories.create_task(world.project_a, title="待审核", difficulty=2)
    now = db_now()
    execution = factories.create_execution(task, agent, claimed_at=now)
    submission = TaskSubmission.objects.create(
        execution=execution,
        task=task,
        summary="结果",
        content="内容",
        self_check=[],
        artifact_refs=[],
        links=[],
        requirements_version=task.requirements_version,
        submitted_by_agent=agent,
        submitted_at=now,
    )
    task.status = TaskStatus.IN_REVIEW.value
    task.assignee_agent = agent
    task.current_submission = submission
    task.active_execution = None
    task.version = task.version + 1
    task.save(
        update_fields=[
            "status",
            "assignee_agent",
            "current_submission",
            "active_execution",
            "version",
        ]
    )
    return task


def test_levels_and_role_never_gate_eligibility(db, world):
    tasks = [
        factories.create_task(world.project_a, title=f"L{difficulty}", difficulty=difficulty)
        for difficulty in (1, 2, 3, 4, 5)
    ]
    members = {world.project_a.pk}
    for agent in (world.s1, world.w1):
        for task in tasks:
            result = claim_eligibility(
                task, agent, member_project_ids=members, available_capacity=1
            )
            assert result["eligible"] is True, (agent.name, task.difficulty, result)


def test_designation_change_switches_who_is_eligible(db, world):
    task = factories.create_task(
        world.project_a,
        title="指定 B",
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent=world.w2,
    )
    members = {world.project_a.pk}

    def who(agent):
        return claim_eligibility(
            task, agent, member_project_ids=members, available_capacity=1
        )["eligible"]

    assert who(world.w2) is True
    assert who(world.w1) is False

    # The supervisor opens the task: every enabled member becomes eligible, and nobody was
    # automatically granted the execution.
    task.claim_mode = ClaimMode.OPEN.value
    task.designated_agent = None
    task.save(update_fields=["claim_mode", "designated_agent"])
    assert who(world.w1) is True
    assert who(world.s2) is True
    assert Task.objects.get(pk=task.pk).assignee_agent_id is None
    assert TaskExecution.objects.count() == 0


def test_revoked_membership_is_reported_and_does_not_auto_reassign(client, world):
    prepared = _create_task(
        client,
        world,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w2.pk),
    )
    published = _publish(client, prepared["id"], prepared["version"])
    assert published.status_code == 200, published.content
    task = Task.objects.get(pk=prepared["id"])
    ProjectMember.objects.filter(project=world.project_a, agent=world.w2).delete()

    revoked = claim_eligibility(
        task,
        world.w2,
        member_project_ids=_member_projects(world.w2),
        available_capacity=1,
    )
    assert revoked["eligible"] is False
    assert revoked["reason"] == REASON_NOT_A_MEMBER

    # The supervisor may open the task or re-designate; nothing happens automatically.
    opened = _assign(client, task.pk, task.version, claim_mode=ClaimMode.OPEN.value)
    assert opened.status_code == 200, opened.content
    fresh = Task.objects.get(pk=task.pk)
    assert fresh.designated_agent_id is None
    assert fresh.assignee_agent_id is None
    assert claim_eligibility(
        fresh, world.w1, member_project_ids=_member_projects(world.w1), available_capacity=1
    )["eligible"] is True
    assert claim_eligibility(
        fresh, world.w2, member_project_ids=_member_projects(world.w2), available_capacity=1
    )["reason"] == REASON_NOT_A_MEMBER
    assert TaskExecution.objects.count() == 0


def test_claim_eligibility_is_read_only(db, world):
    task = factories.create_task(world.project_a, title="只读检查")
    before = list(Task.objects.values_list("pk", "status", "version", "claim_mode"))
    claim_eligibility(
        task, world.w1, member_project_ids={world.project_a.pk}, available_capacity=1
    )
    claim_eligibility(task, world.u, member_project_ids=set(), available_capacity=0)
    assert list(Task.objects.values_list("pk", "status", "version", "claim_mode")) == before
    assert TaskExecution.objects.count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0