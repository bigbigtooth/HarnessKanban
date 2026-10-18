"""Execution end / task slot-release primitives (DEVELOP D05/D13, frozen in T04).

These two primitives are the only place that ends an execution or resets an in-progress
task to TODO. They never open or commit a transaction, never write audit/outbox and never
make a permission decision: callers must already hold the relevant locks and be inside the
outermost transaction.
"""

from __future__ import annotations

import logging
import uuid

from django.db.models import F

from apps.identity.models import Agent
from apps.tasks.models import Task, TaskExecution
from domain.contracts import TaskStatus
from domain.errors import DatabaseBusyError, VersionConflictError

logger = logging.getLogger("domain.execution")


def finish_execution_locked(
    execution: TaskExecution,
    *,
    ended_at,
    end_reason,
) -> bool:
    """End one execution and release exactly one capacity slot.

    Returns ``True`` when this call ended the execution. If the execution was already
    ended, nothing is changed (no slot change, no second event) and ``False`` is returned.

    The ``ended_at IS NULL`` condition is the single-writer guard, so a repeated timeout
    scan or a duplicated recovery call can never release the same slot twice.
    """
    ended = (
        TaskExecution.objects.filter(pk=execution.pk, ended_at__isnull=True)
        .update(ended_at=ended_at, end_reason=end_reason)
    )
    if ended == 0:
        return False

    released = Agent.objects.filter(
        pk=execution.agent_id, reserved_execution_count__gt=0
    ).update(reserved_execution_count=F("reserved_execution_count") - 1)
    if released != 1:
        # No max(count-1, 0) masking: an inconsistent slot count must fail the whole
        # transaction and stay visible for the capacity audit.
        logger.error(
            "容量槽释放异常：execution=%s agent=%s affected=%s",
            execution.pk,
            execution.agent_id,
            released,
        )
        raise DatabaseBusyError(
            "容量槽释放异常，事务已回滚",
            details={"execution_id": str(execution.pk), "affected_rows": released},
        )
    return True


def reset_in_progress_task_locked(
    task: Task,
    *,
    expected_execution_id: uuid.UUID,
    now,
) -> None:
    """Move an IN_PROGRESS task back to TODO in a single CAS.

    Condition: same task, still IN_PROGRESS, still pointing at ``expected_execution_id``,
    and still at the version read under lock. The same statement clears this round's
    ownership fields and increments the task version exactly once.
    """
    updated = Task.objects.filter(
        pk=task.pk,
        status=TaskStatus.IN_PROGRESS.value,
        active_execution_id=expected_execution_id,
        version=task.version,
    ).update(
        status=TaskStatus.TODO.value,
        active_execution_id=None,
        assignee_agent_id=None,
        claimed_at=None,
        submitted_at=None,
        updated_at=now,
        version=F("version") + 1,
    )
    if updated != 1:
        raise VersionConflictError(
            "任务状态或版本已变化，无法回到待办",
            details={
                "task_id": str(task.pk),
                "expected_execution_id": str(expected_execution_id),
                "expected_version": task.version,
            },
        )
