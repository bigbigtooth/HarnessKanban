"""Activity history and transactional outbox writers.

Both are written inside the same transaction as the business change they describe, so a
committed business fact always has its history entry and its event. Payloads never carry
key material, file contents or full sensitive request bodies.
"""

from __future__ import annotations

import logging
from typing import Any

from apps.audit.models import ActivityEvent
from apps.jobs.models import OutboxEvent
from domain.clock import db_now
from domain.contracts import ActorContext, DeliveryState
from domain.transactions import assert_in_transaction

logger = logging.getLogger("domain.events")

SENSITIVE_PAYLOAD_KEYS = {
    "key",
    "agent_token",
    "token",
    "key_hash",
    "password",
    "secret",
    "content",
    "background_snapshot",
}

# Exact-name matching alone is not enough: a payload field named ``agent_key``,
# ``api_token`` or ``result_content`` would slip through. Any key containing one of these
# fragments is dropped, so a new field name cannot silently start leaking secrets.
SENSITIVE_KEY_FRAGMENTS = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
    "content",
    "authorization",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_PAYLOAD_KEYS:
        return True
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def _sanitize(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Drop sensitive keys and bound nesting so payloads stay structured, not secret."""
    if not payload:
        return {}
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if _is_sensitive_key(key):
            continue
        if isinstance(value, dict):
            clean[key] = _sanitize(value)
        elif isinstance(value, (list, tuple)):
            clean[key] = [
                _sanitize(item) if isinstance(item, dict) else _plain(item) for item in value
            ]
        else:
            clean[key] = _plain(value)
    return clean


def _plain(value: Any) -> Any:
    import uuid
    from datetime import datetime
    from decimal import Decimal
    from enum import Enum

    if isinstance(value, (uuid.UUID, datetime, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def record_activity(
    actor: ActorContext,
    action: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
    project_id=None,
    task_id=None,
    execution_id=None,
    submission_id=None,
) -> ActivityEvent:
    """Append one activity event. Replays must not call this a second time."""
    assert_in_transaction()
    return ActivityEvent.objects.create(
        project_id=project_id,
        task_id=task_id,
        execution_id=execution_id,
        submission_id=submission_id,
        actor_type=actor.actor_type.value,
        actor_agent_id=actor.agent_id,
        action=action,
        before=_sanitize(before),
        after=_sanitize(after),
        reason=reason,
        request_id=actor.request_id,
    )


def enqueue_outbox(event_type: str, entity_id, payload: dict[str, Any] | None = None) -> OutboxEvent:
    """Persist an event in the business transaction; dispatch happens after commit."""
    assert_in_transaction()
    return OutboxEvent.objects.create(
        event_type=event_type,
        entity_id=entity_id,
        payload=_sanitize(payload),
        delivery_state=DeliveryState.PENDING.value,
        next_retry_at=db_now(),
    )
