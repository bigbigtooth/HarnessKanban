"""Celery job entrypoints (DEVELOP D18).

Jobs are accelerators, never the source of truth: each one re-reads the database inside a
short transaction, is safe to run concurrently and is idempotent when repeated.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q

from apps.jobs.models import OutboxEvent
from domain.clock import db_now
from domain.contracts import DeliveryState
from domain.errors import DomainError
from domain.service_runner import run_request_service

logger = logging.getLogger("jobs.tasks")

DISPATCH_BATCH = 200
PROCESSING_WAIT_SECONDS = 60


@shared_task(name="jobs.tasks.timeout_scan")
def timeout_scan(limit: int = 200) -> dict:
    """Beat-driven recovery of executions past their fixed deadline."""
    from domain.execution_service import timeout_scan as scan

    result = run_request_service(lambda: scan(limit=limit), require_write_lock=False)
    if result.get("ended"):
        logger.info("超时扫描结束执行 %s 个", result["ended"])
    return result


@shared_task(name="jobs.tasks.outbox_dispatch")
def outbox_dispatch(batch: int = DISPATCH_BATCH) -> dict:
    """Reserve unprocessed events, publish them, and record the outcome."""
    dispatcher_id = f"dispatcher-{uuid.uuid4()}"
    reserved = _reserve_batch(dispatcher_id, batch)
    published = failed = 0
    for event_id in reserved:
        if _publish_one(event_id):
            published += 1
        else:
            failed += 1
    return {"reserved": len(reserved), "published": published, "failed": failed}


def _reserve_batch(dispatcher_id: str, batch: int) -> list:
    """Claim a batch of dispatches with a lease, in a short transaction."""

    def reserve():
        now = db_now()
        stale = now - timedelta(seconds=PROCESSING_WAIT_SECONDS)
        candidates = list(
            OutboxEvent.objects.filter(
                processed_at__isnull=True,
                next_retry_at__lte=now,
            )
            .filter(
                Q(delivery_state=DeliveryState.PENDING.value)
                | Q(delivery_state=DeliveryState.PUBLISHED.value, published_at__lt=stale)
                # A stale dispatch lease must be recoverable after a process exit.
                | Q(delivery_state=DeliveryState.PUBLISHED.value, claimed_at__lt=stale)
            )
            .order_by("created_at", "id")
            .values_list("id", flat=True)[:batch]
        )
        if not candidates:
            return []
        # Claim each event conditionally. A plain bulk update would match
        # ``processed_at IS NULL`` for both dispatchers, so both would reserve the event and
        # both would publish it; filtering on the lease means the loser's UPDATE matches no
        # row after the winner commits.
        claimed: list = []
        for event_id in candidates:
            updated = (
                OutboxEvent.objects.filter(pk=event_id, processed_at__isnull=True)
                .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale))
                .update(claimed_at=now, claimed_by=dispatcher_id)
            )
            if updated == 1:
                claimed.append(event_id)
        return claimed

    return run_request_service(reserve, require_write_lock=False) or []


def _publish_one(event_id) -> bool:
    """Publish one event outside any transaction and record the result conditionally."""
    payload = {"event_id": str(event_id)}
    try:
        _publish_to_broker(payload)
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
        _record_failure(event_id, str(exc))
        return False

    def mark_published():
        now = db_now()
        # Never overwrite a consumer that already finished.
        return OutboxEvent.objects.filter(
            id=event_id, processed_at__isnull=True
        ).update(
            delivery_state=DeliveryState.PUBLISHED.value,
            published_at=now,
            claimed_at=None,
            claimed_by="",
            attempts=OutboxEvent.objects.get(pk=event_id).attempts + 1,
        )

    run_request_service(mark_published, require_write_lock=False)
    return True


def _publish_to_broker(payload: dict) -> None:
    from config.celery_app import app

    app.send_task("jobs.tasks.consume_outbox_event", args=[payload["event_id"]])


def _record_failure(event_id, error: str) -> None:
    from domain.errors import DatabaseBusyError  # noqa: F401  (documented failure class)

    def record():
        event = OutboxEvent.objects.select_for_update().filter(pk=event_id).first()
        if event is None or event.processed_at is not None:
            return 0
        event.attempts = event.attempts + 1
        event.last_error = error[:2000]
        event.claimed_at = None
        event.claimed_by = ""
        backoff = min(2**event.attempts, 300)
        event.next_retry_at = db_now() + timedelta(seconds=backoff)
        from django.conf import settings

        if event.attempts >= settings.HARNESSTASK["outbox_max_attempts"]:
            event.delivery_state = DeliveryState.NEEDS_ATTENTION.value
        event.save(
            update_fields=[
                "attempts",
                "last_error",
                "claimed_at",
                "claimed_by",
                "next_retry_at",
                "delivery_state",
            ]
        )
        return 1

    try:
        run_request_service(record, require_write_lock=False)
    except DomainError:
        logger.exception("记录派发失败时出错 event_id=%s", event_id)


@shared_task(name="jobs.tasks.consume_outbox_event")
def consume_outbox_event(event_id: str) -> dict:
    """Consume one event: the business effect and the processed marker commit together."""

    def consume():
        event = OutboxEvent.objects.select_for_update().filter(pk=event_id).first()
        if event is None:
            return {"status": "missing"}
        if event.processed_at is not None:
            # Already handled: acknowledge without doing the work twice.
            return {"status": "already_processed"}
        _validate_payload(event)
        handler = HANDLERS.get(event.event_type)
        if handler is not None:
            handler(event)
        event.processed_at = db_now()
        event.delivery_state = DeliveryState.PROCESSED.value
        event.save(update_fields=["processed_at", "delivery_state"])
        return {"status": "processed"}

    return run_request_service(consume, require_write_lock=False)


def _validate_payload(event: OutboxEvent) -> None:
    """Never trust a stale task status embedded in a payload."""
    payload = event.payload or {}
    if not isinstance(payload, dict):
        raise ValueError("outbox payload 必须是结构化对象")
    if "status" in payload:
        from apps.tasks.models import Task

        task = Task.objects.filter(pk=event.entity_id).values("status").first()
        if task is not None and payload.get("status") != task.status:
            logger.info(
                "事件 %s 的 payload 状态已过期（payload=%s 当前=%s），以当前数据库状态为准",
                event.pk,
                payload.get("status"),
                task["status"],
            )


def _noop(event: OutboxEvent) -> None:
    """First phase has no external side effect for this event type."""


HANDLERS = {
    "task.claimed": _noop,
    "task.timeout": _noop,
    "task.timeout_recovered": _noop,
    "task.published": _noop,
    "task.release": _noop,
    "task.reclaim": _noop,
    "task.submitted": _noop,
    "task.approved": _noop,
    "task.rejected": _noop,
    "task.reopened": _noop,
    "task.archived": _noop,
    "task.restored": _noop,
    "file.cleanup": _noop,
    "capacity.audit": _noop,
}


@shared_task(name="jobs.tasks.cleanup_idempotency_records")
def cleanup_idempotency_records(limit: int = 500) -> dict:
    from domain.idempotency import touch_expired_cleanup

    removed = run_request_service(lambda: touch_expired_cleanup(limit), require_write_lock=False)
    return {"removed": removed}


@shared_task(name="jobs.tasks.cleanup_temp_files")
def cleanup_temp_files(limit: int = 200) -> dict:
    """Delete only aged, unreferenced staging files; never historical delivery evidence."""
    from domain.file_cleanup import cleanup_unreferenced_temp_files

    return run_request_service(
        lambda: cleanup_unreferenced_temp_files(limit=limit), require_write_lock=False
    )


@shared_task(name="jobs.tasks.capacity_audit")
def capacity_audit(limit: int = 500) -> dict:
    """Compare reserved slots with real open executions; report, never hide, drift."""
    from domain.capacity_audit import audit_capacity_slots

    return run_request_service(lambda: audit_capacity_slots(limit=limit), require_write_lock=False)
