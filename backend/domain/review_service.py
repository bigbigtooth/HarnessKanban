"""Review decisions, rework assignment and task reopen (DEVELOP D14, product 6.4/8).

Exactly one formal review per submission. Approval keeps the submitter and the evidence;
rejection clears the current round and defaults to an exclusive assignment back to the
original executor, without silently widening project authorisation.
"""

from __future__ import annotations

import logging
import uuid

from django.db import IntegrityError
from django.db.models import F

from apps.identity.models import Agent
from apps.projects.models import ProjectMember
from apps.tasks.models import Task, TaskReview, TaskSubmission
from domain.clock import db_now
from domain.contracts import (
    OP_TASK_REOPEN,
    OP_TASK_REVIEW,
    ActorContext,
    ActorType,
    AgentRole,
    ClaimMode,
    ProjectStatus,
    ReviewDecision,
    TaskStatus,
    route_operation,
)
from domain.errors import (
    InvalidArgumentError,
    ResourceNotFoundError,
    ReviewAlreadyDecidedError,
    RoleForbiddenError,
    SelfReviewForbiddenError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan
from domain.permissions import accessible_project_ids, load_actor_agent_locked, require_membership
from domain.presenters import iso, review_view, submission_view, task_summary

logger = logging.getLogger("domain.review")


def review_submission(
    actor: ActorContext,
    *,
    task_id: uuid.UUID,
    payload: dict,
    idem_key: str,
) -> dict:
    decision_raw = str(payload.get("decision") or "").strip().upper()
    if decision_raw not in (ReviewDecision.APPROVE.value, ReviewDecision.REJECT.value):
        raise InvalidArgumentError("decision 必须是 APPROVE 或 REJECT")
    decision = ReviewDecision(decision_raw)
    comment = str(payload.get("comment") or "").strip()
    if decision is ReviewDecision.REJECT and not comment:
        raise InvalidArgumentError("驳回必须填写原因与修改要求")

    expected_version = payload.get("expected_version")
    if expected_version is None or isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise InvalidArgumentError("expected_version 必填且必须是整数")
    submission_id = _parse_uuid(payload.get("submission_id"), "submission_id")

    surface = "web" if actor.actor_type is ActorType.WEB_OPERATOR else "agent"
    operation = route_operation(OP_TASK_REVIEW, surface)
    request_hash = canonical_request_hash(
        operation=operation,
        body={
            "submission_id": str(submission_id),
            "decision": decision.value,
            "comment": comment,
            "expected_version": expected_version,
        },
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

    reviewer, reviewer_verified = _resolve_reviewer(actor, payload)

    plan = LockPlan()
    plan.add_task(task_id)
    plan.add_project(
        Task.objects.filter(pk=task_id).values_list("project_id", flat=True).first()
    )
    if reviewer is not None:
        plan.add_agent(reviewer.pk)
    submission_hint = TaskSubmission.objects.filter(pk=submission_id).values(
        "submitted_by_agent_id", "task_id"
    ).first()
    if submission_hint:
        plan.add_agent(submission_hint["submitted_by_agent_id"])
        plan.add_task(submission_hint["task_id"])
    locked = acquire_plan(plan)

    task = locked.tasks.get(task_id)
    submission = TaskSubmission.objects.filter(pk=submission_id).first()
    if task is None or submission is None or submission.task_id != task.pk:
        raise ResourceNotFoundError("任务或提交不存在")

    if task.status != TaskStatus.IN_REVIEW.value:
        raise VersionConflictError("任务当前不是待审核", details={"task_status": task.status})
    if task.current_submission_id != submission.pk:
        raise VersionConflictError("只能审核当前提交")
    if task.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": task.version}
        )

    # The submitter can never review their own result, and the reviewer must be an enabled
    # project supervisor (for a web operation, an explicitly chosen one).
    if reviewer is None:
        raise RoleForbiddenError("审核必须由项目主管发起")
    if reviewer.pk == submission.submitted_by_agent_id:
        raise SelfReviewForbiddenError()

    now = db_now()
    try:
        review = TaskReview.objects.create(
            submission=submission,
            task=task,
            decision=decision.value,
            comment=comment,
            actor_type=actor.actor_type.value,
            reviewer_agent=reviewer,
            reviewer_identity_verified=reviewer_verified,
            created_at=now,
        )
    except IntegrityError as exc:
        raise ReviewAlreadyDecidedError(details={"submission_id": str(submission.pk)}) from exc

    rework_note = ""
    if decision is ReviewDecision.APPROVE:
        updated = Task.objects.filter(
            pk=task.pk,
            status=TaskStatus.IN_REVIEW.value,
            current_submission_id=submission.pk,
            version=expected_version,
        ).update(
            status=TaskStatus.DONE.value,
            completed_at=now,
            version=expected_version + 1,
        )
    else:
        updated = Task.objects.filter(
            pk=task.pk,
            status=TaskStatus.IN_REVIEW.value,
            current_submission_id=submission.pk,
            version=expected_version,
        ).update(
            status=TaskStatus.TODO.value,
            current_submission_id=None,
            assignee_agent_id=None,
            claimed_at=None,
            submitted_at=None,
            completed_at=None,
            version=expected_version + 1,
        )
        rework_note = _assign_rework(task, submission.submitted_by_agent_id)

    if updated != 1:
        raise VersionConflictError("任务状态或版本已变化，审核未生效")

    task.refresh_from_db()
    action = "task.approved" if decision is ReviewDecision.APPROVE else "task.rejected"
    record_activity(
        actor,
        action,
        task_id=task.pk,
        project_id=task.project_id,
        execution_id=submission.execution_id,
        submission_id=submission.pk,
        before={"status": TaskStatus.IN_REVIEW.value},
        after={"status": task.status, "decision": decision.value, "rework": rework_note},
        reason=comment,
    )
    enqueue_outbox(
        action,
        task.pk,
        {
            "task_id": str(task.pk),
            "project_id": str(task.project_id),
            "submission_id": str(submission.pk),
            "decision": decision.value,
        },
    )

    result = {
        "data": {
            "task_id": str(task.pk),
            "status": task.status,
            "version": task.version,
            "current_submission_id": None if task.current_submission_id is None else str(task.current_submission_id),
            "completed_at": iso(task.completed_at),
            "rework_assignment": rework_note,
            "task": task_summary(task),
            "submission": submission_view(submission),
            "review": review_view(review),
        }
    }
    complete_receipt(receipt, response_data=result, resource_type="task", resource_id=task.pk)
    return {**result, "idempotent_replay": False}


def _resolve_reviewer(actor: ActorContext, payload: dict) -> tuple[Agent | None, bool]:
    if actor.actor_type is ActorType.AGENT:
        agent = load_actor_agent_locked(actor)
        if payload.get("reviewer_agent_id") is not None:
            supplied = _parse_uuid(payload.get("reviewer_agent_id"), "reviewer_agent_id")
            if supplied != agent.pk:
                raise InvalidArgumentError("AgentAPI 的审核者只能来自认证身份")
        if agent.role != AgentRole.SUPERVISOR.value:
            raise RoleForbiddenError("员工不能审核结果")
        return agent, True

    if actor.actor_type is ActorType.WEB_OPERATOR:
        # The local administrator must explicitly choose which supervisor reviews; the
        # chosen identity is a business object, recorded as unverified.
        reviewer_id = _parse_uuid(payload.get("reviewer_agent_id"), "reviewer_agent_id")
        reviewer = Agent.objects.filter(pk=reviewer_id).first()
        if reviewer is None:
            raise ResourceNotFoundError("选定的审核主管不存在")
        if not reviewer.enabled:
            raise InvalidArgumentError("选定的审核主管已被禁用")
        if reviewer.role != AgentRole.SUPERVISOR.value:
            raise RoleForbiddenError("选定的审核人不是主管")
        return reviewer, False

    raise RoleForbiddenError("system 来源不能审核")


def _assign_rework(task: Task, executor_id) -> str:
    """Rejected work defaults to an exclusive assignment to the original executor."""
    executor = Agent.objects.filter(pk=executor_id).first() if executor_id else None
    if executor is None:
        return "unavailable_designated_agent"
    if not executor.enabled:
        return "designated_agent_disabled_needs_reassignment"
    if not ProjectMember.objects.filter(project_id=task.project_id, agent_id=executor.pk).exists():
        return "designated_agent_not_member_needs_reassignment"

    Task.objects.filter(pk=task.pk).update(
        claim_mode=ClaimMode.ASSIGNED.value,
        designated_agent_id=executor.pk,
    )
    return "assigned_to_original_executor"


def reopen_task(
    actor: ActorContext,
    *,
    task_id: uuid.UUID,
    reason: str,
    expected_version: int | None,
    idem_key: str,
) -> dict:
    """Reopen a DONE task for another round, keeping all completed history."""
    reason = (reason or "").strip()
    if not reason:
        raise InvalidArgumentError("重新打开必须填写原因")
    if expected_version is None or isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise InvalidArgumentError("expected_version 必填且必须是整数")

    surface = "web" if actor.actor_type is ActorType.WEB_OPERATOR else "agent"
    operation = route_operation(OP_TASK_REOPEN, surface)
    request_hash = canonical_request_hash(
        operation=operation,
        body={"reason": reason, "expected_version": expected_version},
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

    plan = LockPlan()
    plan.add_task(task_id)
    plan.add_project(Task.objects.filter(pk=task_id).values_list("project_id", flat=True).first())
    acting_agent = None
    if actor.actor_type is ActorType.AGENT:
        acting_agent = load_actor_agent_locked(actor)
        plan.add_agent(acting_agent.pk)
    locked = acquire_plan(plan)

    task = locked.tasks.get(task_id)
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    _require_supervisor_for_task(actor, acting_agent, task)

    if task.status != TaskStatus.DONE.value:
        raise VersionConflictError("只有已完成任务可以重新打开", details={"task_status": task.status})
    if task.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": task.version}
        )
    project = locked.projects.get(task.project_id)
    if project is None or project.status != ProjectStatus.ACTIVE.value:
        from domain.errors import ProjectNotActiveError

        raise ProjectNotActiveError(details={"project_status": None if project is None else project.status})

    now = db_now()
    updated = Task.objects.filter(
        pk=task.pk, status=TaskStatus.DONE.value, version=expected_version
    ).update(
        status=TaskStatus.TODO.value,
        current_submission_id=None,
        assignee_agent_id=None,
        claimed_at=None,
        submitted_at=None,
        completed_at=None,
        updated_at=now,
        version=F("version") + 1,
    )
    if updated != 1:
        raise VersionConflictError("任务状态或版本已变化，重新打开未生效")

    task.refresh_from_db()
    record_activity(
        actor,
        "task.reopened",
        task_id=task.pk,
        project_id=task.project_id,
        before={"status": TaskStatus.DONE.value},
        after={"status": task.status},
        reason=reason,
    )
    enqueue_outbox(
        "task.reopened",
        task.pk,
        {"task_id": str(task.pk), "project_id": str(task.project_id), "reason": reason},
    )

    result = {
        "data": {
            "task_id": str(task.pk),
            "status": task.status,
            "version": task.version,
            "task": task_summary(task),
        }
    }
    complete_receipt(receipt, response_data=result, resource_type="task", resource_id=task.pk)
    return {**result, "idempotent_replay": False}


def _require_supervisor_for_task(actor: ActorContext, acting_agent: Agent | None, task: Task) -> None:
    if actor.actor_type is ActorType.WEB_OPERATOR:
        return
    if acting_agent is None:
        raise RoleForbiddenError("重新打开必须由项目主管发起")
    if acting_agent.role != AgentRole.SUPERVISOR.value:
        raise RoleForbiddenError("员工不能重新打开任务")
    if task.project_id not in accessible_project_ids(acting_agent.pk):
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    require_membership(acting_agent.pk, task.project_id)


def _parse_uuid(value, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(f"字段 {field} 必须是合法 UUID", details={"field": field}) from exc
