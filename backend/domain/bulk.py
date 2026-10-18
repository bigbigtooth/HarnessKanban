"""批量管理动作的共享原语（WebAPI 管理面）。

批量删除/恢复对每个目标独立处理：每项一个 savepoint，失败只回滚该项并把
业务错误码与原因记进结果，一项失败不影响其他项（A23「错误清晰且可重试」）。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from django.db import transaction

from domain.contracts import ActorContext
from domain.errors import DomainError, InvalidArgumentError

# 单次批量请求的目标上限：够用且能保证关键区（行锁 + 活动事件）时长有界。
BULK_MAX_ITEMS = 200


def parse_bulk_ids(payload: dict[str, Any], field: str) -> list[uuid.UUID]:
    """解析批量目标 ID：1..BULK_MAX_ITEMS 个合法 UUID，去重后保持原顺序。"""
    raw = payload.get(field)
    if not isinstance(raw, list) or not raw:
        raise InvalidArgumentError(
            f"{field} 必须是非空数组", details={"field": field, "max_items": BULK_MAX_ITEMS}
        )
    if len(raw) > BULK_MAX_ITEMS:
        raise InvalidArgumentError(
            f"一次最多处理 {BULK_MAX_ITEMS} 项",
            details={"field": field, "max_items": BULK_MAX_ITEMS, "received": len(raw)},
        )
    parsed_ids: list[uuid.UUID] = []
    for item in raw:
        if not isinstance(item, str):
            raise InvalidArgumentError("ID 必须是字符串", details={"field": field, "value": item})
        try:
            parsed = uuid.UUID(item)
        except ValueError as exc:
            raise InvalidArgumentError(
                "ID 必须是合法 UUID", details={"field": field, "value": item}
            ) from exc
        if parsed not in parsed_ids:
            parsed_ids.append(parsed)
    return parsed_ids


class BulkOutcome:
    """逐项结果收集器。"""

    def __init__(self, requested: int) -> None:
        self.requested = requested
        self.succeeded: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def add_success(self, item_id, data: dict[str, Any]) -> None:
        self.succeeded.append(
            {
                "id": str(item_id),
                "deleted_at": data.get("deleted_at"),
                "status": data.get("status"),
                "idempotent_replay": bool(data.get("idempotent_replay")),
            }
        )

    def add_failure(self, item_id, error: DomainError) -> None:
        self.failed.append(
            {
                "id": str(item_id),
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        )


def run_bulk(
    ids: list[uuid.UUID],
    callback: Callable[[uuid.UUID, int, str | None], dict[str, Any]],
    *,
    key_prefix: str | None,
) -> BulkOutcome:
    """逐项执行：每项一个 savepoint；业务错误记入失败项，幂等竞争继续向外抛出。"""
    outcome = BulkOutcome(len(ids))
    for index, item_id in enumerate(ids):
        item_key = f"{key_prefix}:{item_id}" if key_prefix else None
        try:
            with transaction.atomic():
                data = callback(item_id, index, item_key)
            outcome.add_success(item_id, data)
        except DomainError as exc:
            outcome.add_failure(item_id, exc)
    return outcome


def bulk_result(actor: ActorContext, operation: str, outcome: BulkOutcome) -> dict[str, Any]:
    """批量结果信封：调用方据此逐项提示成功与失败原因。"""
    return {
        "operation": operation,
        "requested": outcome.requested,
        "succeeded": outcome.succeeded,
        "failed": outcome.failed,
        "actor_type": actor.actor_type.value,
    }
