"""Progress, release, supervisor reclaim and system timeout recovery (DEVELOP D13).

The wrappers here reuse the T04 primitives (``finish_execution_locked`` /
``reset_in_progress_task_locked``) and add exactly one activity event, one outbox event and
one receipt per real end. A repeated scan or duplicated request ends nothing twice.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from apps.identity.models import Agent
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now
from domain.contracts import (
    OP_TASK_PROGRESS,
    OP_TASK_RECLAIM,
    OP_TASK_RELEASE,
    ActorContext,
    ActorType,
    EndReason,
    TaskStatus,
    route_operation,
)
from domain.errors import (
    ExecutionNotCurrentError,
    InvalidArgumentError,
    ResourceNotFoundError,
    RoleForbiddenError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.execution import finish_execution_locked, reset_in_progress_task_locked
from domain.idempotency import Receipt, canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan
from domain.permissions import (
    accessible_project_ids,
    load_actor_agent_locked,
    require_membership,
)
from domain.presenters import execution_view, iso, task_summary

logger = logging.getLogger("domain.execution_service")


@dataclass
class EndOutcome:
    task: Task
    execution: TaskExecution
    already_ended: bool


def update_progress(
    actor: ActorContext,
    *,
    task_id: uuid.UUID,
    execution_id: uuid.UUID,
    message: str,
    percent: int | None,
    idem_key: str,
) -> dict:
    """Record a short progress message for the current execution.

    Progress never extends the deadline, never changes the task version and never
    completes the task (``percent=100`` is still just a number).
    """
    if message is None or not str(message).strip():
        raise InvalidArgumentError("进度消息不能为空")
    message = str(message).strip()
    if len(message) > 2000:
        raise InvalidArgumentError("进度消息超过长度上限 2000")
    if percent is not None and (percent < 0 or percent > 100):
        raise InvalidArgumentError("percent 必须在 0 到 100 之间")

    agent = load_actor_agent_locked(actor) if actor.actor_type is ActorType.AGENT else None
    if agent is None:
        raise RoleForbiddenError("进度更新必须由当前执行的 Agent 发起")

    request_hash = canonical_request_hash(
        operation=route_operation(OP_TASK_PROGRESS, "agent"),
        body={"message": message, "percent": percent, "execution_id": str(execution_id)},
        resource_id=task_id,
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=route_operation(OP_TASK_PROGRESS, "agent"),
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    plan = LockPlan()
    plan.add_agent(agent.pk)
    plan.add_task(task_id)
    plan.add_execution(execution_id)
    task_hint = Task.objects.filter(pk=task_id).values("project_id").first()
    if task_hint:
        plan.add_project(task_hint["project_id"])
    locked = acquire_plan(plan)

    task = locked.tasks.get(task_id)
    execution = locked.executions.get(execution_id)
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    require_membership(agent.pk, task.project_id)
    if execution is None:
        # A supplied execution id that is not the current execution is a conflict, not a
        # missing resource: the task itself exists and is readable.
        raise ExecutionNotCurrentError(
            "执行 ID 不是当前有效执行", details={"supplied_execution_id": str(execution_id)}
        )

    now = db_now()
    _guard_current_execution(actor, agent, task, execution, now)

    execution.progress_message = message
    execution.progress_percent = percent
    execution.progress_updated_at = now
    execution.save(update_fields=["progress_message", "progress_percent", "progress_updated_at"])

    record_activity(
        actor,
        "execution.progress",
        task_id=task.pk,
        project_id=task.project_id,
        execution_id=execution.pk,
        after={"percent": percent},
        reason=message[:200],
    )
    payload = {"data": {"task_id": str(task.pk), "execution_id": str(execution.pk), "progress": execution_view(execution)}}
    complete_receipt(receipt, response_data=payload, resource_type="task", resource_id=task.pk)
    return {**payload, "idempotent_replay": False}


def _guard_current_execution(actor, agent: Agent, task: Task, execution: TaskExecution, now) -> None:
    if task.status != TaskStatus.IN_PROGRESS.value:
        raise ExecutionNotCurrentError(
            "任务当前不是进行中", details={"task_status": task.status}
        )
    if task.active_execution_id != execution.pk:
        raise ExecutionNotCurrentError("执行 ID 不是当前有效执行")
    if execution.task_id != task.pk:
        raise VersionConflictError("执行记录与任务不匹配")
    if execution.agent_id != agent.pk:
        raise ExecutionNotCurrentError("当前执行属于其他 Agent")
    if execution.ended_at is not None:
        raise ExecutionNotCurrentError("当前执行已经结束")
    if now >= execution.execution_deadline_at:
        from domain.errors import ExecutionTimeoutError

        raise ExecutionTimeoutError(
            details={
                "execution_deadline_at": iso(execution.execution_deadline_at),
                "now": iso(now),
            }
        )


def end_execution(
    actor: ActorContext,
    *,
    task_id: uuid.UUID,
    execution_id: uuid.UUID,
    reason: EndReason,
    expected_version: int | None = None,
    reason_text: str = "",
    idem_key: str,
) -> dict:
    """Release, reclaim or time out one execution and return the task to TODO."""
    if reason is EndReason.SUBMIT:
        raise InvalidArgumentError("SUBMIT 不能通过结束执行接口完成，必须走正式提交")
    if reason in (EndReason.RELEASE, EndReason.RECLAIM) and not reason_text.strip():
        raise InvalidArgumentError("释放或回收必须填写原因")

    if actor.actor_type is ActorType.AGENT:
        agent = load_actor_agent_locked(actor)
    elif actor.actor_type is ActorType.WEB_OPERATOR:
        agent = None
    else:
        agent = None
    if reason is EndReason.RELEASE and agent is None:
        raise RoleForbiddenError("主动释放必须由当前执行的 Agent 发起")

    operation = route_operation(
        OP_TASK_RELEASE if reason is EndReason.RELEASE else OP_TASK_RECLAIM,
        "web" if actor.actor_type is ActorType.WEB_OPERATOR else "agent",
    )
    request_hash = canonical_request_hash(
        operation=operation,
        body={"reason": reason.value, "reason_text": reason_text},
        resource_id=task_id,
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    outcome = _end_execution_locked(
        actor,
        task_id=task_id,
        execution_id=execution_id,
        reason=reason,
        expected_version=expected_version,
        reason_text=reason_text,
        acting_agent=agent,
    )
    task, execution = outcome.task, outcome.execution
    payload = {
        "data": {
            "task_id": str(task.pk),
            "status": task.status,
            "version": task.version,
            "execution_id": str(execution.pk),
            "ended_at": iso(execution.ended_at),
            "end_reason": execution.end_reason,
            "assignee_agent_id": None,
            "task": task_summary(task),
            "execution": execution_view(execution),
        }
    }
    complete_receipt(receipt, response_data=payload, resource_type="task", resource_id=task.pk)
    return {**payload, "idempotent_replay": False}


def _end_execution_locked(
    actor: ActorContext,
    *,
    task_id,
    execution_id,
    reason: EndReason,
    expected_version,
    reason_text: str,
    acting_agent: Agent | None,
) -> EndOutcome:
    plan = LockPlan()
    plan.add_task(task_id)
    plan.add_execution(execution_id)
    hint = (
        TaskExecution.objects.filter(pk=execution_id)
        .values("agent_id", "task__project_id")
        .first()
    )
    if hint:
        plan.add_agent(hint["agent_id"])
        plan.add_project(hint["task__project_id"])
    if acting_agent is not None:
        plan.add_agent(acting_agent.pk)
    locked = acquire_plan(plan)

    task = locked.tasks.get(task_id)
    execution = locked.executions.get(execution_id)
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    if execution is None:
        raise ExecutionNotCurrentError(
            "执行 ID 不是当前有效执行", details={"supplied_execution_id": str(execution_id)}
        )

    if execution.task_id != task.pk:
        raise VersionConflictError("执行记录与任务不匹配")

    if task.active_execution_id != execution.pk or execution.ended_at is not None:
        # Already ended: no task change, no second slot release, no extra event.
        return EndOutcome(task=task, execution=execution, already_ended=True)

    now = db_now()

    if reason is EndReason.TIMEOUT:
        if now < execution.execution_deadline_at:
            raise VersionConflictError(
                "执行尚未到期，不能按超时结束",
                details={"execution_deadline_at": iso(execution.execution_deadline_at), "now": iso(now)},
            )
    elif reason is EndReason.RELEASE:
        if acting_agent is None or execution.agent_id != acting_agent.pk:
            raise ExecutionNotCurrentError("只能释放自己持有的当前执行")
    elif reason is EndReason.RECLAIM:
        _require_reclaim_permission(actor, task)

    if expected_version is not None and task.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": task.version}
        )

    ended = finish_execution_locked(execution, ended_at=now, end_reason=reason.value)
    if not ended:  # pragma: no cover - guarded by the ended_at check above
        return EndOutcome(task=task, execution=execution, already_ended=True)

    reset_in_progress_task_locked(task, expected_execution_id=execution.pk, now=now)
    task.refresh_from_db()
    execution.ended_at = now
    execution.end_reason = reason.value

    record_activity(
        actor,
        f"task.{reason.value.lower()}",
        task_id=task.pk,
        project_id=task.project_id,
        execution_id=execution.pk,
        before={"status": TaskStatus.IN_PROGRESS.value},
        after={"status": task.status},
        reason=reason_text,
    )
    enqueue_outbox(
        f"task.{reason.value.lower()}",
        task.pk,
        {
            "task_id": str(task.pk),
            "execution_id": str(execution.pk),
            "reason": reason.value,
            "project_id": str(task.project_id),
        },
    )
    return EndOutcome(task=task, execution=execution, already_ended=False)


def _require_reclaim_permission(actor: ActorContext, task: Task) -> None:
    if actor.actor_type is ActorType.WEB_OPERATOR:
        return
    from domain.permissions import require_supervisor

    if actor.actor_type is not ActorType.AGENT:
        raise RoleForbiddenError("回收必须由项目主管或本地管理者发起")
    agent = load_actor_agent_locked(actor)
    require_supervisor(agent)
    require_membership(agent.pk, task.project_id)


def timeout_scan(*, limit: int = 200, now=None) -> dict:
    """System rule: end timed-out executions and return their tasks to TODO.

    Safe to run concurrently with other workers and with get_task compensation: the
    conditional update in ``finish_execution_locked`` means an execution is ended once.
    """
    now = now or db_now()
    expired_ids = list(
        TaskExecution.objects.filter(ended_at__isnull=True, execution_deadline_at__lte=now)
        .order_by("execution_deadline_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    if not expired_ids:
        return {"scanned": 0, "ended": 0}

    system_actor = ActorContext(
        actor_type=ActorType.SYSTEM,
        request_id=f"timeout-scan:{uuid.uuid4()}",
    )
    ended_total = 0
    for execution_id in expired_ids:
        plan = LockPlan()
        plan.add_execution(execution_id)
        hint = (
            TaskExecution.objects.filter(pk=execution_id)
            .values("agent_id", "task_id", "task__project_id")
            .first()
        )
        if hint is None:
            continue
        plan.add_agent(hint["agent_id"])
        plan.add_task(hint["task_id"])
        plan.add_project(hint["task__project_id"])
        locked = acquire_plan(plan)
        execution = locked.executions.get(execution_id)
        task = locked.tasks.get(hint["task_id"])
        if execution is None or task is None or execution.ended_at is not None:
            continue
        if task.active_execution_id != execution.pk or task.status != TaskStatus.IN_PROGRESS.value:
            continue
        recovery_now = db_now()
        if recovery_now < execution.execution_deadline_at:
            continue
        if not finish_execution_locked(
            execution, ended_at=recovery_now, end_reason=EndReason.TIMEOUT.value
        ):
            continue
        reset_in_progress_task_locked(task, expected_execution_id=execution.pk, now=recovery_now)
        task.refresh_from_db()
        ended_total += 1
        record_activity(
            system_actor,
            "task.timeout",
            task_id=task.pk,
            project_id=task.project_id,
            execution_id=execution.pk,
            before={"status": TaskStatus.IN_PROGRESS.value},
            after={"status": task.status},
            reason="固定执行时限到期，系统扫描回收",
        )
        enqueue_outbox(
            "task.timeout",
            task.pk,
            {"task_id": str(task.pk), "execution_id": str(execution.pk)},
        )
    return {"scanned": len(expired_ids), "ended": ended_total}
