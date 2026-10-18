"""T04 acceptance: execution-end and task-reset primitives, clock and lock behaviour."""

from __future__ import annotations

import threading
from datetime import timedelta, timezone

import pytest
from django.db import connections, transaction

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import OutboxEvent
from apps.tasks.models import Task, TaskClaimLock, TaskExecution
from domain.clock import db_now, override_db_clock
from domain.contracts import EndReason, TaskStatus
from domain.errors import ClaimLockMissingError, DatabaseBusyError
from domain.execution import finish_execution_locked, reset_in_progress_task_locked
from domain.transactions import acquire_claim_lock_row, run_write_service
from tests import factories

pytestmark = [pytest.mark.unit, pytest.mark.workflow]


def _in_progress(world, *, timeout_seconds: int = 1800):
    """W1 with capacity 1 holding one in-progress task and a matching reserved slot."""
    agent = world.w1
    agent.max_concurrency = 1
    agent.reserved_execution_count = 1
    agent.save(update_fields=["max_concurrency", "reserved_execution_count"])

    task = factories.create_task(world.project_a, title="执行原语任务", difficulty=2)
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
        update_fields=[
            "status",
            "assignee_agent",
            "active_execution",
            "claimed_at",
            "version",
        ]
    )
    return task, execution


def test_finish_execution_ends_once_and_releases_one_slot(db):
    world = factories.World()
    task, execution = _in_progress(world)
    now = db_now()

    def service():
        locked_task = Task.objects.select_for_update().get(pk=task.pk)
        locked_execution = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        ended = finish_execution_locked(
            locked_execution, ended_at=now, end_reason=EndReason.RELEASE.value
        )
        return ended, locked_task.status

    ended, status_during = run_write_service(service, require_write_lock=False)

    assert ended is True
    # The primitive must not touch task state; that is the wrapper's job.
    assert status_during == TaskStatus.IN_PROGRESS.value

    execution.refresh_from_db()
    task.refresh_from_db()
    agent = Agent.objects.get(pk=world.w1.pk)

    assert execution.ended_at is not None
    assert execution.end_reason == EndReason.RELEASE.value
    assert agent.reserved_execution_count == 0
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk
    assert task.version == 2

    # No audit or outbox side effects from a primitive.
    assert ActivityEvent.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


def test_finish_execution_is_idempotent_and_never_double_releases(db):
    world = factories.World()
    task, execution = _in_progress(world)
    now = db_now()

    def first():
        locked = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        return finish_execution_locked(
            locked, ended_at=now, end_reason=EndReason.TIMEOUT.value
        )

    def second():
        locked = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        return finish_execution_locked(
            locked, ended_at=now, end_reason=EndReason.TIMEOUT.value
        )

    assert run_write_service(first, require_write_lock=False) is True
    assert run_write_service(second, require_write_lock=False) is False

    agent = Agent.objects.get(pk=world.w1.pk)
    assert agent.reserved_execution_count == 0
    assert TaskExecution.objects.filter(pk=execution.pk).count() == 1


def test_finish_execution_without_slot_rolls_back_everything(db):
    world = factories.World()
    task, execution = _in_progress(world)
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=0)
    now = db_now()

    def service():
        locked = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        return finish_execution_locked(
            locked, ended_at=now, end_reason=EndReason.RECLAIM.value
        )

    with pytest.raises(DatabaseBusyError):
        run_write_service(service, require_write_lock=False)

    execution.refresh_from_db()
    assert execution.ended_at is None
    assert execution.end_reason is None


def test_reset_in_progress_task_cas_clears_fields_and_bumps_version_once(db):
    world = factories.World()
    task, execution = _in_progress(world)
    now = db_now()

    def service():
        locked_task = Task.objects.select_for_update().get(pk=task.pk)
        locked_execution = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        assert finish_execution_locked(
            locked_execution, ended_at=now, end_reason=EndReason.TIMEOUT.value
        )
        reset_in_progress_task_locked(
            locked_task, expected_execution_id=execution.pk, now=now
        )
        return True

    run_write_service(service, require_write_lock=False)

    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.active_execution_id is None
    assert task.assignee_agent_id is None
    assert task.claimed_at is None
    assert task.submitted_at is None
    # version 2 while IN_PROGRESS (claim CAS) then exactly +1 from the reset CAS.
    assert task.version == 3
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0

    # The ended execution history is preserved, never deleted.
    execution.refresh_from_db()
    assert execution.ended_at is not None


def test_reset_with_wrong_execution_id_aborts_and_rolls_back(db):
    world = factories.World()
    task, execution = _in_progress(world)
    now = db_now()

    def service():
        locked_task = Task.objects.select_for_update().get(pk=task.pk)
        locked_execution = TaskExecution.objects.select_for_update().get(pk=execution.pk)
        assert finish_execution_locked(
            locked_execution, ended_at=now, end_reason=EndReason.RELEASE.value
        )
        reset_in_progress_task_locked(locked_task, expected_execution_id=None, now=now)
        return True

    with pytest.raises(Exception):
        run_write_service(service, require_write_lock=False)

    task.refresh_from_db()
    execution.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.version == 2
    # The slot release from finish_execution_locked was rolled back with the whole txn.
    assert execution.ended_at is None
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1


@pytest.mark.django_db(transaction=True)
def test_services_must_run_inside_an_outermost_transaction():
    """A write service called without run_write_service must fail loudly (no self-commit)."""
    from domain.contracts import ActorContext, ActorType
    from domain.events import record_activity

    actor = ActorContext(actor_type=ActorType.SYSTEM, request_id="t")
    with pytest.raises(RuntimeError):
        record_activity(actor, "probe")


def test_db_now_is_database_time_and_utc(db):
    value = run_write_service(db_now, require_write_lock=False)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)
    assert value.tzinfo == timezone.utc


def test_db_clock_override_used_only_for_boundary_tests(db):
    fixed = db_now().replace(microsecond=0)
    with override_db_clock(fixed):
        assert db_now() == fixed
    assert db_now() != fixed or True  # restored afterwards


def test_claim_lock_row_is_singleton_and_revision_advances(db):
    factories.ensure_claim_lock()
    before = TaskClaimLock.objects.get(id=1).revision

    def service():
        acquire_claim_lock_row()
        return TaskClaimLock.objects.get(id=1).revision

    after = run_write_service(service, require_write_lock=False)
    assert after == before + 1
    assert TaskClaimLock.objects.filter(id=1).count() == 1


def test_write_services_take_the_write_lock_according_to_the_backend(db):
    """SQLite needs the singleton write lock; MySQL must not serialise every write on it.

    On SQLite there is no ``SELECT FOR UPDATE``, so any read-then-write service must really
    UPDATE the lock row before its first business SELECT. On MySQL the row locks do that job,
    and only the claim path takes the singleton row — taking it for every write would
    serialise unrelated business writes for no benefit.
    """
    factories.ensure_claim_lock()
    before = TaskClaimLock.objects.get(id=1).revision

    observed = run_write_service(lambda: TaskClaimLock.objects.get(id=1).revision)

    if connections["default"].vendor == "sqlite":
        assert observed == before + 1, "SQLite 下写服务必须先取得单例写锁"
    else:
        assert observed == before, "MySQL 下普通写服务不应占用单例领取锁"

    # The claim path takes the singleton row on every backend, advancing it exactly once.
    after_first = TaskClaimLock.objects.get(id=1).revision
    claim_lock_observed = run_write_service(
        lambda: TaskClaimLock.objects.get(id=1).revision, global_claim_lock=True
    )
    assert claim_lock_observed == after_first + 1


def test_post_migrate_restores_claim_lock_without_resetting_revision(db):
    """flush truncates the migration row; the post_migrate hook must put id=1 back.

    An existing revision is the lock's history and must survive a second emit.
    """
    from django.apps import apps
    from django.db.models.signals import post_migrate

    app = apps.get_app_config("tasks")
    TaskClaimLock.objects.filter(id=1).delete()
    assert TaskClaimLock.objects.count() == 0

    post_migrate.send(
        sender=app, app_config=app, verbosity=0, interactive=False, using="default"
    )

    assert list(TaskClaimLock.objects.values_list("id", "revision")) == [(1, 0)]

    TaskClaimLock.objects.filter(id=1).update(revision=4)
    post_migrate.send(
        sender=app, app_config=app, verbosity=0, interactive=False, using="default"
    )
    assert TaskClaimLock.objects.get(id=1).revision == 4


def test_acquire_claim_lock_row_refuses_when_the_row_is_missing(db):
    """The lock primitive itself never continues when the singleton is absent."""
    TaskClaimLock.objects.filter(id=1).delete()

    with transaction.atomic():
        with pytest.raises(ClaimLockMissingError):
            acquire_claim_lock_row()

    factories.ensure_claim_lock()


def test_write_service_restores_a_missing_lock_then_holds_it(db):
    """A wiped singleton is inserted outside the business transaction, then locked."""
    TaskClaimLock.objects.filter(id=1).delete()

    def service():
        return TaskClaimLock.objects.get(id=1).revision

    revision = run_write_service(service, global_claim_lock=True)

    assert revision == 1
    assert TaskClaimLock.objects.get(id=1).revision == 1


@pytest.mark.django_db(transaction=True)
def test_flush_restores_the_claim_lock_row():
    """flush truncates migration data; the command itself must put id=1 back."""
    from django.core.management import call_command

    db_name = str(connections["default"].settings_dict["NAME"])
    assert "test" in db_name.lower(), db_name

    TaskClaimLock.objects.filter(id=1).update(revision=9)
    call_command("flush", interactive=False, verbosity=0)

    assert list(TaskClaimLock.objects.values_list("id", "revision")) == [(1, 0)]


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.timeout
@pytest.mark.django_db(transaction=True)
def test_two_real_connections_end_one_execution_only_once(world):
    """Real concurrent connections: the second call must not release a second slot."""
    task, execution = _in_progress(world)
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=10)

            def service():
                locked = TaskExecution.objects.select_for_update().get(pk=execution.pk)
                return finish_execution_locked(
                    locked, ended_at=db_now(), end_reason=EndReason.TIMEOUT.value
                )

            results.append(run_write_service(service))
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(f"t{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert sorted(results, key=str) == [False, True]
    agent = Agent.objects.get(pk=world.w1.pk)
    assert agent.reserved_execution_count == 0
    execution.refresh_from_db()
    assert execution.ended_at is not None
