"""Reserved-slot consistency audit (DEVELOP D18, product 4.1/15/18).

The invariant is simple: an Agent's ``reserved_execution_count`` equals the number of its
executions that have not ended, including executions whose fixed deadline already passed
but which have not been reclaimed yet. Expired-but-unreclaimed executions still hold their
slot by design, so they count.

Drift is reported with its cause and repaired under the agent's row lock from a fresh
read. A negative or over-limit count is never hidden by clamping: the repair sets the
truthful value and records why it differed.
"""

from __future__ import annotations

import logging
import uuid

from apps.identity.models import Agent
from apps.tasks.models import TaskExecution
from domain.contracts import ActorContext, ActorType
from domain.events import record_activity

logger = logging.getLogger("domain.capacity_audit")


def audit_capacity_slots(*, limit: int = 500, agent_ids=None) -> dict:
    """Compare reserved slots with real open executions and repair drift with evidence."""
    queryset = Agent.objects.order_by("pk")
    if agent_ids is not None:
        queryset = queryset.filter(pk__in=list(agent_ids))
    candidates = list(queryset.values_list("pk", flat=True)[:limit])

    checked = 0
    repaired = 0
    reported = 0
    for agent_id in candidates:
        agent = Agent.objects.select_for_update().filter(pk=agent_id).first()
        if agent is None:
            continue
        checked += 1
        # Re-read after locking: a concurrent claim/release must not be audited stale.
        expected = TaskExecution.objects.filter(
            agent_id=agent.pk, ended_at__isnull=True
        ).count()

        if agent.reserved_execution_count == expected:
            continue

        reason = _drift_reason(agent, expected)
        updated = Agent.objects.filter(
            pk=agent.pk, reserved_execution_count=agent.reserved_execution_count
        ).update(reserved_execution_count=expected)
        if updated != 1:
            # Someone changed the row between read and write; report without forcing.
            logger.warning("Agent %s 容量修复时行已变化，跳过本轮", agent.pk)
            reported += 1
            continue

        repaired += 1
        if expected < 0 or expected > agent.max_concurrency:
            reported += 1
            logger.error("Agent %s 容量超出上限：%s", agent.pk, reason)
        record_activity(
            ActorContext(
                actor_type=ActorType.SYSTEM, request_id=f"capacity-audit:{uuid.uuid4()}"
            ),
            "agent.capacity_repaired",
            before={"reserved_execution_count": agent.reserved_execution_count},
            after={"reserved_execution_count": expected},
            reason=reason,
        )
        logger.info("Agent %s 容量槽修复：%s", agent.pk, reason)

    return {"checked": checked, "repaired": repaired, "reported": reported}


def _drift_reason(agent: Agent, expected: int) -> str:
    # ``ck_agent_reserved_within_limit`` already prevents a stored count above
    # max_concurrency, so these two branches are defensive guards against a future schema
    # or manual data change; they must still report rather than clamp.
    if expected < 0:  # pragma: no cover - count() cannot be negative
        return "未释放容量槽数为负，数据库状态不一致"
    if expected > agent.max_concurrency:  # pragma: no cover - prevented by the check constraint
        return (
            f"未结束执行数 {expected} 超过最大并发 {agent.max_concurrency}；"
            "原占槽计数为 "
            f"{agent.reserved_execution_count}，已按真实执行数修复"
        )
    return (
        f"占槽计数 {agent.reserved_execution_count} 与真实未结束执行数 {expected} 不一致，"
        "已按真实执行数修复"
    )
