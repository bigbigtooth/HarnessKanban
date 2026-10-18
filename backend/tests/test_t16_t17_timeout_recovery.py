"""T16/T17 acceptance: fixed timeout, progress, release, reclaim and system recovery."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import OutboxEvent
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now, override_db_clock
from domain.contracts import (
    DIFFICULTY_TIMEOUT_SECONDS,
    EndReason,
    TaskStatus,
    parse_level_filters,
)
from domain.errors import (
    ExecutionNotCurrentError,
    ExecutionTimeoutError,
    InvalidArgumentError,
    RoleForbiddenError,
    VersionConflictError,
)
from domain.execution_service import end_execution, timeout_scan, update_progress
from domain.service_runner import run_request_service
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.timeout, pytest.mark.workflow]


def new_key() -> str:
    return str(uuid.uuid4())


def _claimed_task(world, *, difficulty: int = 2, agent=None, offset_seconds: int = 0):
    """Claim a task for real, optionally back-dating the claim to create a timed-out slot."""
    from domain.claim_service import ClaimRequest, claim_task

    agent = agent or world.w1
    task = factories.create_task(world.project_a, difficulty=difficulty)
    result = run_request_service(
        lambda: claim_task(agent_actor(agent), ClaimRequest(levels=[difficulty]), new_key()),
        global_claim_lock=True,
    )
    assert result["data"] is not None
    execution = TaskExecution.objects.get(pk=result["data"]["execution_id"])
    if offset_seconds:
        moved_claim = execution.claimed_at - timedelta(seconds=offset_seconds)
        # Move the whole window back so the deadline arithmetic stays consistent.
        TaskExecution.objects.filter(pk=execution.pk).update(
            claimed_at=moved_claim,
            execution_deadline_at=moved_claim
            + timedelta(seconds=execution.execution_timeout_seconds),
        )
        execution.refresh_from_db()
    task.refresh_from_db()
    return task, execution


def test_timeout_constants_are_the_confirmed_five_levels():
    assert DIFFICULTY_TIMEOUT_SECONDS == {1: 900, 2: 1800, 3: 7200, 4: 28800, 5: 86400}
    ordered = [DIFFICULTY_TIMEOUT_SECONDS[level] for level in range(1, 6)]
    assert ordered == sorted(ordered)
    assert len(set(ordered)) == 5


def test_level_parser_covers_the_documented_cases():
    assert parse_level_filters([]) == [1, 2, 3, 4, 5]
    assert parse_level_filters([("l1", "1"), ("l2", "1")]) == [1, 2]
    assert parse_level_filters([("l5", "1")]) == [5]
    assert parse_level_filters([("l1", "0"), ("l2", "0")]) == []
    assert parse_level_filters([("l3", "1"), ("l1", "1")]) == [1, 3]

    with pytest.raises(InvalidArgumentError):
        parse_level_filters([("l1", "1"), ("l1", "0")])
    with pytest.raises(InvalidArgumentError):
        parse_level_filters([("l1", "2")])
    with pytest.raises(InvalidArgumentError):
        parse_level_filters([("l0", "1")])
    with pytest.raises(InvalidArgumentError):
        parse_level_filters([("l6", "1")])


def test_progress_does_not_extend_the_deadline_or_bump_the_task_version(db):
    world = factories.World()
    task, execution = _claimed_task(world)
    before_deadline = execution.execution_deadline_at
    before_version = task.version

    result = update_progress(
        agent_actor(world.w1),
        task_id=task.pk,
        execution_id=execution.pk,
        message="已完成一半",
        percent=50,
        idem_key=new_key(),
    )

    assert result["data"]["progress"]["progress_percent"] == 50
    execution.refresh_from_db()
    task.refresh_from_db()
    assert execution.execution_deadline_at == before_deadline
    assert execution.progress_message == "已完成一半"
    assert task.version == before_version
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk


def test_progress_percent_100_does_not_complete_the_task(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    update_progress(
        agent_actor(world.w1),
        task_id=task.pk,
        execution_id=execution.pk,
        message="全部完成",
        percent=100,
        idem_key=new_key(),
    )

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk


def test_progress_at_or_after_deadline_is_rejected(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    with override_db_clock(execution.execution_deadline_at):
        with pytest.raises(ExecutionTimeoutError):
            update_progress(
                agent_actor(world.w1),
                task_id=task.pk,
                execution_id=execution.pk,
                message="太晚了",
                percent=None,
                idem_key=new_key(),
            )

    execution.refresh_from_db()
    assert execution.progress_message == ""


def test_progress_from_another_agent_is_rejected(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    with pytest.raises(ExecutionNotCurrentError):
        update_progress(
            agent_actor(world.w2),
            task_id=task.pk,
            execution_id=execution.pk,
            message="别人的执行",
            percent=None,
            idem_key=new_key(),
        )


def test_stale_execution_id_is_rejected(db):
    world = factories.World()
    task, execution = _claimed_task(world)
    stale_id = uuid.uuid4()

    with pytest.raises(ExecutionNotCurrentError):
        update_progress(
            agent_actor(world.w1),
            task_id=task.pk,
            execution_id=stale_id,
            message="旧执行",
            percent=None,
            idem_key=new_key(),
        )


def test_release_returns_task_to_todo_and_releases_one_slot(db):
    world = factories.World()
    task, execution = _claimed_task(world)
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1

    end_execution(
        agent_actor(world.w1),
        task_id=task.pk,
        execution_id=execution.pk,
        reason=EndReason.RELEASE,
        reason_text="今天做不完",
        idem_key=new_key(),
    )

    task.refresh_from_db()
    execution.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.active_execution_id is None
    assert task.assignee_agent_id is None
    assert task.claimed_at is None
    assert execution.ended_at is not None
    assert execution.end_reason == EndReason.RELEASE.value
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    assert ActivityEvent.objects.filter(task=task, action="task.release").count() == 1
    assert OutboxEvent.objects.filter(entity_id=task.pk, event_type="task.release").count() == 1


def test_repeat_recovery_does_not_release_the_slot_twice(db):
    world = factories.World()
    task, execution = _claimed_task(world)
    key = new_key()

    end_execution(
        agent_actor(world.w1),
        task_id=task.pk,
        execution_id=execution.pk,
        reason=EndReason.RELEASE,
        reason_text="释放",
        idem_key=key,
    )
    # Same key -> replay, not a second end.
    replay = end_execution(
        agent_actor(world.w1),
        task_id=task.pk,
        execution_id=execution.pk,
        reason=EndReason.RELEASE,
        reason_text="释放",
        idem_key=key,
    )
    assert replay["idempotent_replay"] is True

    agent = Agent.objects.get(pk=world.w1.pk)
    assert agent.reserved_execution_count == 0
    assert ActivityEvent.objects.filter(task=task, action="task.release").count() == 1
    assert TaskExecution.objects.filter(task=task).count() == 1


def test_release_requires_a_reason(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    with pytest.raises(InvalidArgumentError):
        end_execution(
            agent_actor(world.w1),
            task_id=task.pk,
            execution_id=execution.pk,
            reason=EndReason.RELEASE,
            reason_text="   ",
            idem_key=new_key(),
        )


def test_supervisor_can_reclaim_and_a_worker_cannot(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    with pytest.raises(RoleForbiddenError):
        end_execution(
            agent_actor(world.w2),
            task_id=task.pk,
            execution_id=execution.pk,
            reason=EndReason.RECLAIM,
            reason_text="抢别人的活",
            idem_key=new_key(),
        )

    end_execution(
        agent_actor(world.s1),
        task_id=task.pk,
        execution_id=execution.pk,
        reason=EndReason.RECLAIM,
        reason_text="重新安排",
        idem_key=new_key(),
    )
    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0


def test_reclaim_keeps_the_assigned_designation(db):
    from domain.claim_service import ClaimRequest, claim_task

    world = factories.World()
    task = factories.create_task(
        world.project_a,
        difficulty=2,
        claim_mode="ASSIGNED",
        designated_agent=world.w1,
    )
    claimed = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[2]), new_key()),
        global_claim_lock=True,
    )
    execution_id = claimed["data"]["execution_id"]

    end_execution(
        agent_actor(world.s1),
        task_id=task.pk,
        execution_id=uuid.UUID(execution_id),
        reason=EndReason.RECLAIM,
        reason_text="超时改派前先回收",
        idem_key=new_key(),
    )

    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.claim_mode == "ASSIGNED"
    assert task.designated_agent_id == world.w1.pk


def test_timeout_scan_recovers_only_expired_executions_and_is_repeatable(db):
    world = factories.World()
    fresh_task, fresh_execution = _claimed_task(world, agent=world.w2)
    stale_task, stale_execution = _claimed_task(world, agent=world.w1, offset_seconds=4000)

    result = timeout_scan(limit=50)
    assert result["ended"] == 1

    stale_task.refresh_from_db()
    stale_execution.refresh_from_db()
    assert stale_execution.ended_at is not None
    assert stale_execution.end_reason == EndReason.TIMEOUT.value
    assert stale_task.status == TaskStatus.TODO.value
    assert stale_task.active_execution_id is None
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0

    fresh_task.refresh_from_db()
    fresh_execution.refresh_from_db()
    assert fresh_execution.ended_at is None
    assert fresh_task.status == TaskStatus.IN_PROGRESS.value
    assert Agent.objects.get(pk=world.w2.pk).reserved_execution_count == 1

    # A second scan must not touch anything again.
    second = timeout_scan(limit=50)
    assert second["ended"] == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    assert ActivityEvent.objects.filter(task=stale_task, action="task.timeout").count() == 1


def test_timeout_end_cannot_be_forced_before_the_deadline(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    with pytest.raises(VersionConflictError):
        end_execution(
            agent_actor(world.w1),
            task_id=task.pk,
            execution_id=execution.pk,
            reason=EndReason.TIMEOUT,
            reason_text="",
            idem_key=new_key(),
        )


def test_ended_execution_cannot_write_progress(db):
    world = factories.World()
    task, execution = _claimed_task(world)
    end_execution(
        agent_actor(world.w1),
        task_id=task.pk,
        execution_id=execution.pk,
        reason=EndReason.RELEASE,
        reason_text="释放",
        idem_key=new_key(),
    )

    with pytest.raises(ExecutionNotCurrentError):
        update_progress(
            agent_actor(world.w1),
            task_id=task.pk,
            execution_id=execution.pk,
            message="复活",
            percent=None,
            idem_key=new_key(),
        )
    execution.refresh_from_db()
    assert execution.progress_message == ""


@pytest.mark.claim
def test_stale_agent_progress_cannot_touch_a_new_execution(db):
    """A18: the old execution must not resurrect or overwrite the new holder's state."""
    from domain.claim_service import ClaimRequest, claim_task

    world = factories.World()
    task, old_execution = _claimed_task(world)
    end_execution(
        agent_actor(world.s1),
        task_id=task.pk,
        execution_id=old_execution.pk,
        reason=EndReason.RECLAIM,
        reason_text="回收",
        idem_key=new_key(),
    )

    # w1 lost access to the execution; w1 must first be able to claim again.
    new_claim = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[2]), new_key()),
        global_claim_lock=True,
    )
    new_execution_id = uuid.UUID(new_claim["data"]["execution_id"])
    assert new_execution_id != old_execution.pk

    with pytest.raises(ExecutionNotCurrentError):
        update_progress(
            agent_actor(world.w1),
            task_id=task.pk,
            execution_id=old_execution.pk,
            message="旧执行回写",
            percent=None,
            idem_key=new_key(),
        )

    new_execution = TaskExecution.objects.get(pk=new_execution_id)
    assert new_execution.progress_message == ""
    assert new_execution.ended_at is None


def test_expired_deadline_boundary_uses_database_time(db):
    world = factories.World()
    task, execution = _claimed_task(world)

    just_before = execution.execution_deadline_at - timedelta(microseconds=1)
    with override_db_clock(just_before):
        result = update_progress(
            agent_actor(world.w1),
            task_id=task.pk,
            execution_id=execution.pk,
            message="刚好赶上",
            percent=99,
            idem_key=new_key(),
        )
        assert result["data"]["progress"]["progress_percent"] == 99

    with override_db_clock(execution.execution_deadline_at):
        with pytest.raises(ExecutionTimeoutError):
            update_progress(
                agent_actor(world.w1),
                task_id=task.pk,
                execution_id=execution.pk,
                message="等于截止时刻",
                percent=100,
                idem_key=new_key(),
            )
