"""T26 acceptance: outbox dispatch state machine and idempotent consumption.

The broker is the only thing replaced in these tests (an external dependency); every state
transition under test runs against the real database rows.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from apps.jobs.models import OutboxEvent
from apps.tasks.models import Task
from domain.clock import db_now
from domain.contracts import DeliveryState
from domain.events import enqueue_outbox
from domain.service_runner import run_request_service
from jobs import tasks as job_tasks
from tests import factories

pytestmark = [pytest.mark.jobs, pytest.mark.fault]


def commit_event(entity_id=None, event_type: str = "task.claimed") -> OutboxEvent:
    entity = entity_id or uuid.uuid4()
    return run_request_service(
        lambda: enqueue_outbox(event_type, entity, {"task_id": str(entity)}),
        require_write_lock=False,
    )


def test_business_event_survives_a_failed_wakeup(db, monkeypatch):
    """A committed business fact must never be reported as uncommitted because Redis is down."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)

    def boom(payload):
        raise RuntimeError("redis 不可用")

    monkeypatch.setattr(job_tasks, "_publish_to_broker", boom)
    event = commit_event(task.pk)

    result = job_tasks.outbox_dispatch(batch=10)

    assert result["failed"] == 1
    event.refresh_from_db()
    assert event.processed_at is None
    assert event.delivery_state == DeliveryState.PENDING.value
    assert event.attempts == 1
    assert "redis 不可用" in event.last_error
    assert event.next_retry_at is not None
    assert Task.objects.filter(pk=task.pk).exists()


def test_dispatch_marks_published_and_consumer_marks_processed_once(db, monkeypatch):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    monkeypatch.setattr(job_tasks, "_publish_to_broker", lambda payload: None)
    event = commit_event(task.pk, event_type="task.published")

    dispatch = job_tasks.outbox_dispatch(batch=10)
    assert dispatch["published"] == 1

    event.refresh_from_db()
    assert event.delivery_state == DeliveryState.PUBLISHED.value
    assert event.published_at is not None
    assert event.processed_at is None

    first = job_tasks.consume_outbox_event(str(event.pk))
    assert first["status"] == "processed"
    event.refresh_from_db()
    assert event.delivery_state == DeliveryState.PROCESSED.value
    assert event.processed_at is not None
    processed_at = event.processed_at

    # A redelivered message is acknowledged without doing the work again.
    second = job_tasks.consume_outbox_event(str(event.pk))
    assert second["status"] == "already_processed"
    event.refresh_from_db()
    assert event.processed_at == processed_at


def test_failure_after_success_does_not_overwrite_the_processed_marker(db, monkeypatch):
    """The dispatcher must never turn a processed event back into a failed one."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    monkeypatch.setattr(job_tasks, "_publish_to_broker", lambda payload: None)
    event = commit_event(task.pk)
    job_tasks.outbox_dispatch(batch=10)
    job_tasks.consume_outbox_event(str(event.pk))
    event.refresh_from_db()
    assert event.delivery_state == DeliveryState.PROCESSED.value

    job_tasks._record_failure(event.pk, "迟到的派发失败")

    event.refresh_from_db()
    assert event.delivery_state == DeliveryState.PROCESSED.value
    assert event.last_error == ""
    assert event.attempts == 1


def test_repeated_failures_escalate_to_needs_attention(db, monkeypatch):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)

    def boom(payload):
        raise RuntimeError("broker 持续失败")

    monkeypatch.setattr(job_tasks, "_publish_to_broker", boom)
    event = commit_event(task.pk)
    from django.conf import settings

    max_attempts = settings.HARNESSTASK["outbox_max_attempts"]
    for _ in range(max_attempts):
        # Make the event due again so each round is attempted.
        OutboxEvent.objects.filter(pk=event.pk).update(next_retry_at=db_now() - timedelta(seconds=1))
        job_tasks.outbox_dispatch(batch=10)

    event.refresh_from_db()
    assert event.attempts == max_attempts
    assert event.delivery_state == DeliveryState.NEEDS_ATTENTION.value
    # The event is retained for manual recovery, never dropped.
    assert OutboxEvent.objects.filter(pk=event.pk).exists()
    assert event.processed_at is None


def test_stale_dispatch_lease_is_recoverable_after_a_process_exit(db, monkeypatch):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    monkeypatch.setattr(job_tasks, "_publish_to_broker", lambda payload: None)
    event = commit_event(task.pk)

    # Simulate a dispatcher that reserved the event and then died.
    stale = db_now() - timedelta(seconds=job_tasks.PROCESSING_WAIT_SECONDS + 60)
    OutboxEvent.objects.filter(pk=event.pk).update(
        delivery_state=DeliveryState.PUBLISHED.value,
        published_at=stale,
        claimed_at=stale,
        claimed_by="dead-dispatcher",
        next_retry_at=stale,
    )

    result = job_tasks.outbox_dispatch(batch=10)
    assert result["reserved"] == 1
    assert result["published"] == 1
    event.refresh_from_db()
    assert event.published_at is not None
    assert event.published_at > stale


def test_outbox_carries_no_secret_material(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    event = run_request_service(
        lambda: enqueue_outbox(
            "task.claimed",
            task.pk,
            {
                "task_id": str(task.pk),
                "agent_key": "agt_should-not-be-here",
                "content": "完整结果正文不应入库",
                "nested": {"token": "secret", "task_id": str(task.pk)},
            },
        ),
        require_write_lock=False,
    )

    event.refresh_from_db()
    assert "agent_key" not in event.payload
    assert "content" not in event.payload
    assert "token" not in event.payload["nested"]
    assert event.payload["task_id"] == str(task.pk)
