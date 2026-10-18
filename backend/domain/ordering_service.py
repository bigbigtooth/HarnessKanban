"""In-column task ordering (DEVELOP D15, product 6.3/12.3).

Ordering is not a way to move work between columns: the target task keeps its status,
assignee, execution and submission, and only its ``sort_order`` and version change. The
service never trusts that two client-supplied neighbours are really adjacent — it looks up
the real neighbours inside the locked column, so a filtered board view cannot silently
reorder cards the caller never saw.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.db.models import F

from apps.tasks.models import Task
from domain.clock import db_now
from domain.contracts import ActorContext, ProjectStatus, TaskStatus
from domain.errors import (
    InvalidArgumentError,
    InvalidTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan
from domain.permissions import require_project_editable, require_project_supervisor
from domain.presenters import task_summary

logger = logging.getLogger("domain.ordering")

OPERATION_REORDER = "POST /api/web/v1/projects/{id}/task-order"

SORT_ORDER_STEP = Decimal("1000")
# Anything smaller than this cannot be halved again safely at 10 decimal places.
MIN_SORT_GAP = Decimal("0.000001")
MAX_COLUMN_SIZE = 2000

PAYLOAD_FIELDS = {"task_id", "expected_version", "before_task_id", "after_task_id", "status"}


def reorder_task(
    actor: ActorContext, project_id: uuid.UUID, *, payload: dict[str, Any], idem_key: str
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - PAYLOAD_FIELDS)
    if unknown:
        raise InvalidArgumentError("请求包含不支持的字段", details={"fields": unknown})

    task_id = _coerce_uuid(payload.get("task_id"), "task_id")
    expected_version = _require_version(payload.get("expected_version"))
    before_id = _coerce_uuid(payload.get("before_task_id"), "before_task_id") if payload.get("before_task_id") else None
    after_id = _coerce_uuid(payload.get("after_task_id"), "after_task_id") if payload.get("after_task_id") else None
    if before_id is None and after_id is None:
        raise InvalidArgumentError("必须提供 before_task_id 或 after_task_id 至少一个锚点")
    if before_id is not None and before_id == after_id:
        raise InvalidArgumentError("前后锚点不能是同一个任务")
    if task_id in (before_id, after_id):
        raise InvalidArgumentError("锚点不能是目标任务自身")
    asserted_status = payload.get("status")

    request_hash = canonical_request_hash(
        operation=OPERATION_REORDER,
        body={
            "task_id": str(task_id),
            "expected_version": expected_version,
            "before_task_id": None if before_id is None else str(before_id),
            "after_task_id": None if after_id is None else str(after_id),
            "status": asserted_status,
        },
        resource_id=task_id,
        project_id=project_id,
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OPERATION_REORDER,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    plan = LockPlan()
    plan.add_project(project_id)
    plan.add_task(task_id)
    for anchor in (before_id, after_id):
        plan.add_task(anchor)
    if actor.agent_id is not None:
        plan.add_agent(actor.agent_id)
    locked = acquire_plan(plan)

    project = locked.projects.get(project_id)
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    require_project_supervisor(actor, project)
    require_project_editable(project)

    task = locked.tasks.get(task_id)
    if task is None or task.project_id != project_id:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    if task.is_archived:
        raise InvalidTransitionError("归档任务不参与列内排序", details={"task_id": str(task.pk)})
    if task.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": task.version}
        )
    if asserted_status is not None and asserted_status != task.status:
        # A drag across columns must go through the business actions, never through ordering.
        raise InvalidTransitionError(
            "排序列与任务当前状态不一致，跨列移动必须使用业务动作",
            details={"asserted": asserted_status, "current": task.status},
        )

    before = _anchor(locked, before_id, task, "before_task_id")
    after = _anchor(locked, after_id, task, "after_task_id")

    column = _column_queryset(task)
    before_anchor, after_anchor = _resolve_real_neighbours(column, task, before, after)
    sort_order, renumbered = _compute_sort_order(column, task, before_anchor, after_anchor)
    if sort_order is None:
        _renumber_column(column, task)
        renumbered = True
        column = _column_queryset(task)
        before_anchor, after_anchor = _resolve_real_neighbours(column, task, before, after)
        sort_order, _ = _compute_sort_order(column, task, before_anchor, after_anchor)
        if sort_order is None:  # pragma: no cover - defensive
            raise VersionConflictError("无法为任务分配排序值，请刷新后重试")

    previous_sort_order = task.sort_order
    now = db_now()
    updated = Task.objects.filter(
        pk=task.pk, version=expected_version, status=task.status, is_archived=False
    ).update(sort_order=sort_order, version=F("version") + 1, updated_at=now)
    if updated != 1:
        raise VersionConflictError("任务在排序期间被其他操作修改，请刷新看板")

    task.refresh_from_db()

    record_activity(
        actor,
        "task.reordered",
        task_id=task.pk,
        project_id=task.project_id,
        before={"sort_order": str(previous_sort_order)},
        after={
            "sort_order": str(sort_order),
            "before_task_id": None if before_anchor is None else str(before_anchor.pk),
            "after_task_id": None if after_anchor is None else str(after_anchor.pk),
            "renumbered": renumbered,
        },
    )
    enqueue_outbox(
        "task.reordered",
        task.pk,
        {
            "task_id": str(task.pk),
            "project_id": str(task.project_id),
            "status": task.status,
            "renumbered": renumbered,
        },
    )

    result = {
        "task": task_summary(task),
        "sort_order": str(task.sort_order),
        "version": task.version,
        "status": task.status,
        "renumbered": renumbered,
    }
    complete_receipt(receipt, response_data=result, resource_type="task", resource_id=task.pk)
    return {**result, "idempotent_replay": False}


def _column_queryset(task: Task):
    """Every live card in the same project column, in stable order."""
    return Task.objects.filter(
        project_id=task.project_id, status=task.status, is_archived=False
    ).order_by("sort_order", "created_at", "id")


def _anchor(locked, anchor_id, task: Task, field: str):
    if anchor_id is None:
        return None
    anchor = locked.tasks.get(anchor_id)
    if anchor is None:
        raise ResourceNotFoundError(f"锚点任务不存在：{field}", details={"field": field})
    if anchor.project_id != task.project_id:
        raise InvalidArgumentError("锚点必须属于同一项目", details={"field": field})
    if anchor.status != task.status:
        # A neighbour in another column would be a disguised cross-column move.
        raise InvalidTransitionError(
            "锚点必须与目标任务处于同一列", details={"field": field, "anchor_status": anchor.status}
        )
    if anchor.is_archived:
        raise InvalidArgumentError("锚点不能是已归档任务", details={"field": field})
    return anchor


def _resolve_real_neighbours(column, task: Task, before, after):
    """Replace client-supplied anchors with the real adjacent cards in the column.

    Anchor rows are re-read from the database first: after a column renumber the values
    loaded earlier in this transaction are stale, and comparing stale values would look
    like an exhausted gap forever.
    """
    if before is not None:
        before.refresh_from_db(fields=["sort_order", "status", "is_archived"])
    if after is not None:
        after.refresh_from_db(fields=["sort_order", "status", "is_archived"])
    others = column.exclude(pk=task.pk)
    if before is not None and after is not None:
        if before.sort_order >= after.sort_order:
            raise InvalidArgumentError("前后锚点的排序值顺序不正确")
        between = others.filter(
            sort_order__gt=before.sort_order, sort_order__lt=after.sort_order
        ).exists()
        if between:
            # The caller's view was filtered or stale; letting it through would reorder
            # cards the caller never saw.
            raise VersionConflictError("锚点之间还存在其他任务，请刷新看板后重试")
        return before, after
    if before is not None:
        following = others.filter(sort_order__gt=before.sort_order).order_by("sort_order", "id").first()
        return before, following
    previous = others.filter(sort_order__lt=after.sort_order).order_by("-sort_order", "-id").first()
    return previous, after


def _compute_sort_order(column, task: Task, before, after):
    """Midpoint between the resolved neighbours; ``None`` when the gap is exhausted."""
    if before is not None and after is not None:
        gap = after.sort_order - before.sort_order
        if gap <= MIN_SORT_GAP:
            return None, False
        return _quantize((before.sort_order + after.sort_order) / 2), False
    if before is not None:
        return _quantize(before.sort_order + SORT_ORDER_STEP), False
    if after is not None:
        value = after.sort_order - SORT_ORDER_STEP
        return _quantize(value), False
    # Empty column: keep the default spacing.
    return _quantize(SORT_ORDER_STEP), False


def _renumber_column(column, task: Task) -> None:
    """Re-space the unlocked-into column with a fixed step, locking tasks by id order."""
    ids = list(column.values_list("pk", flat=True))
    if len(ids) > MAX_COLUMN_SIZE:
        raise VersionConflictError(
            "列内任务过多，无法在一次事务中重编号，请分页整理",
            details={"column_size": len(ids), "limit": MAX_COLUMN_SIZE},
        )
    rows = list(Task.objects.select_for_update().filter(pk__in=sorted(ids)).order_by("sort_order", "created_at", "id"))
    for index, row in enumerate(rows, start=1):
        target = _quantize(SORT_ORDER_STEP * index)
        if row.sort_order != target:
            Task.objects.filter(pk=row.pk).update(sort_order=target)
    logger.info("列内排序值已重编号：project=%s status=%s 数量=%s", task.project_id, task.status, len(rows))


def _quantize(value: Decimal) -> Decimal:
    """Clamp to the column's 10-decimal precision so no precision is lost on save."""
    return value.quantize(Decimal("0.0000000001"))


def _require_version(value) -> int:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError("expected_version 必填且必须是整数", details={"field": "expected_version"})
    return value


def _coerce_uuid(value, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(f"字段 {field} 必须是合法 UUID", details={"field": field}) from exc
