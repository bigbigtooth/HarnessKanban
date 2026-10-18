"""Persistent idempotency receipts (DEVELOP D07).

The receipt is reserved and completed inside the same transaction as the business change,
so a committed success can always be replayed and a rolled-back attempt leaves nothing
behind. Replay is checked *before* new-action state/assignment/capacity/deadline rules:
a submit that already succeeded must keep succeeding after its deadline passed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError

from apps.jobs.models import IdempotencyRecord
from domain.clock import db_now
from domain.contracts import IdempotencyState
from domain.errors import (
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    IdempotencyRaceLost,
)
from domain.transactions import assert_in_transaction

logger = logging.getLogger("domain.idempotency")

# Fields that must never be persisted inside a receipt payload.
SENSITIVE_RECEIPT_KEYS = {"agent_key", "key", "token", "agent_token", "plaintext_key"}


@dataclass
class Receipt:
    """Outcome of the reserve/lookup step for one idempotency key."""

    record: IdempotencyRecord | None
    replayed: bool

    @property
    def response_data(self) -> Any:
        return None if self.record is None else self.record.response_data

    @property
    def http_status(self) -> int:
        return 200 if self.record is None else self.record.http_status

    @property
    def resource_id(self):
        return None if self.record is None else self.record.resource_id


def canonical_request_hash(
    *,
    operation: str,
    body: dict[str, Any] | None = None,
    resource_id: str | None = None,
    project_id: str | None = None,
    levels: list[int] | None = None,
) -> str:
    """Stable digest of the business-meaningful request.

    Deliberately excludes request_id, key material and key version so the same agent may
    retry with a rotated key, while a different level/project/body is detected as reuse.
    """
    payload = {
        "operation": operation,
        "body": _normalize(body),
        "resource_id": _string_or_none(resource_id),
        "project_id": _string_or_none(project_id),
        "levels": sorted(levels) if levels is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _string_or_none(value) -> str | None:
    return None if value is None else str(value)


def _normalize(value):
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return getattr(value, "value")
    return value


def lookup_or_reserve(
    *,
    actor_scope: str,
    operation: str,
    key: str,
    request_hash: str,
    sensitive: bool = False,
    resource_type: str = "",
) -> Receipt:
    """Return an existing receipt for replay, or atomically reserve a new key.

    Must be called inside the outermost transaction, after the permission checks and (for
    claims) after the global lock, but before any business mutation.
    """
    assert_in_transaction()
    if not key or not key.strip():
        raise IdempotencyKeyReusedError("幂等键不能为空", details={"operation": operation})
    key = key.strip()

    existing = IdempotencyRecord.objects.filter(
        actor_scope=actor_scope, operation=operation, key=key
    ).first()

    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyKeyReusedError(
                "同一幂等键被用于不同的请求参数",
                details={"operation": operation, "key": key},
            )
        if existing.state == IdempotencyState.DONE.value:
            return Receipt(record=existing, replayed=True)
        raise IdempotencyInProgressError(details={"operation": operation, "key": key})

    try:
        record = IdempotencyRecord.objects.create(
            actor_scope=actor_scope,
            operation=operation,
            key=key,
            request_hash=request_hash,
            state=IdempotencyState.IN_PROGRESS.value,
            sensitive=sensitive,
            resource_type=resource_type,
            expires_at=db_now() + _retention(),
        )
    except IntegrityError as exc:
        # Another transaction holds the same key. Django has marked this transaction for
        # rollback, so leave it immediately and let the adapter re-read in a fresh one.
        raise IdempotencyRaceLost(actor_scope, operation, key) from exc
    return Receipt(record=record, replayed=False)


def complete_receipt(
    receipt: Receipt,
    *,
    response_data: Any,
    http_status: int = 200,
    resource_type: str = "",
    resource_id=None,
) -> IdempotencyRecord:
    """Store the success receipt in the business transaction."""
    assert_in_transaction()
    record = receipt.record
    if record is None:  # pragma: no cover - defensive
        raise RuntimeError("缺少幂等记录，无法写入成功回执")

    payload = response_data
    if record.sensitive:
        payload = strip_plaintext_key(payload)

    record.response_data = payload
    record.http_status = http_status
    record.state = IdempotencyState.DONE.value
    if resource_type:
        record.resource_type = resource_type
    if resource_id is not None:
        record.resource_id = resource_id
    record.save(
        update_fields=["response_data", "http_status", "state", "resource_type", "resource_id"]
    )
    return record


def strip_plaintext_key(payload: Any) -> Any:
    """Remove plaintext key material from a receipt while keeping the rest of the result."""
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            if key in SENSITIVE_RECEIPT_KEYS:
                continue
            clean[key] = strip_plaintext_key(value)
        if any(key in payload for key in SENSITIVE_RECEIPT_KEYS):
            clean["key_delivery_unavailable"] = True
        return clean
    if isinstance(payload, list):
        return [strip_plaintext_key(item) for item in payload]
    return payload


def read_winning_receipt(actor_scope: str, operation: str, key: str) -> IdempotencyRecord | None:
    """Read the winning receipt in a fresh transaction after losing an idempotency race."""
    return IdempotencyRecord.objects.filter(
        actor_scope=actor_scope, operation=operation, key=key
    ).first()


def touch_expired_cleanup(limit: int = 500) -> int:
    """Delete expired receipts. Execution and result history is never removed here."""
    from django.db import transaction as db_transaction

    cutoff = db_now()
    with db_transaction.atomic():
        ids = list(
            IdempotencyRecord.objects.filter(expires_at__lt=cutoff)
            .order_by("expires_at")
            .values_list("id", flat=True)[:limit]
        )
        if not ids:
            return 0
        return IdempotencyRecord.objects.filter(id__in=ids).delete()[0]


def _retention():
    from datetime import timedelta

    from django.conf import settings

    days = settings.HARNESSTASK["idempotency_retention_days"]
    return timedelta(days=days)
