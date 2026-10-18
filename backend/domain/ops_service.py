"""Operational visibility and manual recovery (DEVELOP D18/D19, PLAN T34).

Two things must be answerable without reading a log file: *what is stuck* and *how do I
retry it*. Failures are recorded in the append-only activity table (which already carries
the originating ``request_id`` plus task/execution/agent ids), and the outbox keeps its own
attempt/error state, so both are queryable through the management surface.

A failure record is written in its own short transaction *after* the failing transaction
has rolled back, and is best effort: if the database is the thing that is broken, recording
the failure must not replace the original error, and must never turn a committed business
fact into an apparent failure.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from django.db import transaction as db_transaction
from django.db.models import Q

from apps.audit.models import ActivityEvent
from apps.jobs.models import OutboxEvent
from domain.clock import db_now
from domain.contracts import (
    ACTION_CAPACITY_REPAIRED,
    ACTION_LOCK_TIMEOUT,
    ACTION_OUTBOX_MANUAL_RETRY,
    ACTION_RETRY_EXHAUSTED,
    ActorContext,
    ActorType,
    DeliveryState,
    OP_OPS_CAPACITY_AUDIT,
    OP_OPS_OUTBOX_RETRY,
)
from domain.errors import InvalidTransitionError, ResourceNotFoundError
from domain.events import record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.presenters import activity_view, iso

logger = logging.getLogger("domain.ops")

# Actions that represent an operational problem someone may need to act on.
FAILURE_ACTIONS = (ACTION_RETRY_EXHAUSTED, ACTION_LOCK_TIMEOUT, ACTION_CAPACITY_REPAIRED)

# Never surface these into an operational response, even accidentally.
KEY_TOKEN_PATTERN = re.compile(r"\bagt_[A-Za-z0-9_\-]+")
PLACEHOLDER = "<redacted>"


def redact(text: str | None) -> str:
    """Strip key material and private filesystem paths out of operator-visible text.

    Driver errors and stored failure reasons are free-form strings, so a single pasted
    key or an absolute storage path could otherwise reach the management response.
    """
    if not text:
        return ""
    cleaned = KEY_TOKEN_PATTERN.sub("agt_<redacted>", text)
    for candidate in _private_paths():
        if candidate:
            cleaned = cleaned.replace(candidate, PLACEHOLDER)
    return cleaned


def _private_paths() -> tuple[str, ...]:
    from django.conf import settings

    paths = []
    try:
        paths.append(str(settings.FILE_STORAGE_ROOT))
    except Exception:  # pragma: no cover - settings guard
        pass
    try:
        database_name = str(settings.DATABASES["default"].get("NAME") or "")
        if database_name:
            paths.append(database_name)
            parent = str(Path(database_name).parent)
            if parent:
                paths.append(parent)
    except Exception:  # pragma: no cover - settings guard
        pass
    return tuple(sorted({item for item in paths if item}, key=len, reverse=True))


def record_system_failure(
    action: str,
    *,
    reason: str,
    request_id: str = "",
    details: dict[str, Any] | None = None,
    project_id=None,
    task_id=None,
    execution_id=None,
) -> bool:
    """Persist one operational failure record in its own transaction. Best effort."""
    try:
        with db_transaction.atomic():
            record_activity(
                ActorContext(
                    actor_type=ActorType.SYSTEM,
                    request_id=request_id or f"system:{uuid.uuid4()}",
                ),
                action,
                reason=reason[:2000],
                after=_sanitize_details(details),
                project_id=project_id,
                task_id=task_id,
                execution_id=execution_id,
            )
        return True
    except Exception:  # noqa: BLE001 - the original error must survive this
        logger.exception("记录系统失败事件失败（不掩盖原始错误）action=%s", action)
        return False


def _sanitize_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Operational details stay structured and secret-free."""
    from domain.events import _sanitize

    return _sanitize(details or {})


def list_failures(
    actor: ActorContext,
    *,
    page: int = 1,
    page_size: int = 20,
    include_resolved: bool = False,
) -> dict[str, Any]:
    """Paginated view of everything that is currently stuck or that failed and was recorded."""
    events = OutboxEvent.objects.filter(processed_at__isnull=True).filter(
        Q(delivery_state=DeliveryState.NEEDS_ATTENTION.value)
        | Q(delivery_state=DeliveryState.PENDING.value, attempts__gt=0)
    )
    if not include_resolved:
        events = events.exclude(delivery_state=DeliveryState.PROCESSED.value)
    event_rows = list(events.order_by("-created_at", "-id")[:500])

    actions = list(FAILURE_ACTIONS)
    records = ActivityEvent.objects.filter(action__in=actions).order_by("-created_at", "-id")[:500]

    items: list[dict[str, Any]] = []
    for event in event_rows:
        items.append(
            {
                "record_type": "OUTBOX_EVENT",
                "id": str(event.pk),
                "event_type": event.event_type,
                "entity_id": str(event.entity_id),
                "delivery_state": event.delivery_state,
                "attempts": event.attempts,
                "last_error": redact(event.last_error),
                "next_retry_at": iso(event.next_retry_at),
                "published_at": iso(event.published_at),
                "processed_at": iso(event.processed_at),
                "created_at": iso(event.created_at),
                "recoverable": True,
            }
        )
    for record in records:
        view = activity_view(record)
        items.append(
            {
                "record_type": "SYSTEM_FAILURE",
                "id": view["id"],
                "action": view["action"],
                "reason": redact(view["reason"]),
                "request_id": view["request_id"],
                "task_id": view["task_id"],
                "execution_id": view["execution_id"],
                "project_id": view["project_id"],
                "details": view["after"],
                "created_at": view["created_at"],
                "recoverable": False,
            }
        )

    items.sort(key=lambda item: item["created_at"] or "", reverse=True)
    total = len(items)
    offset = (page - 1) * page_size
    window = items[offset : offset + page_size]
    return {"items": window, "total": total, "page": page, "page_size": page_size}


def retry_outbox_event(
    actor: ActorContext, event_id: uuid.UUID, *, idem_key: str
) -> dict[str, Any]:
    """Manually make a stuck event dispatchable again, keeping the original event row."""
    request_hash = canonical_request_hash(
        operation=OP_OPS_OUTBOX_RETRY, resource_id=event_id
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_OPS_OUTBOX_RETRY,
        key=idem_key,
        request_hash=request_hash,
        resource_type="outbox_event",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    event = OutboxEvent.objects.select_for_update().filter(pk=event_id).first()
    if event is None:
        raise ResourceNotFoundError("事件不存在")
    if event.processed_at is not None:
        # A consumed event must never be resurrected: the business effect already happened.
        raise InvalidTransitionError(
            "事件已被消费，不能重新派发",
            details={"event_id": str(event.pk), "processed_at": iso(event.processed_at)},
        )

    now = db_now()
    previous_state = event.delivery_state
    event.delivery_state = DeliveryState.PENDING.value
    event.next_retry_at = now
    event.claimed_at = None
    event.claimed_by = ""
    event.save(
        update_fields=["delivery_state", "next_retry_at", "claimed_at", "claimed_by", "updated_at"]
    )

    record_activity(
        actor,
        ACTION_OUTBOX_MANUAL_RETRY,
        task_id=event.entity_id if event.event_type.startswith("task.") else None,
        before={"delivery_state": previous_state, "attempts": event.attempts},
        after={"delivery_state": event.delivery_state},
        reason="人工重派未处理事件",
    )

    result = {
        "event_id": str(event.pk),
        "event_type": event.event_type,
        "delivery_state": event.delivery_state,
        "attempts": event.attempts,
        "next_retry_at": iso(event.next_retry_at),
        "previous_state": previous_state,
    }
    complete_receipt(receipt, response_data=result, resource_type="outbox_event", resource_id=event.pk)
    return {**result, "idempotent_replay": False}


def run_capacity_audit(actor: ActorContext, *, idem_key: str, limit: int = 500) -> dict[str, Any]:
    """Run the reserved-slot audit on demand and report what it found."""
    from domain.capacity_audit import audit_capacity_slots

    request_hash = canonical_request_hash(operation=OP_OPS_CAPACITY_AUDIT)
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_OPS_CAPACITY_AUDIT,
        key=idem_key,
        request_hash=request_hash,
        resource_type="capacity_audit",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    summary = audit_capacity_slots(limit=limit)
    result = {"audit": summary, "as_of": iso(db_now())}
    complete_receipt(receipt, response_data=result, resource_type="capacity_audit")
    return {**result, "idempotent_replay": False}
