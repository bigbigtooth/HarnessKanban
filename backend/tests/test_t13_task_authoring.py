"""T13 acceptance: task authoring, publish/withdraw, edit restrictions and history.

Every test asserts database rows (status/version/requirements_version/published_at,
ActivityEvent/OutboxEvent, absence of executions), not only HTTP status codes.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from apps.audit.models import ActivityEvent
from apps.files.models import AttachmentLink, FileAsset
from apps.identity.models import Agent
from apps.jobs.models import OutboxEvent
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now
from domain.contracts import ClaimMode, ProjectStatus, TaskStatus
from domain.errors import RetryTransaction
from domain import task_service
from domain.transactions import run_write_service
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.workflow]

WEB_TASKS = "/api/web/v1/tasks"


def _key() -> str:
    return uuid.uuid4().hex


def _payload(world, **overrides) -> dict:
    payload = {
        "project_id": str(world.project_a.pk),
        "title": "编制中的任务",
        "description": "实现任务编制流程",
        "acceptance_criteria": "创建不发布；补齐要求后才能进入待办",
        "difficulty": 2,
        "claim_mode": ClaimMode.OPEN.value,
    }
    payload.update(overrides)
    return payload


def _post(client, url: str, payload: dict, key: str | None = None, **kwargs):
    headers = {"Idempotency-Key": key or _key()}
    headers.update(kwargs.pop("headers", {}))
    return client.post(
        url, data=json.dumps(payload), content_type="application/json", headers=headers, **kwargs
    )


def _patch(client, url: str, payload: dict, key: str | None = None):
    headers = {"Idempotency-Key": key or _key()}
    return client.patch(
        url, data=json.dumps(payload), content_type="application/json", headers=headers
    )


def _create(client, world, **overrides) -> dict:
    response = _post(client, WEB_TASKS, _payload(world, **overrides))
    assert response.status_code == 200, response.content
    return response.json()["data"]


def _publish(client, task_id, version: int, key: str | None = None):
    return _post(client, f"{WEB_TASKS}/{task_id}/publish", {"expected_version": version}, key=key)


def _in_progress_task(world, agent=None):
    """A claimed task with a matching reserved capacity slot (no claim service involved)."""
    agent = agent or world.w1
    agent.reserved_execution_count = 1
    agent.save(update_fields=["reserved_execution_count"])
    task = factories.create_task(world.project_a, title="进行中任务", difficulty=2)
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
    return task, execution


# --------------------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------------------


def test_create_starts_in_preparing_with_version_one_and_writes_history(client, world):
    data = _create(client, world)

    assert data["status"] == TaskStatus.PREPARING.value
    assert data["version"] == 1
    assert data["requirements_version"] == 1
    assert data["published_at"] is None
    assert data["claimed_at"] is None
    assert data["number"] == 1
    assert data["display_number"] == "TASK-0001"
    assert data["difficulty"] == 2
    assert data["difficulty_name"] == "L2"
    assert data["execution_timeout_seconds"] == 1800
    assert data["claim_mode"] == ClaimMode.OPEN.value
    assert data["is_archived"] is False
    assert data["idempotent_replay"] is False
    assert data["description"] == "实现任务编制流程"
    assert data["acceptance_criteria"] == "创建不发布；补齐要求后才能进入待办"

    task = Task.objects.get(pk=data["id"])
    assert task.status == TaskStatus.PREPARING.value
    assert task.version == 1
    assert task.requirements_version == 1
    assert task.published_at is None
    assert task.created_by_agent_id is None
    assert task.sort_order == Decimal(1000)
    assert task.active_execution_id is None
    assert task.assignee_agent_id is None

    # Creating never claims, reserves or starts a timer.
    assert TaskExecution.objects.count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0

    events = ActivityEvent.objects.filter(task=task)
    assert events.count() == 1
    event = events.get()
    assert event.action == "created"
    assert event.project_id == world.project_a.pk
    assert event.actor_type == "web_operator"
    assert event.before == {}
    assert event.after["status"] == TaskStatus.PREPARING.value
    assert OutboxEvent.objects.filter(event_type="task.created", entity_id=task.pk).count() == 1


def test_create_requires_idempotency_key_and_writes_nothing_without_it(client, world):
    response = client.post(
        WEB_TASKS, data=json.dumps(_payload(world)), content_type="application/json"
    )
    assert response.status_code == 400, response.content
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert Task.objects.count() == 0
    assert ActivityEvent.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


def test_numbers_are_per_project_sequential_and_never_reused(client, world):
    first = _create(client, world, title="一号")
    second = _create(client, world, title="二号")
    other_project = _create(client, world, title="B 项目一号", project_id=str(world.project_b.pk))

    assert (first["number"], second["number"]) == (1, 2)
    assert other_project["number"] == 1

    # A manually reserved high number (or a gap) never lets a later create reuse a number.
    factories.create_task(world.project_a, number=7, title="占位编号")
    fourth = _create(client, world, title="三号")
    assert fourth["number"] == 8

    # Archiving keeps the number reserved.
    archived = _create(client, world, title="待归档")
    assert archived["number"] == 9
    response = _post(
        client,
        f"{WEB_TASKS}/{archived['id']}/archive",
        {"expected_version": archived["version"], "reason": "不再需要"},
    )
    assert response.status_code == 200, response.content
    fifth = _create(client, world, title="四号")
    assert fifth["number"] == 10


def test_number_collision_is_translated_into_a_whole_service_retry(db, world):
    existing = factories.create_task(world.project_a, number=3, title="已占用编号")

    with pytest.raises(RetryTransaction):
        task_service._insert_task_row(
            project=world.project_a,
            number=3,
            title="冲突",
            difficulty=1,
            status=TaskStatus.PREPARING.value,
            claim_mode=ClaimMode.OPEN.value,
        )

    assert Task.objects.filter(pk=existing.pk).count() == 1
    assert Task.objects.filter(project=world.project_a).count() == 1


def test_retry_policy_reruns_the_authoring_service_after_a_number_race(db, world, monkeypatch):
    """Fault injection: the first attempt fails like a real unique conflict, then succeeds."""
    calls: list[int] = []
    real_insert = task_service._insert_task_row

    def flaky_insert(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RetryTransaction("模拟编号竞争")
        return real_insert(**kwargs)

    monkeypatch.setattr(task_service, "_insert_task_row", flaky_insert)

    from tests.conftest import agent_actor

    result = run_write_service(
        lambda: task_service.create_task(
            agent_actor(world.s1), _payload(world), "retry-key"
        )
    )

    assert len(calls) == 2
    assert result["status"] == TaskStatus.PREPARING.value
    assert Task.objects.filter(project=world.project_a).count() == 1
    assert ActivityEvent.objects.filter(action="created").count() == 1


def test_create_rejects_invalid_payloads_without_writing_rows(client, world):
    cases = [
        {"title": None},
        {"title": "   "},
        {"title": "x" * 201},
        {"difficulty": 0},
        {"difficulty": 6},
        {"difficulty": "2"},
        {"claim_mode": "BOTH"},
        {"claim_mode": ClaimMode.OPEN.value, "designated_agent_id": str(world.w1.pk)},
        {"claim_mode": ClaimMode.ASSIGNED.value},
        {"labels": "not-a-list"},
        {"due_at": "not-a-date"},
        {"status": TaskStatus.TODO.value},
        {"sort_order": 100},
    ]
    for overrides in cases:
        response = _post(client, WEB_TASKS, _payload(world, **overrides))
        assert response.status_code in (400, 404), (overrides, response.content)
        assert response.json()["error"]["code"] in ("INVALID_ARGUMENT", "RESOURCE_NOT_FOUND")

    assert Task.objects.count() == 0
    assert ActivityEvent.objects.count() == 0


def test_create_on_archived_or_completed_project_is_rejected(client, world):
    for status in (ProjectStatus.ARCHIVED, ProjectStatus.COMPLETED):
        project = factories.create_project(f"不可写项目-{status.value}", status=status)
        response = _post(client, WEB_TASKS, _payload(world, project_id=str(project.pk)))
        assert response.status_code == 409, response.content
        assert response.json()["error"]["code"] == "PROJECT_NOT_ACTIVE"

    assert Task.objects.count() == 0


# --------------------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------------------


def test_publish_moves_to_todo_after_completing_requirements(client, world):
    prepared = _create(
        client, world, description="", acceptance_criteria="", title="待补要求"
    )
    assert prepared["status"] == TaskStatus.PREPARING.value

    failed = _publish(client, prepared["id"], prepared["version"])
    assert failed.status_code == 400, failed.content
    assert failed.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert sorted(failed.json()["error"]["details"]["fields"]) == [
        "acceptance_criteria",
        "description",
    ]

    task = Task.objects.get(pk=prepared["id"])
    assert task.status == TaskStatus.PREPARING.value
    assert task.version == 1
    assert task.published_at is None
    assert ActivityEvent.objects.filter(task=task, action="published").count() == 0
    assert OutboxEvent.objects.filter(event_type="task.published").count() == 0

    edited = _patch(
        client,
        f"{WEB_TASKS}/{prepared['id']}",
        {
            "expected_version": 1,
            "description": "补齐要求",
            "acceptance_criteria": "可检查的验收标准",
        },
    )
    assert edited.status_code == 200, edited.content
    assert edited.json()["data"]["requirements_version"] == 2

    published = _publish(client, prepared["id"], 2)
    assert published.status_code == 200, published.content
    body = published.json()["data"]
    assert body["status"] == TaskStatus.TODO.value
    assert body["version"] == 3
    assert body["published_at"] is not None
    assert body["idempotent_replay"] is False

    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.version == 3
    assert task.published_at is not None
    assert task.requirements_version == 2

    event = ActivityEvent.objects.get(task=task, action="published")
    assert event.before["status"] == TaskStatus.PREPARING.value
    assert event.after["status"] == TaskStatus.TODO.value
    assert OutboxEvent.objects.filter(event_type="task.published", entity_id=task.pk).count() == 1
    assert TaskExecution.objects.count() == 0


@pytest.mark.parametrize("difficulty", [1, 2, 3, 4, 5])
def test_publish_accepts_every_valid_difficulty(client, world, difficulty):
    prepared = _create(client, world, difficulty=difficulty)
    assert prepared["difficulty"] == difficulty
    published = _publish(client, prepared["id"], prepared["version"])
    assert published.status_code == 200, published.content
    assert published.json()["data"]["status"] == TaskStatus.TODO.value


def test_publish_rejects_illegal_transitions_and_stale_versions(client, world):
    prepared = _create(client, world)
    task_id = prepared["id"]

    stale = _publish(client, task_id, 99)
    assert stale.status_code == 409, stale.content
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    assert _publish(client, task_id, 1).status_code == 200
    Task.objects.filter(pk=task_id).update(version=2)

    again = _publish(client, task_id, 2)
    assert again.status_code == 409, again.content
    assert again.json()["error"]["code"] == "INVALID_TRANSITION"
    assert again.json()["error"]["details"]["status"] == TaskStatus.TODO.value

    task = Task.objects.get(pk=task_id)
    assert task.status == TaskStatus.TODO.value
    assert task.version == 2
    assert ActivityEvent.objects.filter(task=task, action="published").count() == 1


def test_publish_requires_active_project(client, world):
    draft = factories.create_project("草稿项目", status=ProjectStatus.DRAFT)
    prepared = _create(client, world, project_id=str(draft.pk))
    assert prepared["status"] == TaskStatus.PREPARING.value

    response = _publish(client, prepared["id"], prepared["version"])
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "PROJECT_NOT_ACTIVE"

    task = Task.objects.get(pk=prepared["id"])
    assert task.status == TaskStatus.PREPARING.value
    assert task.version == 1


def test_publish_revalidates_the_designated_agent(client, world):
    prepared = _create(
        client,
        world,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w1.pk),
    )
    assert prepared["claim_mode"] == ClaimMode.ASSIGNED.value
    assert prepared["designated_agent_id"] == str(world.w1.pk)

    # Losing project membership after the designation blocks publishing.
    from apps.projects.models import ProjectMember

    ProjectMember.objects.filter(project=world.project_a, agent=world.w1).delete()
    response = _publish(client, prepared["id"], prepared["version"])
    assert response.status_code == 400, response.content
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert Task.objects.get(pk=prepared["id"]).status == TaskStatus.PREPARING.value

    # Re-designating a still-authorised agent lets it publish.
    factories.add_member(world.project_a, world.w1)
    assert _publish(client, prepared["id"], prepared["version"]).status_code == 200

    # A disabled designated agent is re-validated the same way.
    second = _create(
        client,
        world,
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=str(world.w2.pk),
    )
    Agent.objects.filter(pk=world.w2.pk).update(enabled=False)
    disabled = _publish(client, second["id"], second["version"])
    assert disabled.status_code == 400, disabled.content
    assert disabled.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_archived_project_blocks_edit_publish_and_archive(client, world):
    prepared = _create(client, world)
    ProjectStatus_archived = ProjectStatus.ARCHIVED.value
    from apps.projects.models import Project

    Project.objects.filter(pk=world.project_a.pk).update(status=ProjectStatus_archived)

    edit = _patch(
        client, f"{WEB_TASKS}/{prepared['id']}", {"expected_version": 1, "title": "改名"}
    )
    assert edit.status_code == 409, edit.content
    assert edit.json()["error"]["code"] == "PROJECT_NOT_ACTIVE"

    publish = _publish(client, prepared["id"], 1)
    assert publish.status_code == 409
    assert publish.json()["error"]["code"] == "PROJECT_NOT_ACTIVE"

    archive = _post(
        client, f"{WEB_TASKS}/{prepared['id']}/archive", {"expected_version": 1, "reason": "冻结"}
    )
    assert archive.status_code == 409
    assert archive.json()["error"]["code"] == "PROJECT_NOT_ACTIVE"

    task = Task.objects.get(pk=prepared["id"])
    assert task.title == "编制中的任务"
    assert task.status == TaskStatus.PREPARING.value
    assert task.version == 1
    assert ActivityEvent.objects.filter(task=task).count() == 1


# --------------------------------------------------------------------------------------
# Withdraw
# --------------------------------------------------------------------------------------


def test_withdraw_returns_to_preparing_and_requires_reason(client, world):
    prepared = _create(client, world)
    assert _publish(client, prepared["id"], 1).status_code == 200
    task_id = prepared["id"]

    missing_reason = _post(client, f"{WEB_TASKS}/{task_id}/withdraw", {"expected_version": 2})
    assert missing_reason.status_code == 400, missing_reason.content
    assert missing_reason.json()["error"]["code"] == "INVALID_ARGUMENT"

    blank_reason = _post(
        client, f"{WEB_TASKS}/{task_id}/withdraw", {"expected_version": 2, "reason": "   "}
    )
    assert blank_reason.status_code == 400

    withdrawn = _post(
        client,
        f"{WEB_TASKS}/{task_id}/withdraw",
        {"expected_version": 2, "reason": "要求需要重写"},
    )
    assert withdrawn.status_code == 200, withdrawn.content
    assert withdrawn.json()["data"]["status"] == TaskStatus.PREPARING.value
    assert withdrawn.json()["data"]["version"] == 3

    task = Task.objects.get(pk=task_id)
    assert task.status == TaskStatus.PREPARING.value
    assert task.published_at is not None  # history is preserved, only the status moves back
    event = ActivityEvent.objects.get(task=task, action="withdrawn")
    assert event.reason == "要求需要重写"
    assert event.before["status"] == TaskStatus.TODO.value
    assert OutboxEvent.objects.filter(event_type="task.withdrawn").count() == 1

    # A withdrawn task can be published again after the fix.
    fixed = _patch(
        client,
        f"{WEB_TASKS}/{task_id}",
        {"expected_version": 3, "acceptance_criteria": "新的验收标准"},
    )
    assert fixed.status_code == 200, fixed.content
    assert fixed.json()["data"]["requirements_version"] == 2
    assert _publish(client, task_id, 4).status_code == 200


def test_withdraw_forbidden_for_preparing_and_in_progress(client, world):
    prepared = _create(client, world)
    response = _post(
        client, f"{WEB_TASKS}/{prepared['id']}/withdraw", {"expected_version": 1, "reason": "x"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"

    in_progress, execution = _in_progress_task(world)
    response = _post(
        client,
        f"{WEB_TASKS}/{in_progress.pk}/withdraw",
        {"expected_version": in_progress.version, "reason": "x"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"

    task = Task.objects.get(pk=in_progress.pk)
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk
    assert task.version == in_progress.version


def test_task_reclaimed_back_to_todo_can_be_withdrawn_and_republished(client, world):
    """A once-claimed task must still be withdrawable and publishable (T13 acceptance)."""
    task, execution = _in_progress_task(world)
    now = db_now()
    execution.ended_at = now
    execution.end_reason = "RECLAIM"
    execution.save(update_fields=["ended_at", "end_reason"])
    task.status = TaskStatus.TODO.value
    task.active_execution = None
    task.assignee_agent = None
    task.claimed_at = None
    task.version = task.version + 1
    task.save(
        update_fields=["status", "active_execution", "assignee_agent", "claimed_at", "version"]
    )
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=0)

    withdrawn = _post(
        client,
        f"{WEB_TASKS}/{task.pk}/withdraw",
        {"expected_version": task.version, "reason": "返工前重新整理要求"},
    )
    assert withdrawn.status_code == 200, withdrawn.content
    assert withdrawn.json()["data"]["status"] == TaskStatus.PREPARING.value

    republished = _publish(client, task.pk, task.version + 1)
    assert republished.status_code == 200, republished.content
    assert republished.json()["data"]["status"] == TaskStatus.TODO.value
    # The ended execution history stays available.
    assert TaskExecution.objects.filter(task=task).count() == 1


# --------------------------------------------------------------------------------------
# PATCH whitelist and version rules
# --------------------------------------------------------------------------------------


def test_patch_rejects_status_assignment_and_unknown_fields(client, world):
    prepared = _create(client, world)
    url = f"{WEB_TASKS}/{prepared['id']}"

    forbidden = [
        {"status": TaskStatus.TODO.value},
        {"assignee_agent_id": str(world.w1.pk)},
        {"active_execution_id": str(uuid.uuid4())},
        {"current_submission_id": str(uuid.uuid4())},
        {"version": 7},
        {"requirements_version": 7},
        {"number": 42},
        {"project_id": str(world.project_b.pk)},
        {"is_archived": True},
        {"agent_id": str(world.w1.pk)},
        {"created_by_agent_id": str(world.w1.pk)},
        {"claim_mode": ClaimMode.ASSIGNED.value},
        {"sort_order": 500},
    ]
    for overrides in forbidden:
        payload = {"expected_version": 1, **overrides}
        response = _patch(client, url, payload)
        assert response.status_code == 400, (overrides, response.content)
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    empty = _patch(client, url, {"expected_version": 1})
    assert empty.status_code == 400, empty.content

    task = Task.objects.get(pk=prepared["id"])
    assert task.status == TaskStatus.PREPARING.value
    assert task.version == 1
    assert task.requirements_version == 1
    assert task.is_archived is False
    assert ActivityEvent.objects.filter(task=task).count() == 1


def test_patch_in_preparing_bumps_requirements_version_only_for_substantive_fields(client, world):
    prepared = _create(client, world)
    url = f"{WEB_TASKS}/{prepared['id']}"

    substantive = _patch(
        client,
        url,
        {"expected_version": 1, "title": "新标题", "description": "新描述", "difficulty": 4},
    )
    assert substantive.status_code == 200, substantive.content
    data = substantive.json()["data"]
    assert data["version"] == 2
    assert data["requirements_version"] == 2
    assert data["difficulty"] == 4
    assert data["execution_timeout_seconds"] == 28800

    non_substantive = _patch(
        client, url, {"expected_version": 2, "due_at": "2026-11-01T00:00:00Z", "labels": ["紧急"]}
    )
    assert non_substantive.status_code == 200, non_substantive.content
    data = non_substantive.json()["data"]
    assert data["version"] == 3
    assert data["requirements_version"] == 2
    assert data["labels"] == ["紧急"]
    assert data["due_at"] == "2026-11-01T00:00:00Z"

    stale = _patch(client, url, {"expected_version": 1, "title": "旧版本"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    task = Task.objects.get(pk=prepared["id"])
    assert task.title == "新标题"
    assert task.version == 3
    assert [event.action for event in ActivityEvent.objects.filter(task=task)] == [
        "created",
        "updated",
        "updated",
    ]
    assert OutboxEvent.objects.filter(event_type="task.updated", entity_id=task.pk).count() == 2


def test_patch_in_todo_requires_withdraw_for_substantive_changes(client, world):
    prepared = _create(client, world)
    assert _publish(client, prepared["id"], 1).status_code == 200
    url = f"{WEB_TASKS}/{prepared['id']}"

    for overrides in (
        {"description": "改动实质要求"},
        {"acceptance_criteria": "改动验收"},
        {"difficulty": 5},
    ):
        response = _patch(client, url, {"expected_version": 2, **overrides})
        assert response.status_code == 409, (overrides, response.content)
        assert response.json()["error"]["code"] == "INVALID_TRANSITION"

    task = Task.objects.get(pk=prepared["id"])
    assert task.version == 2
    assert task.requirements_version == 1
    assert task.description == "实现任务编制流程"
    assert ActivityEvent.objects.filter(task=task).count() == 2

    # Title, labels and the planning deadline stay editable while the task is TODO, and a
    # non-substantive edit never bumps requirements_version.
    renamed = _patch(client, url, {"expected_version": 2, "title": "待办中改名"})
    assert renamed.status_code == 200, renamed.content
    assert renamed.json()["data"]["version"] == 3
    assert renamed.json()["data"]["requirements_version"] == 1

    allowed = _patch(
        client, url, {"expected_version": 3, "due_at": "2026-12-01T00:00:00Z", "labels": ["P1"]}
    )
    assert allowed.status_code == 200, allowed.content
    assert allowed.json()["data"]["version"] == 4
    assert allowed.json()["data"]["requirements_version"] == 1
    assert allowed.json()["data"]["title"] == "待办中改名"


def test_patch_after_claim_cannot_overwrite_requirements_snapshot(client, world):
    task, execution = _in_progress_task(world)
    url = f"{WEB_TASKS}/{task.pk}"

    for overrides in (
        {"title": "覆盖标题"},
        {"description": "覆盖描述"},
        {"acceptance_criteria": "覆盖验收"},
        {"difficulty": 5},
    ):
        response = _patch(client, url, {"expected_version": task.version, **overrides})
        assert response.status_code == 409, (overrides, response.content)
        assert response.json()["error"]["code"] == "INVALID_TRANSITION"

    allowed = _patch(
        client, url, {"expected_version": task.version, "due_at": None, "labels": ["返工前"]}
    )
    assert allowed.status_code == 200, allowed.content
    assert allowed.json()["data"]["labels"] == ["返工前"]

    fresh = Task.objects.get(pk=task.pk)
    assert fresh.title == "进行中任务"
    assert fresh.description == "描述"
    assert fresh.difficulty == 2
    assert fresh.requirements_version == 1
    assert fresh.active_execution_id == execution.pk
    assert fresh.assignee_agent_id == world.w1.pk


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


def test_same_key_and_body_replays_without_second_side_effect(client, world):
    payload = _payload(world)
    first = _post(client, WEB_TASKS, payload, key="replay-1")
    assert first.status_code == 200, first.content
    first_data = first.json()["data"]
    assert first_data["idempotent_replay"] is False

    second = _post(client, WEB_TASKS, payload, key="replay-1")
    assert second.status_code == 200, second.content
    second_data = second.json()["data"]
    assert second_data["idempotent_replay"] is True
    assert second_data["id"] == first_data["id"]
    assert second_data["version"] == first_data["version"]

    assert Task.objects.count() == 1
    assert Task.objects.filter(project=world.project_a).count() == 1
    assert ActivityEvent.objects.filter(action="created").count() == 1
    assert OutboxEvent.objects.filter(event_type="task.created").count() == 1

    different = _post(client, WEB_TASKS, _payload(world, title="另一个任务"), key="replay-1")
    assert different.status_code == 409, different.content
    assert different.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert Task.objects.count() == 1


def test_publish_replay_keeps_the_original_receipt_after_later_changes(client, world):
    prepared = _create(client, world)
    url = f"{WEB_TASKS}/{prepared['id']}/publish"
    first = _post(client, url, {"expected_version": 1}, key="publish-key")
    assert first.status_code == 200, first.content

    # Even after the task moved on (stale status/version), the same key returns the receipt.
    replayed = _post(client, url, {"expected_version": 1}, key="publish-key")
    assert replayed.status_code == 200, replayed.content
    assert replayed.json()["data"]["idempotent_replay"] is True
    assert replayed.json()["data"]["status"] == TaskStatus.TODO.value
    assert replayed.json()["data"]["version"] == 2
    assert ActivityEvent.objects.filter(action="published").count() == 1
    assert OutboxEvent.objects.filter(event_type="task.published").count() == 1


# --------------------------------------------------------------------------------------
# Detail and activity endpoints
# --------------------------------------------------------------------------------------


def test_task_detail_exposes_requirements_current_execution_and_history(client, world):
    task, execution = _in_progress_task(world)
    asset = FileAsset.objects.create(
        storage_key=f"files/{uuid.uuid4().hex}",
        original_name="参考.md",
        media_type="text/markdown",
        size=12,
        sha256="0" * 64,
        uploader_type="web_operator",
        project=world.project_a,
    )
    AttachmentLink.objects.create(
        file_asset=asset, project=world.project_a, task=task, purpose="TASK_REFERENCE"
    )

    response = client.get(f"{WEB_TASKS}/{task.pk}")
    assert response.status_code == 200, response.content
    data = response.json()["data"]

    assert data["id"] == str(task.pk)
    assert data["status"] == TaskStatus.IN_PROGRESS.value
    assert data["description"] == "描述"
    assert data["acceptance_criteria"] == "验收标准"
    assert data["difficulty"] == 2
    assert data["execution_timeout_seconds"] == 1800
    assert data["claim_mode"] == ClaimMode.OPEN.value
    assert data["attachment_count"] == 1
    assert data["current_execution"]["execution_id"] == str(execution.pk)
    assert data["current_execution"]["agent_id"] == str(world.w1.pk)
    assert data["current_submission"] is None
    assert [item["execution_id"] for item in data["execution_history"]] == [str(execution.pk)]
    assert data["submission_history"] == []
    assert data["review_history"] == []
    assert data["execution_deadline_at"] == data["current_execution"]["execution_deadline_at"]

    missing = client.get(f"{WEB_TASKS}/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    malformed = client.get(f"{WEB_TASKS}/not-a-uuid")
    assert malformed.status_code == 400


def test_activity_endpoint_returns_the_task_timeline_newest_last(client, world):
    prepared = _create(client, world)
    assert _publish(client, prepared["id"], 1).status_code == 200
    assert (
        _post(
            client,
            f"{WEB_TASKS}/{prepared['id']}/withdraw",
            {"expected_version": 2, "reason": "重写要求"},
        ).status_code
        == 200
    )

    response = client.get(f"{WEB_TASKS}/{prepared['id']}/activity")
    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert body["total"] == 3
    assert [item["action"] for item in body["items"]] == ["created", "published", "withdrawn"]
    assert body["items"][-1]["reason"] == "重写要求"
    assert body["items"][0]["request_id"]

    paged = client.get(f"{WEB_TASKS}/{prepared['id']}/activity?page=2&page_size=2")
    assert paged.status_code == 200
    assert len(paged.json()["data"]["items"]) == 1
    assert paged.json()["data"]["page"] == 2


# --------------------------------------------------------------------------------------
# AgentAPI surface: identity comes from the Key, workers cannot author or publish
# --------------------------------------------------------------------------------------

AGENT_TASKS = "/api/agent/v1/tasks"


@pytest.fixture
def agent_api():
    from fastapi.testclient import TestClient

    from agentapi.main import app

    return TestClient(app)


def _agent_post(api, url, token, payload=None, *, idem=None, query=None):
    headers = {"AGENT_TOKEN": token}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    body = {} if payload is None else payload
    full = f"{url}?{query}" if query else url
    return api.post(full, json=body, headers=headers)


def _agent_patch(api, url, token, payload, *, idem):
    return api.patch(url, json=payload, headers={"AGENT_TOKEN": token, "Idempotency-Key": idem})


@pytest.mark.django_db(transaction=True)
def test_agent_supervisor_authors_and_publishes_while_worker_is_forbidden(world, agent_api):
    created = _agent_post(
        agent_api,
        AGENT_TASKS,
        world.s1_key,
        _payload(world),
        idem=_key(),
    )
    assert created.status_code == 200, created.content
    data = created.json()["data"]
    assert data["status"] == TaskStatus.PREPARING.value
    assert data["version"] == 1

    task = Task.objects.get(pk=data["id"])
    assert task.created_by_agent_id == world.s1.pk
    event = ActivityEvent.objects.get(task=task, action="created")
    assert event.actor_type == "agent"
    assert event.actor_agent_id == world.s1.pk

    url = f"{AGENT_TASKS}/{task.pk}"
    for response in (
        _agent_post(agent_api, AGENT_TASKS, world.w1_key, _payload(world), idem=_key()),
        _agent_patch(
            agent_api, url, world.w1_key, {"expected_version": 1, "title": "改名"}, idem=_key()
        ),
        _agent_post(
            agent_api,
            f"{url}/publish",
            world.w1_key,
            {"expected_version": 1},
            idem=_key(),
        ),
        _agent_post(
            agent_api,
            f"{url}/assign",
            world.w1_key,
            {"expected_version": 1, "claim_mode": ClaimMode.OPEN.value},
            idem=_key(),
        ),
    ):
        assert response.status_code == 403, response.content
        assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"

    # Body-supplied identity is refused outright.
    identity = _agent_post(
        agent_api,
        AGENT_TASKS,
        world.s1_key,
        _payload(world, agent_id=str(world.w1.pk)),
        idem=_key(),
    )
    assert identity.status_code == 400, identity.content
    assert identity.json()["error"]["code"] == "INVALID_ARGUMENT"

    override = _agent_post(
        agent_api,
        f"{url}/publish",
        world.s1_key,
        {"expected_version": 1, "assignee_agent_id": str(world.w1.pk)},
        idem=_key(),
    )
    assert override.status_code == 400

    published = _agent_post(
        agent_api, f"{url}/publish", world.s1_key, {"expected_version": 1}, idem=_key()
    )
    assert published.status_code == 200, published.content
    assert published.json()["data"]["status"] == TaskStatus.TODO.value
    assert Task.objects.get(pk=task.pk).status == TaskStatus.TODO.value


@pytest.mark.django_db(transaction=True)
def test_agent_api_requires_a_valid_token(world, agent_api):
    missing = agent_api.post(AGENT_TASKS, json=_payload(world))
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AGENT_TOKEN_MISSING"

    bogus = _agent_post(agent_api, AGENT_TASKS, "not-a-key", _payload(world), idem=_key())
    assert bogus.status_code == 401
    assert bogus.json()["error"]["code"] == "AGENT_TOKEN_INVALID"
    assert Task.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_agent_publish_requires_membership_of_the_task_project(world, agent_api):
    """U supervises nothing in project A: the task it cannot see answers 404."""
    created = _agent_post(agent_api, AGENT_TASKS, world.s1_key, _payload(world), idem=_key())
    task_id = created.json()["data"]["id"]
    Agent.objects.filter(pk=world.u.pk).update(role="SUPERVISOR")

    forbidden = _agent_post(
        agent_api, f"{AGENT_TASKS}/{task_id}/publish", world.u_key, {"expected_version": 1}, idem=_key()
    )
    assert forbidden.status_code == 404, forbidden.content
    assert forbidden.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert Task.objects.get(pk=task_id).status == TaskStatus.PREPARING.value
