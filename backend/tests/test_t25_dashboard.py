"""T25 acceptance: scope-consistent global statistics and exception summary.

Every case drives the real services (claim -> submit -> reject/approve) and asserts the
returned counts plus the database rows behind them; nothing is mocked. The A20 sample
(1/2/1/1/1 -> reject -> 1/3/1/0/1, total 6) is covered first because it is the frozen
acceptance scenario of DEVELOP D17.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from django.db import connection
from django.test import Client as DjangoClient
from django.test.utils import CaptureQueriesContext
from fastapi.testclient import TestClient

from apps.identity.models import Agent
from apps.tasks.models import Task, TaskExecution, TaskSubmission
from domain.claim_service import ClaimRequest, claim_task
from domain.clock import db_now
from domain.contracts import (
    DIFFICULTY_LEVELS,
    ActorContext,
    ActorType,
    ReviewDecision,
    TaskStatus,
)
from domain.dashboard_service import (
    EXCEPTION_OVERDUE_RECLAIM,
    EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT,
    NO_TASKS_REASON,
    REASON_DESIGNATED_AGENT_DISABLED,
    REASON_EXECUTION_OVERDUE,
    TASK_VISIBILITY_FULL,
    TASK_VISIBILITY_PUBLISHED_ONLY,
    DashboardScope,
    get_dashboard,
)
from domain.review_service import review_submission
from domain.service_runner import run_request_service
from domain.submission_service import submit_result
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.dashboard, pytest.mark.api]

HOST = {"HTTP_HOST": "127.0.0.1:8000"}


def new_key() -> str:
    return str(uuid.uuid4())


def web() -> ActorContext:
    return ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))


def claim(agent, task) -> TaskExecution:
    """Claim one specific task through the real atomic claim service."""
    result = run_request_service(
        lambda: claim_task(
            agent_actor(agent),
            ClaimRequest(levels=[task.difficulty], task_id=task.pk, expected_version=task.version),
            new_key(),
        ),
        global_claim_lock=True,
    )
    assert result["data"] is not None, result
    task.refresh_from_db()
    return TaskExecution.objects.get(pk=result["data"]["execution_id"])


def submit(actor, task, execution, *, summary: str = "已完成实现") -> TaskSubmission:
    body = {
        "execution_id": str(execution.pk),
        "expected_version": task.version,
        "summary": summary,
        "content": "变更说明与验证步骤",
        "self_check": [{"criterion": "验收标准一", "passed": True, "evidence": "记录"}],
        "artifacts": [],
        "links": [],
    }
    run_request_service(
        lambda: submit_result(actor, task_id=task.pk, payload=body, idem_key=new_key()),
        require_write_lock=False,
    )
    task.refresh_from_db()
    return TaskSubmission.objects.get(pk=task.current_submission_id)


def review(actor, task, submission, decision: str, *, comment: str = "已核对"):
    body = {
        "submission_id": str(submission.pk),
        "expected_version": task.version,
        "decision": decision,
        "comment": comment,
    }
    run_request_service(
        lambda: review_submission(actor, task_id=task.pk, payload=body, idem_key=new_key()),
        require_write_lock=False,
    )
    task.refresh_from_db()


def back_date_execution(execution: TaskExecution, *, seconds: int) -> None:
    """Move the whole claim window back so the fixed deadline is already past."""
    moved_claim = execution.claimed_at - timedelta(seconds=seconds)
    TaskExecution.objects.filter(pk=execution.pk).update(
        claimed_at=moved_claim,
        execution_deadline_at=moved_claim + timedelta(seconds=execution.execution_timeout_seconds),
    )
    execution.refresh_from_db()


# --------------------------------------------------------------------------------------
# 1. A20 sample: 1/2/1/1/1, then one real reject -> 1/3/1/0/1 with the same total
# --------------------------------------------------------------------------------------


def test_a20_six_task_sample_then_one_real_reject_keeps_the_total(db):
    world = factories.World()
    project = world.project_a

    factories.create_task(project, title="准备中", status=TaskStatus.PREPARING, difficulty=5)
    factories.create_task(project, title="待办 A", difficulty=1)
    factories.create_task(project, title="待办 B", difficulty=2)
    in_progress = factories.create_task(project, title="进行中", difficulty=3)
    in_review = factories.create_task(project, title="待审核", difficulty=4)
    done = factories.create_task(project, title="已完成", difficulty=1)

    # Real claim -> submit for both IN_PROGRESS / IN_REVIEW / DONE sample rows.
    claim(world.w1, in_progress)
    review_execution = claim(world.w2, in_review)
    submission = submit(agent_actor(world.w2), in_review, review_execution)
    done_execution = claim(world.w2, done)
    done_submission = submit(agent_actor(world.w2), done, done_execution)
    review(agent_actor(world.s1), done, done_submission, ReviewDecision.APPROVE.value, comment="通过")

    before = get_dashboard(web(), DashboardScope())
    assert before["task_status_counts"] == {
        "PREPARING": 1,
        "TODO": 2,
        "IN_PROGRESS": 1,
        "IN_REVIEW": 1,
        "DONE": 1,
    }
    assert before["task_total"] == 6
    assert sum(before["task_status_counts"].values()) == 6
    assert before["difficulty_counts"] == {"1": 2, "2": 1, "3": 1, "4": 1, "5": 1}
    assert sum(before["difficulty_counts"].values()) == 6

    # ONE real reject: IN_REVIEW -> TODO, history preserved, no new task.
    review(
        agent_actor(world.s1),
        in_review,
        submission,
        ReviewDecision.REJECT.value,
        comment="验收标准未满足",
    )

    after = get_dashboard(web(), DashboardScope())
    assert after["task_status_counts"] == {
        "PREPARING": 1,
        "TODO": 3,
        "IN_PROGRESS": 1,
        "IN_REVIEW": 0,
        "DONE": 1,
    }
    assert after["task_total"] == 6
    assert sum(after["task_status_counts"].values()) == 6
    assert after["difficulty_counts"] == before["difficulty_counts"]
    assert Task.objects.filter(project=project).count() == 6
    assert TaskSubmission.objects.filter(task=in_review).count() == 1
    assert after["completion"]["done"] == 1
    assert after["completion"]["rate"] == round(1 / 6, 4)


# --------------------------------------------------------------------------------------
# 2. History is not a task
# --------------------------------------------------------------------------------------


def test_executions_and_extra_submissions_never_change_task_counts(db):
    world = factories.World()
    task = factories.create_task(world.project_a, title="返工两轮", difficulty=2)

    first_execution = claim(world.w1, task)
    first_submission = submit(agent_actor(world.w1), task, first_execution)
    review(agent_actor(world.s1), task, first_submission, ReviewDecision.REJECT.value, comment="返工")

    second_execution = claim(world.w1, task)
    submit(agent_actor(world.w1), task, second_execution, summary="第二轮补充完成")

    assert TaskExecution.objects.filter(task=task).count() == 2
    assert TaskSubmission.objects.filter(task=task).count() == 2

    data = get_dashboard(web(), DashboardScope())
    assert data["task_total"] == 1
    assert data["task_status_counts"] == {
        "PREPARING": 0,
        "TODO": 0,
        "IN_PROGRESS": 0,
        "IN_REVIEW": 1,
        "DONE": 0,
    }
    assert sum(data["difficulty_counts"].values()) == 1
    assert data["completion"]["done"] == 0
    assert data["completion"]["rate"] == 0.0
    assert data["completion"]["rate_available"] is True


# --------------------------------------------------------------------------------------
# 3. Worker scope: PREPARING 0 + PUBLISHED_ONLY, supervisor sees the real count
# --------------------------------------------------------------------------------------


@pytest.mark.identity
def test_worker_reports_zero_preparing_and_published_only_scope(db):
    world = factories.World()
    project = world.project_a
    factories.create_task(project, title="准备中", status=TaskStatus.PREPARING, difficulty=3)
    factories.create_task(project, title="待办", status=TaskStatus.TODO, difficulty=3)

    worker = get_dashboard(agent_actor(world.w1), DashboardScope())
    assert worker["scope"]["task_visibility"] == TASK_VISIBILITY_PUBLISHED_ONLY
    assert worker["scope"]["role"] == "WORKER"
    assert worker["task_status_counts"]["PREPARING"] == 0
    assert worker["task_total"] == 1
    assert sum(worker["task_status_counts"].values()) == 1
    assert worker["projects"][0]["task_status_counts"]["PREPARING"] == 0
    assert worker["projects"][0]["task_total"] == 1

    supervisor = get_dashboard(agent_actor(world.s1), DashboardScope())
    assert supervisor["scope"]["task_visibility"] == TASK_VISIBILITY_FULL
    assert supervisor["scope"]["role"] == "SUPERVISOR"
    assert supervisor["task_status_counts"]["PREPARING"] == 1
    assert supervisor["task_total"] == 2

    # The local administrator has the same full visibility as a supervisor.
    administrator = get_dashboard(web(), DashboardScope())
    assert administrator["scope"]["task_visibility"] == TASK_VISIBILITY_FULL
    assert administrator["scope"]["actor_type"] == ActorType.WEB_OPERATOR.value
    assert administrator["task_status_counts"]["PREPARING"] == 1
    assert administrator["task_total"] == 2


# --------------------------------------------------------------------------------------
# 4. Agent scope: another project's tasks and agents never appear
# --------------------------------------------------------------------------------------


def test_agent_dashboard_only_contains_its_own_projects(db):
    world = factories.World()
    factories.create_task(world.project_a, title="A-1", difficulty=1)
    factories.create_task(world.project_a, title="A-2", difficulty=1)
    factories.create_task(world.project_a, title="A-归档", is_archived=True, difficulty=1)
    factories.create_task(world.project_b, title="B-1", difficulty=1)
    factories.create_task(world.project_b, title="B-2", difficulty=1)

    data = get_dashboard(agent_actor(world.w1), DashboardScope())
    assert data["scope"]["project_ids"] == [str(world.project_a.pk)]
    assert data["scope"]["project_count"] == 1
    assert data["task_total"] == 2
    assert data["task_status_counts"]["TODO"] == 2
    assert [project["project_id"] for project in data["projects"]] == [str(world.project_a.pk)]
    assert data["project_total"] == 1

    # Agents referenced by the workload stay inside the caller's visible projects.
    agent_ids = {item["agent_id"] for item in data["agents"]}
    assert str(world.w1.pk) in agent_ids
    assert str(world.u.pk) not in agent_ids
    assert data["agent_counts"]["total"] == 4

    # The local administrator sees both projects: 4 non-archived tasks.
    administrator = get_dashboard(web(), DashboardScope())
    assert administrator["task_total"] == 4
    assert str(world.u.pk) in {item["agent_id"] for item in administrator["agents"]}


# --------------------------------------------------------------------------------------
# 5. Empty scope reports "no tasks", never 100%
# --------------------------------------------------------------------------------------


def test_empty_project_reports_no_tasks_instead_of_full_completion(db):
    world = factories.World()
    factories.create_task(world.project_a, title="其他项目任务", difficulty=1)

    data = get_dashboard(web(), DashboardScope(project_id=world.project_b.pk))
    assert data["task_total"] == 0
    assert data["task_status_counts"] == {status.value: 0 for status in TaskStatus}
    assert sum(data["task_status_counts"].values()) == 0
    assert data["completion_rate"] is None
    assert data["completion"] == {
        "done": 0,
        "total": 0,
        "rate": None,
        "rate_available": False,
        "reason": NO_TASKS_REASON,
    }
    assert data["completion_rate"] != 1
    assert data["projects"][0]["project_id"] == str(world.project_b.pk)
    assert data["projects"][0]["task_total"] == 0
    assert data["projects"][0]["completion_rate"] is None

    # An agent with no project at all has an empty scope, not a fabricated rate.
    lonely, _ = factories.create_agent("无项目员工", "WORKER")
    empty = get_dashboard(agent_actor(lonely), DashboardScope())
    assert empty["scope"]["project_ids"] == []
    assert empty["scope"]["project_count"] == 0
    assert empty["task_total"] == 0
    assert empty["completion_rate"] is None
    assert empty["agents"] == []


# --------------------------------------------------------------------------------------
# 6. include_archived
# --------------------------------------------------------------------------------------


def test_include_archived_toggle_adds_exactly_the_archived_tasks(db):
    world = factories.World()
    for index in range(3):
        factories.create_task(world.project_a, title=f"当前任务 {index}", difficulty=1)
    factories.create_task(world.project_a, title="任务归档标记", is_archived=True, difficulty=1)
    factories.create_task(world.archived_project, title="归档项目任务 1", difficulty=1)
    factories.create_task(world.archived_project, title="归档项目任务 2", difficulty=1)

    default = get_dashboard(web(), DashboardScope())
    assert default["task_total"] == 3
    assert default["archived_task_total"] == 3
    assert default["scope"]["include_archived"] is False
    assert default["scope"]["archived_project_total"] == 1
    assert default["project_counts"]["ARCHIVED"] == 0
    assert default["project_total"] == 2

    included = get_dashboard(web(), DashboardScope(include_archived=True))
    assert included["task_total"] == 6
    assert included["task_total"] == default["task_total"] + default["archived_task_total"]
    assert included["task_status_counts"]["TODO"] == 6
    assert included["project_counts"]["ARCHIVED"] == 1
    assert included["project_total"] == 3
    assert included["scope"]["include_archived"] is True


# --------------------------------------------------------------------------------------
# 7. Timed-out but unreclaimed: not working, slot still occupied
# --------------------------------------------------------------------------------------


@pytest.mark.timeout
def test_timed_out_unreclaimed_execution_is_not_working_but_holds_its_slot(db):
    world = factories.World()
    task = factories.create_task(world.project_a, title="超时未回收", difficulty=2)
    execution = claim(world.w1, task)
    back_date_execution(execution, seconds=execution.execution_timeout_seconds + 60)
    assert execution.execution_deadline_at <= db_now()
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1

    data = get_dashboard(web(), DashboardScope())

    # Not working, still holding the slot: both numbers must be reported separately.
    assert data["agent_counts"]["working"] == 0
    assert data["execution_counts"]["effective_executions"] == 0
    assert data["execution_counts"]["waiting_reclaim_executions"] == 1
    assert data["execution_counts"]["open_executions"] == 1
    assert data["execution_counts"]["reserved_slots"] == 1

    agent_item = next(item for item in data["agents"] if item["agent_id"] == str(world.w1.pk))
    assert agent_item["effective_executions"] == 0
    assert agent_item["waiting_reclaim_executions"] == 1
    assert agent_item["reserved_slots"] == 1
    assert agent_item["available_capacity"] == 0

    # The task itself is located by the exception list, and task counts are untouched.
    assert data["exception_counts"][EXCEPTION_OVERDUE_RECLAIM] == 1
    assert data["exception_counts"][EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT] == 0
    items = [item for item in data["exceptions"] if item["task_id"] == str(task.pk)]
    assert len(items) == 1
    assert items[0]["reason"] == REASON_EXECUTION_OVERDUE
    assert items[0]["kind"] == EXCEPTION_OVERDUE_RECLAIM
    assert items[0]["overdue_seconds"] > 0
    assert items[0]["execution_deadline_at"] is not None
    assert data["task_status_counts"]["IN_PROGRESS"] == 1
    assert data["task_total"] == 1

    # The database still says the execution has not ended: nothing was reclaimed by a read.
    execution.refresh_from_db()
    assert execution.ended_at is None
    assert task.status == TaskStatus.IN_PROGRESS.value


@pytest.mark.timeout
def test_unusable_designated_agent_is_reported_as_an_exception(db):
    world = factories.World()
    disabled, _ = factories.create_agent("被禁用的指定人", "WORKER", enabled=False, max_concurrency=1)
    factories.add_member(world.project_a, disabled)
    outsider, _ = factories.create_agent("非成员指定人", "WORKER")
    disabled_task = factories.create_task(
        world.project_a,
        title="指定给禁用 Agent",
        difficulty=1,
        claim_mode="ASSIGNED",
        designated_agent=disabled,
    )
    outsider_task = factories.create_task(
        world.project_a,
        title="指定给非成员 Agent",
        difficulty=1,
        claim_mode="ASSIGNED",
        designated_agent=outsider,
    )
    ok_task = factories.create_task(
        world.project_a,
        title="指定给有效成员",
        difficulty=1,
        claim_mode="ASSIGNED",
        designated_agent=world.w1,
    )

    data = get_dashboard(web(), DashboardScope())
    assert data["exception_counts"][EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT] == 2
    assert data["exception_counts"][EXCEPTION_OVERDUE_RECLAIM] == 0
    by_task = {item["task_id"]: item for item in data["exceptions"]}
    assert str(ok_task.pk) not in by_task
    assert by_task[str(disabled_task.pk)]["reasons"] == [REASON_DESIGNATED_AGENT_DISABLED]
    assert by_task[str(disabled_task.pk)]["designated_agent_name"] == "被禁用的指定人"
    assert by_task[str(outsider_task.pk)]["reasons"] == ["DESIGNATED_AGENT_NOT_MEMBER"]
    # A designated but unclaimed task never occupies a working slot.
    assert data["execution_counts"]["reserved_slots"] == 0
    assert data["agent_counts"]["working"] == 0


# --------------------------------------------------------------------------------------
# 8. as_of and internal consistency
# --------------------------------------------------------------------------------------


def test_as_of_is_utc_and_the_returned_counts_agree(db):
    world = factories.World()
    factories.create_task(world.project_a, title="准备中", status=TaskStatus.PREPARING, difficulty=1)
    factories.create_task(world.project_a, title="待办", status=TaskStatus.TODO, difficulty=2)
    factories.create_task(world.project_b, title="待办 B", status=TaskStatus.TODO, difficulty=3)

    data = get_dashboard(web(), DashboardScope())

    assert data["as_of"].endswith("Z")
    as_of = datetime.fromisoformat(data["as_of"])
    assert as_of.tzinfo is not None
    assert as_of.utcoffset() == timedelta(0)
    assert abs((db_now() - as_of).total_seconds()) < 300

    assert set(data["task_status_counts"]) == {status.value for status in TaskStatus}
    assert set(data["difficulty_counts"]) == {"1", "2", "3", "4", "5"}
    assert sum(data["task_status_counts"].values()) == data["task_total"] == 3
    assert sum(data["difficulty_counts"].values()) == data["task_total"]
    assert data["completion"]["done"] == data["task_status_counts"]["DONE"]
    assert data["completion"]["total"] == data["task_total"]
    assert data["project_total"] == sum(data["project_counts"].values())
    assert sum(project["task_total"] for project in data["projects"]) == data["task_total"]
    assert data["exception_counts"]["total"] == (
        data["exception_counts"][EXCEPTION_OVERDUE_RECLAIM]
        + data["exception_counts"][EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT]
    )

    # Connectivity buckets are mutually exclusive and cover every referenced agent.
    counts = data["agent_counts"]
    assert (
        counts["online"] + counts["offline"] + counts["never_connected"] + counts["disabled"]
        == counts["total"]
    )
    assert counts["online"] == 0  # 未心跳过的 fixture Agent
    assert counts["never_connected"] == counts["total"]


def test_the_five_status_and_five_difficulty_counts_come_from_one_statement(db):
    """D17: the counts must come from ONE conditional aggregate, not per-status queries."""
    world = factories.World()
    factories.create_task(world.project_a, title="待办", difficulty=1)
    factories.create_task(
        world.project_a, title="准备中", status=TaskStatus.PREPARING, difficulty=4
    )

    with CaptureQueriesContext(connection) as captured:
        data = get_dashboard(web(), DashboardScope())

    # Exactly one ungrouped statement carries both the five status counts and the five
    # difficulty counts; splitting them into several queries would break this.
    def is_the_d17_statement(sql: str) -> bool:
        return (
            "GROUP BY" not in sql.upper()
            and "tasks_task" in sql
            and all(status.value in sql for status in TaskStatus)
            and all(f"difficulty_{level}" in sql for level in DIFFICULTY_LEVELS)
        )

    aggregates = [
        query["sql"] for query in captured.captured_queries if is_the_d17_statement(query["sql"])
    ]
    assert len(aggregates) == 1, "\n\n".join(aggregates)
    assert data["task_total"] == 2
    assert data["task_status_counts"]["PREPARING"] == 1


def test_agent_connectivity_buckets_are_mutually_exclusive(db):
    """product 4.3: disabled wins, then never connected / online / offline (90s window)."""
    world = factories.World()
    now = db_now()
    Agent.objects.filter(pk=world.s1.pk).update(last_heartbeat_at=now - timedelta(seconds=10))
    Agent.objects.filter(pk=world.s2.pk).update(last_heartbeat_at=now - timedelta(seconds=600))
    Agent.objects.filter(pk=world.w2.pk).update(enabled=False)

    data = get_dashboard(web(), DashboardScope())
    counts = data["agent_counts"]
    assert counts["total"] == 5
    assert counts["online"] == 1
    assert counts["offline"] == 1
    assert counts["disabled"] == 1
    assert counts["never_connected"] == 2
    assert counts["enabled"] == 4
    assert counts["online"] + counts["offline"] + counts["never_connected"] + counts["disabled"] == 5

    states = {item["agent_id"]: item["online_state"] for item in data["agents"]}
    assert states[str(world.s1.pk)] == "ONLINE_IDLE"
    assert states[str(world.s2.pk)] == "OFFLINE"
    assert states[str(world.w2.pk)] == "DISABLED"
    assert states[str(world.w1.pk)] == "NEVER_CONNECTED"


def test_dashboard_read_does_not_change_any_business_row(db):
    world = factories.World()
    task = factories.create_task(world.project_a, title="只读检查", difficulty=2)
    execution = claim(world.w1, task)

    before_task = Task.objects.get(pk=task.pk)
    before_agent = Agent.objects.get(pk=world.w1.pk)
    before_events = len(TaskExecution.objects.filter(task=task))

    get_dashboard(web(), DashboardScope())
    get_dashboard(agent_actor(world.w1), DashboardScope())

    after_task = Task.objects.get(pk=task.pk)
    assert after_task.version == before_task.version
    assert after_task.status == before_task.status == TaskStatus.IN_PROGRESS.value
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1
    assert Agent.objects.get(pk=world.w1.pk).version == before_agent.version
    assert len(TaskExecution.objects.filter(task=task)) == before_events == 1
    execution.refresh_from_db()
    assert execution.ended_at is None


# --------------------------------------------------------------------------------------
# 9. WebAPI HTTP surface
# --------------------------------------------------------------------------------------


def test_webapi_dashboard_and_activity_over_http(db):
    world = factories.World()
    task = factories.create_task(world.project_a, title="HTTP 统计", difficulty=2)
    claim(world.w1, task)

    client = DjangoClient()

    response = client.get("/api/web/v1/dashboard", **HOST)
    assert response.status_code == 200, response.content
    body = json.loads(response.content)
    assert set(body) == {"data", "request_id"}
    assert response["X-Request-Id"] == body["request_id"]
    data = body["data"]
    assert data["scope"]["task_visibility"] == TASK_VISIBILITY_FULL
    assert data["scope"]["actor_type"] == ActorType.WEB_OPERATOR.value
    assert data["scope"]["include_archived"] is False
    assert data["task_total"] == 1
    assert data["task_status_counts"]["IN_PROGRESS"] == 1
    assert data["as_of"].endswith("Z")

    narrowed = client.get(f"/api/web/v1/dashboard?project_id={world.project_b.pk}", **HOST)
    assert narrowed.status_code == 200, narrowed.content
    narrowed_data = json.loads(narrowed.content)["data"]
    assert narrowed_data["task_total"] == 0
    assert narrowed_data["scope"]["project_id"] == str(world.project_b.pk)

    unknown = client.get("/api/web/v1/dashboard?status=TODO", **HOST)
    assert unknown.status_code == 400
    assert json.loads(unknown.content)["error"]["code"] == "UNSUPPORTED_PARAMETER"

    bad_bool = client.get("/api/web/v1/dashboard?include_archived=maybe", **HOST)
    assert bad_bool.status_code == 400
    assert json.loads(bad_bool.content)["error"]["code"] == "INVALID_ARGUMENT"

    missing_project = client.get(f"/api/web/v1/dashboard?project_id={uuid.uuid4()}", **HOST)
    assert missing_project.status_code == 404
    assert json.loads(missing_project.content)["error"]["code"] == "RESOURCE_NOT_FOUND"

    activity = client.get("/api/web/v1/activity", **HOST)
    assert activity.status_code == 200, activity.content
    activity_data = json.loads(activity.content)["data"]
    assert set(activity_data) == {"items", "total", "page", "page_size"}
    assert activity_data["total"] == 1
    claimed_events = [item for item in activity_data["items"] if item["task_id"] == str(task.pk)]
    assert claimed_events and claimed_events[0]["action"] == "claimed"
    assert claimed_events[0]["project_id"] == str(world.project_a.pk)

    narrowed_activity = client.get(
        f"/api/web/v1/activity?project_id={world.project_b.pk}", **HOST
    )
    assert narrowed_activity.status_code == 200
    assert json.loads(narrowed_activity.content)["data"]["total"] == 0

    filtered = client.get(f"/api/web/v1/activity?task_id={uuid.uuid4()}", **HOST)
    assert filtered.status_code == 200
    assert json.loads(filtered.content)["data"] == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
    }

    bad_action = client.get("/api/web/v1/activity?action=", **HOST)
    assert bad_action.status_code == 400
    assert json.loads(bad_action.content)["error"]["code"] == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------------------
# 9b. AgentAPI HTTP surface (real AGENT_TOKEN authentication)
# --------------------------------------------------------------------------------------


@pytest.mark.identity
@pytest.mark.django_db(transaction=True)
def test_agentapi_dashboard_uses_the_token_scope_and_the_frozen_envelope():
    factories.ensure_claim_lock()
    world = factories.World()
    factories.create_task(world.project_a, title="A 待办", difficulty=1)
    factories.create_task(
        world.project_a, title="A 准备中", status=TaskStatus.PREPARING, difficulty=1
    )
    factories.create_task(world.project_b, title="B 待办", difficulty=1)

    from agentapi.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/agent/v1/dashboard", headers={"AGENT_TOKEN": world.w1_key})
        assert response.status_code == 200, response.text
        body = json.loads(response.content)
        assert set(body) == {"data", "request_id"}
        assert response.headers["X-Request-Id"] == body["request_id"]
        data = body["data"]
        assert data["scope"]["actor_type"] == ActorType.AGENT.value
        assert data["scope"]["agent_id"] == str(world.w1.pk)
        assert data["scope"]["task_visibility"] == TASK_VISIBILITY_PUBLISHED_ONLY
        assert data["task_total"] == 1
        assert data["task_status_counts"]["PREPARING"] == 0
        assert str(world.project_b.pk) not in data["scope"]["project_ids"]
        project_ids = {project["project_id"] for project in data["projects"]}
        assert project_ids == {str(world.project_a.pk)}

        supervisor = client.get(
            "/api/agent/v1/dashboard", headers={"AGENT_TOKEN": world.s1_key}
        )
        assert supervisor.status_code == 200, supervisor.text
        supervisor_data = json.loads(supervisor.content)["data"]
        assert supervisor_data["scope"]["task_visibility"] == TASK_VISIBILITY_FULL
        assert supervisor_data["task_status_counts"]["PREPARING"] == 1
        assert supervisor_data["task_total"] == 3  # 项目 A + 项目 B（s1 同时是两者成员）

        missing = client.get("/api/agent/v1/dashboard")
        assert missing.status_code == 401
        assert json.loads(missing.content)["error"]["code"] == "AGENT_TOKEN_MISSING"

        unknown = client.get(
            "/api/agent/v1/dashboard", headers={"AGENT_TOKEN": "agt_not-a-real-key"}
        )
        assert unknown.status_code == 401
        assert json.loads(unknown.content)["error"]["code"] == "AGENT_TOKEN_INVALID"

        foreign = client.get(
            f"/api/agent/v1/dashboard?project_id={world.project_b.pk}",
            headers={"AGENT_TOKEN": world.w1_key},
        )
        assert foreign.status_code == 404
        assert json.loads(foreign.content)["error"]["code"] == "RESOURCE_NOT_FOUND"

        own = client.get(
            f"/api/agent/v1/dashboard?project_id={world.project_a.pk}",
            headers={"AGENT_TOKEN": world.w1_key},
        )
        assert own.status_code == 200, own.text
        own_data = json.loads(own.content)["data"]
        assert own_data["scope"]["project_id"] == str(world.project_a.pk)
        assert own_data["task_total"] == 1

        rejected = client.get(
            "/api/agent/v1/dashboard?l1=1", headers={"AGENT_TOKEN": world.w1_key}
        )
        assert rejected.status_code == 400
        assert json.loads(rejected.content)["error"]["code"] == "INVALID_ARGUMENT"

        duplicated = client.get(
            "/api/agent/v1/dashboard?include_archived=true&include_archived=false",
            headers={"AGENT_TOKEN": world.w1_key},
        )
        assert duplicated.status_code == 400
        assert json.loads(duplicated.content)["error"]["code"] == "INVALID_ARGUMENT"
