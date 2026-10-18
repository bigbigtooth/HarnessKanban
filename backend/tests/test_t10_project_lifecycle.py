"""T10 acceptance: project lifecycle (activate/complete/archive/restore) and its gates.

Every case asserts real database rows, not just response codes: failure paths must leave
the project version/status/archived_from_status, member rows, task rows and the event
counters untouched, and each real action appends exactly one ActivityEvent + OutboxEvent.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client

from apps.audit.models import ActivityEvent
from apps.jobs.models import IdempotencyRecord, OutboxEvent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskSubmission
from domain.clock import db_now
from domain.contracts import AgentRole, EndReason, ProjectStatus, TaskStatus
from domain.errors import ProjectNotActiveError, ResourceNotFoundError
from domain.permissions import require_project_active, require_project_editable
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.projects, pytest.mark.workflow]

BASE = "/api/web/v1"


@pytest.fixture
def web_client(db):
    """Local WebAPI client with an allowed Host (the boundary middleware rejects testserver)."""
    return Client(SERVER_NAME="127.0.0.1")


def post_action(client, project, action: str, *, key: str | None = None, payload: dict | None = None):
    body = dict(payload or {})
    if "expected_version" not in body:
        # Always use the committed version: an earlier action in the same test may have
        # incremented it and the in-memory object is then stale.
        body["expected_version"] = Project.objects.get(pk=project.pk).version
    return client.post(
        f"{BASE}/projects/{project.pk}/{action}",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def make_in_progress_task(project, agent, *, title: str = "进行中任务", archived: bool = False):
    task = factories.create_task(project, title=title, status=TaskStatus.TODO)
    now = db_now()
    execution = factories.create_execution(task, agent, claimed_at=now)
    task.status = TaskStatus.IN_PROGRESS.value
    task.assignee_agent = agent
    task.active_execution = execution
    task.claimed_at = now
    task.is_archived = archived
    task.version = task.version + 1
    task.save(
        update_fields=[
            "status",
            "assignee_agent",
            "active_execution",
            "claimed_at",
            "is_archived",
            "version",
        ]
    )
    return task, execution


def make_in_review_task(project, agent, *, title: str = "待审核任务"):
    task, execution = make_in_progress_task(project, agent, title=title)
    now = db_now()
    submission = TaskSubmission.objects.create(
        execution=execution,
        task=task,
        summary="结果摘要",
        content="结果正文",
        requirements_version=task.requirements_version,
        submitted_by_agent=agent,
        submitted_at=now,
    )
    execution.ended_at = now
    execution.end_reason = EndReason.SUBMIT.value
    execution.save(update_fields=["ended_at", "end_reason"])
    task.status = TaskStatus.IN_REVIEW.value
    task.active_execution = None
    task.current_submission = submission
    task.submitted_at = now
    task.version = task.version + 1
    task.save(
        update_fields=["status", "active_execution", "current_submission", "submitted_at", "version"]
    )
    return task, submission


def make_done_task(project, agent, *, title: str = "已完成任务"):
    """DONE requires a current submission, so it is built through the real row shapes."""
    task, _submission = make_in_review_task(project, agent, title=title)
    task.status = TaskStatus.DONE.value
    task.completed_at = db_now()
    task.version = task.version + 1
    task.save(update_fields=["status", "completed_at", "version"])
    return task


def project_state(project) -> dict:
    project.refresh_from_db()
    return {
        "status": project.status,
        "version": project.version,
        "archived_from_status": project.archived_from_status,
        "content_version": project.content_version,
    }


def task_rows(project) -> list[dict]:
    return [
        {
            "id": str(task.pk),
            "status": task.status,
            "version": task.version,
            "is_archived": task.is_archived,
            "active_execution_id": None if task.active_execution_id is None else str(task.active_execution_id),
            "assignee_agent_id": None if task.assignee_agent_id is None else str(task.assignee_agent_id),
            "claimed_at": task.claimed_at,
            "submitted_at": task.submitted_at,
            "number": task.number,
        }
        for task in Task.objects.filter(project=project).order_by("number")
    ]


def member_rows(project) -> list[str]:
    return sorted(
        str(agent_id)
        for agent_id in ProjectMember.objects.filter(project=project).values_list("agent_id", flat=True)
    )


def event_counts() -> dict[str, int]:
    return {
        "activity": ActivityEvent.objects.count(),
        "outbox": OutboxEvent.objects.count(),
        "receipts": IdempotencyRecord.objects.count(),
    }


def full_snapshot(project) -> dict:
    return {
        "project": project_state(project),
        "tasks": task_rows(project),
        "members": member_rows(project),
        "events": event_counts(),
    }


# --------------------------------------------------------------------------------------
# activate
# --------------------------------------------------------------------------------------


def test_activate_draft_requires_enabled_supervisor_and_changes_nothing(world, web_client):
    project = factories.create_project("草稿项目", status=ProjectStatus.DRAFT)
    factories.add_member(project, world.w1)
    disabled_supervisor, _ = factories.create_agent(
        factories.unique_name("禁用主管"), AgentRole.SUPERVISOR, enabled=False
    )
    factories.add_member(project, disabled_supervisor)
    before = full_snapshot(project)

    response = post_action(web_client, project, "activate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_SUPERVISOR_REQUIRED"
    assert full_snapshot(project) == before


def test_activate_draft_with_enabled_supervisor_transitions_and_records_one_event(world, web_client):
    project = factories.create_project("可启用项目", status=ProjectStatus.DRAFT)
    factories.add_member(project, world.s1)
    factories.add_member(project, world.w1)
    before_version = project.version

    response = post_action(web_client, project, "activate")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["status"] == ProjectStatus.ACTIVE.value
    project.refresh_from_db()
    assert project.status == ProjectStatus.ACTIVE.value
    assert project.version == before_version + 1
    assert project.archived_from_status is None

    activities = list(ActivityEvent.objects.filter(project=project))
    outbox = list(OutboxEvent.objects.filter(entity_id=project.pk))
    assert [event.action for event in activities] == ["project.activate"]
    assert activities[0].actor_type == "web_operator"
    assert [event.event_type for event in outbox] == ["project.activated"]
    records = IdempotencyRecord.objects.filter(resource_id=project.pk)
    assert [record.state for record in records] == ["DONE"]


def test_activate_replay_returns_receipt_without_second_event_or_version_bump(world, web_client):
    project = factories.create_project("重放项目", status=ProjectStatus.DRAFT)
    factories.add_member(project, world.s1)
    key = uuid.uuid4().hex
    # A replay must send the *same* business body (same request digest).
    original_payload = {"expected_version": project.version}

    first = post_action(web_client, project, "activate", key=key, payload=original_payload)
    assert first.status_code == 200
    version_after_first = project_state(project)["version"]
    events_after_first = event_counts()

    replay = post_action(web_client, project, "activate", key=key, payload=original_payload)

    assert replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    assert project_state(project)["version"] == version_after_first
    assert event_counts() == events_after_first


def test_activate_rejects_stale_expected_version(world, web_client):
    project = factories.create_project("版本冲突项目", status=ProjectStatus.DRAFT)
    factories.add_member(project, world.s1)
    before = full_snapshot(project)

    response = post_action(
        web_client, project, "activate", payload={"expected_version": project.version + 5}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert full_snapshot(project) == before


def test_activate_completed_project_returns_to_active(world, web_client):
    project = factories.create_project("结项项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, world.s1)
    make_done_task(project, world.s1)
    completed = post_action(web_client, project, "complete")
    assert completed.status_code == 200
    assert project_state(project)["status"] == ProjectStatus.COMPLETED.value

    response = post_action(web_client, project, "activate")

    assert response.status_code == 200
    assert project_state(project)["status"] == ProjectStatus.ACTIVE.value


def test_activate_archived_project_is_rejected_and_needs_restore(world, web_client):
    project = factories.create_project(
        "归档项目", status=ProjectStatus.ARCHIVED, archived_from_status=ProjectStatus.ACTIVE.value
    )
    factories.add_member(project, world.s1)
    before = full_snapshot(project)

    response = post_action(web_client, project, "activate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    assert full_snapshot(project) == before


# --------------------------------------------------------------------------------------
# complete
# --------------------------------------------------------------------------------------


def test_complete_requires_at_least_one_non_archived_task(world, web_client):
    project = factories.create_project("空项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, world.s1)
    before = full_snapshot(project)

    response = post_action(web_client, project, "complete")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    assert response.json()["error"]["details"]["non_archived_tasks"] == 0
    assert full_snapshot(project) == before


def test_complete_blocked_by_preparing_and_todo_tasks(world, web_client):
    project = factories.create_project("未完成项目", status=ProjectStatus.ACTIVE)
    factories.create_task(project, title="准备中", status=TaskStatus.PREPARING)
    factories.create_task(project, title="待办", status=TaskStatus.TODO)
    make_done_task(project, world.s1, title="完成")
    before = full_snapshot(project)

    response = post_action(web_client, project, "complete")

    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details["statuses"] == {"PREPARING": 1, "TODO": 1}
    assert full_snapshot(project) == before


def test_complete_blocked_by_in_progress_and_in_review_tasks(world, web_client):
    project = factories.create_project("执行中项目", status=ProjectStatus.ACTIVE)
    make_in_progress_task(project, world.w1)
    make_in_review_task(project, world.w2)
    make_done_task(project, world.s1, title="完成")
    before = full_snapshot(project)

    response = post_action(web_client, project, "complete")

    assert response.status_code == 409
    assert response.json()["error"]["details"]["statuses"] == {"IN_PROGRESS": 1, "IN_REVIEW": 1}
    assert full_snapshot(project) == before


def test_complete_succeeds_when_every_non_archived_task_is_done(world, web_client):
    project = factories.create_project("可结项项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, world.s1)
    make_done_task(project, world.s1, title="完成一")
    make_done_task(project, world.s2, title="完成二")
    factories.create_task(project, title="已归档待办", status=TaskStatus.TODO, is_archived=True)
    make_in_progress_task(project, world.w1, title="已归档进行中", archived=True)
    tasks_before = task_rows(project)

    response = post_action(web_client, project, "complete")

    assert response.status_code == 200
    assert project_state(project)["status"] == ProjectStatus.COMPLETED.value
    # Lifecycle never rewrites a task row.
    assert task_rows(project) == tasks_before
    assert [event.action for event in ActivityEvent.objects.filter(project=project)] == [
        "project.complete"
    ]
    assert [event.event_type for event in OutboxEvent.objects.all()] == ["project.completed"]


# --------------------------------------------------------------------------------------
# archive / restore
# --------------------------------------------------------------------------------------


def test_archive_blocked_by_in_progress_or_in_review_tasks(world, web_client):
    project = factories.create_project("执行中项目", status=ProjectStatus.ACTIVE)
    make_in_progress_task(project, world.w1)
    before = full_snapshot(project)

    response = post_action(web_client, project, "archive", payload={"reason": "暂停"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    assert response.json()["error"]["details"]["blocking_tasks"] == 1
    assert full_snapshot(project) == before

    in_review_project = factories.create_project("待审核项目", status=ProjectStatus.ACTIVE)
    make_in_review_task(in_review_project, world.w2)
    in_review_before = full_snapshot(in_review_project)

    in_review_response = post_action(
        web_client, in_review_project, "archive", payload={"reason": "暂停"}
    )

    assert in_review_response.status_code == 409
    assert full_snapshot(in_review_project) == in_review_before


def test_archive_requires_reason(world, web_client):
    project = factories.create_project("缺原因项目", status=ProjectStatus.ACTIVE)
    before = full_snapshot(project)

    blank = post_action(web_client, project, "archive", payload={"reason": "   "})
    missing = post_action(web_client, project, "archive")

    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert missing.status_code == 400
    assert full_snapshot(project) == before


def test_archive_saves_previous_status_and_restore_returns_to_it(world, web_client):
    project = factories.create_project("归档恢复项目", status=ProjectStatus.ACTIVE)
    factories.create_task(project, title="待办任务", status=TaskStatus.TODO)
    tasks_before = task_rows(project)

    archived = post_action(web_client, project, "archive", payload={"reason": "临时冻结"})

    assert archived.status_code == 200
    state = project_state(project)
    assert state["status"] == ProjectStatus.ARCHIVED.value
    assert state["archived_from_status"] == ProjectStatus.ACTIVE.value
    assert task_rows(project) == tasks_before
    assert [event.action for event in ActivityEvent.objects.filter(project=project)] == [
        "project.archive"
    ]
    assert ActivityEvent.objects.get(project=project).reason == "临时冻结"

    # Archived projects are read-only for every edit/active gate.
    project.refresh_from_db()
    with pytest.raises(ResourceNotFoundError):
        require_project_editable(project)
    with pytest.raises(ProjectNotActiveError):
        require_project_active(project)

    restored = post_action(web_client, project, "restore")

    assert restored.status_code == 200
    state = project_state(project)
    assert state["status"] == ProjectStatus.ACTIVE.value
    assert state["archived_from_status"] is None
    project.refresh_from_db()
    require_project_editable(project)  # editable again
    assert task_rows(project) == tasks_before
    assert [event.action for event in ActivityEvent.objects.filter(project=project)] == [
        "project.archive",
        "project.restore",
    ]
    assert sorted(
        event.event_type for event in OutboxEvent.objects.filter(entity_id=project.pk)
    ) == ["project.archived", "project.restored"]


def test_restore_without_saved_status_falls_back_to_draft(world, web_client):
    project = factories.create_project("无来源状态项目", status=ProjectStatus.ARCHIVED)

    response = post_action(web_client, project, "restore")

    assert response.status_code == 200
    assert project_state(project)["status"] == ProjectStatus.DRAFT.value


def test_restore_non_archived_project_is_rejected(world, web_client):
    project = factories.create_project("进行中项目", status=ProjectStatus.ACTIVE)
    before = full_snapshot(project)

    response = post_action(web_client, project, "restore")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    assert full_snapshot(project) == before


def test_double_archive_is_rejected_without_overwriting_archived_from_status(world, web_client):
    project = factories.create_project("重复归档项目", status=ProjectStatus.ACTIVE)
    assert post_action(web_client, project, "archive", payload={"reason": "第一次"}).status_code == 200
    before = full_snapshot(project)

    response = post_action(web_client, project, "archive", payload={"reason": "第二次"})

    assert response.status_code == 409
    assert project_state(project)["archived_from_status"] == ProjectStatus.ACTIVE.value
    assert full_snapshot(project) == before


# --------------------------------------------------------------------------------------
# request guards and cross-cutting invariants
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["activate", "complete", "archive", "restore"])
def test_lifecycle_write_requires_idempotency_key(world, web_client, action):
    project = factories.create_project("幂等键项目", status=ProjectStatus.DRAFT)
    before = full_snapshot(project)

    response = web_client.post(
        f"{BASE}/projects/{project.pk}/{action}",
        data=json.dumps({"expected_version": project.version, "reason": "原因"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert full_snapshot(project) == before


def test_reusing_a_key_with_different_content_is_rejected(world, web_client):
    project = factories.create_project("键复用项目", status=ProjectStatus.ACTIVE)
    key = uuid.uuid4().hex
    assert post_action(web_client, project, "archive", key=key, payload={"reason": "原因一"}).status_code == 200
    before = full_snapshot(project)

    response = post_action(web_client, project, "archive", key=key, payload={"reason": "原因二"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert full_snapshot(project) == before


def test_unknown_project_and_malformed_id_are_not_found_or_invalid(world, web_client):
    missing = web_client.post(
        f"{BASE}/projects/{uuid.uuid4()}/activate",
        data=json.dumps({"expected_version": 1}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    malformed = web_client.post(
        f"{BASE}/projects/not-a-uuid/activate",
        data=json.dumps({"expected_version": 1}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_expected_version_is_rejected(world, web_client):
    project = factories.create_project("缺版本项目", status=ProjectStatus.DRAFT)
    before = full_snapshot(project)

    response = web_client.post(
        f"{BASE}/projects/{project.pk}/activate",
        data=json.dumps({}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert full_snapshot(project) == before


def test_lifecycle_routes_only_answer_their_action_method(world, web_client):
    """GET/PUT on an action route is a protocol error, never a silent state change."""
    project = factories.create_project("方法约束项目", status=ProjectStatus.DRAFT)
    before = full_snapshot(project)

    get_response = web_client.get(f"{BASE}/projects/{project.pk}/activate")
    put_response = web_client.put(
        f"{BASE}/projects/{project.pk}/activate",
        data=json.dumps({"expected_version": 1}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )

    assert get_response.status_code == 405
    assert get_response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert put_response.status_code == 405
    assert full_snapshot(project) == before


def test_lifecycle_gates_follow_project_status(world):
    """The shared gates other write paths use: archived is never editable or active."""
    draft = factories.create_project("草稿", status=ProjectStatus.DRAFT)
    completed = factories.create_project("已结项", status=ProjectStatus.COMPLETED)
    archived = factories.create_project("已归档", status=ProjectStatus.ARCHIVED)

    require_project_editable(draft)
    require_project_editable(completed)
    require_project_editable(factories.create_project("进行中", status=ProjectStatus.ACTIVE))

    for project in (draft, completed, archived):
        with pytest.raises(ProjectNotActiveError):
            require_project_active(project)
    with pytest.raises(ResourceNotFoundError):
        require_project_editable(archived)


def test_failed_lifecycle_actions_leave_every_row_untouched(world, web_client):
    """A rejected action must roll back project, member and event state together."""
    project = factories.create_project("失败快照项目", status=ProjectStatus.ACTIVE)
    factories.add_member(project, world.s1)
    factories.add_member(project, world.w1)
    make_in_progress_task(project, world.w1)
    before = full_snapshot(project)

    responses = [
        post_action(web_client, project, "archive", payload={"reason": "会被拒绝"}),
        post_action(web_client, project, "complete"),
        post_action(web_client, project, "activate"),
        post_action(web_client, project, "restore"),
        post_action(web_client, project, "archive", payload={"reason": "陈旧版本", "expected_version": project.version + 3}),
    ]

    assert [response.status_code for response in responses] == [409, 409, 409, 409, 409]
    assert full_snapshot(project) == before


def test_archived_project_status_is_not_written_by_lifecycle_actions(world, web_client):
    """Restore is the only path out of ARCHIVED; it never rewrites other rows."""
    project = factories.create_project(
        "归档只读项目",
        status=ProjectStatus.ARCHIVED,
        archived_from_status=ProjectStatus.COMPLETED.value,
    )
    factories.add_member(project, world.s1)
    archived_task = make_done_task(project, world.s1, title="已归档任务")
    archived_task.is_archived = True
    archived_task.save(update_fields=["is_archived"])
    tasks_before = task_rows(project)
    members_before = member_rows(project)

    assert post_action(web_client, project, "activate").status_code == 409
    assert post_action(web_client, project, "complete").status_code == 409
    assert task_rows(project) == tasks_before

    response = post_action(web_client, project, "restore")

    assert response.status_code == 200
    assert project_state(project)["status"] == ProjectStatus.COMPLETED.value
    assert task_rows(project) == tasks_before
    assert member_rows(project) == members_before


def test_project_context_version_rows_are_unchanged_by_lifecycle(world, web_client):
    """Lifecycle actions must not fabricate or rewrite context versions."""
    from apps.projects.models import ProjectContextVersion

    project = factories.create_project("上下文项目", status=ProjectStatus.ACTIVE, background="背景")
    contexts_before = list(
        ProjectContextVersion.objects.filter(project=project).values_list(
            "number", "background_snapshot"
        )
    )

    assert post_action(web_client, project, "archive", payload={"reason": "冻结"}).status_code == 200
    assert post_action(web_client, project, "restore").status_code == 200

    contexts_after = list(
        ProjectContextVersion.objects.filter(project=project).values_list(
            "number", "background_snapshot"
        )
    )
    assert contexts_after == contexts_before
    assert Project.objects.get(pk=project.pk).content_version == 1
