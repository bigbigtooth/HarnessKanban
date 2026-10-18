"""Project membership authorisation, revocation and the last-supervisor guard (T08, D08).

Membership expresses *authorisation only*; the global role lives on ``Agent`` (product 2.2).
Removing, demoting or disabling a supervisor must leave every affected ACTIVE project with
at least one enabled supervisor, so the guard and the write happen in the *same*
transaction, after a lock plan that covers every affected project, agent, task and
execution. Two concurrent supervisor revocations therefore serialise on the project row:
the second one re-counts the still-committed membership rows and is refused when the
project would be left without a supervisor.

Revocation disposition:

* ``WAIT`` - authorisation is revoked immediately (the membership row is deleted, so every
  read/write guard answers 404/401 from the next request on) while the open execution keeps
  its slot until the fixed deadline.
* ``NOW`` - every open execution of the agent in the affected project is ended inside the
  same transaction through the frozen T04 primitives (`finish_execution_locked` +
  `reset_in_progress_task_locked`); the task goes back to TODO exactly once and exactly one
  activity event plus one outbox event is written per ended execution.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db import IntegrityError

from apps.identity.models import Agent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now
from domain.contracts import AgentRole, ActorContext, EndReason, ProjectStatus, TaskStatus
from domain.errors import (
    InvalidArgumentError,
    InvalidTransitionError,
    LastSupervisorRequiredError,
    MembershipConflictError,
    ResourceNotFoundError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.execution import finish_execution_locked, reset_in_progress_task_locked
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, LockedRows, acquire_plan, require
from domain.permissions import require_project_editable
from domain.presenters import iso
from domain.transactions import assert_in_transaction

logger = logging.getLogger("domain.membership")

# Idempotency operation scope (CONTRACT.md §8: "<METHOD> <route template>").
OP_MEMBERSHIP_ADD = "PUT /api/web/v1/projects/{id}/members/{agent_id}"
OP_MEMBERSHIP_REMOVE = "DELETE /api/web/v1/projects/{id}/members/{agent_id}"

ACTION_ADD = "add"
ACTION_REMOVE = "remove"

# Revocation disposition for open executions (product 4.2, DEVELOP D08).
DISPOSITION_WAIT = "WAIT"
DISPOSITION_NOW = "NOW"
DISPOSITIONS = (DISPOSITION_WAIT, DISPOSITION_NOW)

REASON_MAX_LENGTH = 500

ACTIVITY_MEMBERSHIP_ADD = "membership.add"
ACTIVITY_MEMBERSHIP_REMOVE = "membership.remove"
ACTIVITY_EXECUTION_RECLAIM = "reclaim"

EVENT_MEMBERSHIP_ADDED = "project.member.added"
EVENT_MEMBERSHIP_REMOVED = "project.member.removed"
EVENT_EXECUTION_RECLAIMED = "task.execution.reclaimed"


# --------------------------------------------------------------------------------------
# Shared helpers (used by agent_service for the global role / disable changes)
# --------------------------------------------------------------------------------------


def project_ids_for_agent(agent_id) -> set:
    """Every project the agent is authorised for (membership only, no role involved)."""
    return set(
        ProjectMember.objects.filter(agent_id=agent_id).values_list("project_id", flat=True)
    )


def active_project_ids(project_ids) -> set:
    return set(
        Project.objects.filter(
            pk__in=set(project_ids), status=ProjectStatus.ACTIVE.value
        ).values_list("pk", flat=True)
    )


def open_execution_scope(agent_id, *, project_ids: set | None = None) -> list[dict[str, Any]]:
    """Open (not yet ended) executions of the agent, optionally limited to some projects.

    ``ended_at IS NULL`` is the effective-execution definition used for revocation: an
    execution whose deadline already passed still holds its capacity slot until the
    reclamation transaction runs, so it must be part of the disposition decision.
    """
    queryset = TaskExecution.objects.filter(agent_id=agent_id, ended_at__isnull=True)
    if project_ids is not None:
        queryset = queryset.filter(task__project_id__in=set(project_ids))
    return list(queryset.values("id", "task_id", "task__project_id"))


def build_revocation_plan(
    agent_id, *, execution_project_ids: set | None = None
) -> tuple[LockPlan, set, list[dict[str, Any]]]:
    """Decide the whole related id set *before* locking anything (DEVELOP D05/D12).

    Returns the plan, the membership project ids and the open-execution rows read without
    locks; the caller must re-validate them after ``acquire_plan``.
    """
    plan = LockPlan()
    plan.add_agent(agent_id)
    membership_ids = project_ids_for_agent(agent_id)
    for project_id in membership_ids:
        plan.add_project(project_id)
    executions = open_execution_scope(agent_id, project_ids=execution_project_ids)
    for row in executions:
        plan.add_project(row["task__project_id"])
        plan.add_task(row["task_id"])
        plan.add_execution(row["id"])
    return plan, membership_ids, executions


def revalidate_revocation_scope(
    agent_id, plan: LockPlan, *, execution_project_ids: set | None = None
) -> list[dict[str, Any]]:
    """Re-read the scope under the acquired locks; a changed relation re-plans the whole txn.

    Must be called after ``acquire_plan``: a relation that appeared between the unlocked
    read and the lock is outside the plan, so the transaction is rolled back and retried by
    ``run_write_service`` instead of silently continuing without the required lock.
    """
    membership_ids = project_ids_for_agent(agent_id)
    executions = open_execution_scope(agent_id, project_ids=execution_project_ids)
    valid = membership_ids <= plan.project_ids
    if valid:
        for row in executions:
            if not (
                plan.contains_execution(row["id"])
                and plan.contains_task(row["task_id"])
                and plan.contains_project(row["task__project_id"])
            ):
                valid = False
                break
    require(valid, "撤权锁计划在取得锁后已失效（成员或执行关系变化）")
    return executions


def locked_executions(
    locked: LockedRows, scope: list[dict[str, Any]]
) -> list[TaskExecution]:
    rows = []
    for row in sorted(scope, key=lambda item: item["id"]):
        execution = locked.executions.get(row["id"])
        if execution is None:
            raise VersionConflictError(
                "回收执行时执行行缺失", details={"execution_id": str(row["id"])}
            )
        rows.append(execution)
    return rows


def require_enabled_supervisors_preserved(project_ids, *, removed_agent_ids) -> None:
    """After the change, every ACTIVE project must still have an enabled supervisor.

    Only call this while holding the project rows of ``project_ids``: the count below is
    the decision, and it must not be read before the lock (DEVELOP D08).
    """
    removed = {value for value in removed_agent_ids if value is not None}
    for project_id in sorted(set(project_ids), key=str):
        remaining = (
            ProjectMember.objects.filter(
                project_id=project_id,
                agent__enabled=True,
                agent__role=AgentRole.SUPERVISOR.value,
            )
            .exclude(agent_id__in=removed)
            .exists()
        )
        if not remaining:
            raise LastSupervisorRequiredError(
                "项目必须保留至少一名有效主管，请先新增另一名主管",
                details={"project_id": str(project_id)},
            )


def reclaim_executions(
    actor: ActorContext,
    *,
    executions: list[TaskExecution],
    tasks: dict[uuid.UUID, Task],
    reason: str,
) -> list[dict[str, Any]]:
    """Immediately end the given open executions in the caller's transaction.

    Reuses the frozen T04 primitives (never a second implementation, never a hand written
    ``TODO``), releases exactly one capacity slot per execution and writes exactly one
    activity event plus one outbox event per ended execution. Any failure aborts the whole
    transaction, including the revocation that requested it.
    """
    assert_in_transaction()
    now = db_now()
    ended: list[dict[str, Any]] = []
    for execution in sorted(executions, key=lambda item: item.pk):
        task = tasks.get(execution.task_id)
        if task is None:
            raise VersionConflictError(
                "回收执行时任务锁缺失", details={"execution_id": str(execution.pk)}
            )
        if not finish_execution_locked(
            execution, ended_at=now, end_reason=EndReason.RECLAIM.value
        ):
            # Already ended under this lock: never release a second slot or add a second
            # event for the same execution.
            continue
        reset_in_progress_task_locked(task, expected_execution_id=execution.pk, now=now)
        record_activity(
            actor,
            ACTIVITY_EXECUTION_RECLAIM,
            project_id=task.project_id,
            task_id=task.pk,
            execution_id=execution.pk,
            reason=reason,
            before={
                "task_status": TaskStatus.IN_PROGRESS.value,
                "assignee_agent_id": str(execution.agent_id),
                "execution_deadline_at": iso(execution.execution_deadline_at),
            },
            after={
                "task_status": TaskStatus.TODO.value,
                "task_version": task.version + 1,
                "execution_ended_at": iso(now),
                "end_reason": EndReason.RECLAIM.value,
                "actor_type": actor.actor_type.value,
            },
        )
        enqueue_outbox(
            EVENT_EXECUTION_RECLAIMED,
            execution.pk,
            {
                "execution_id": str(execution.pk),
                "task_id": str(task.pk),
                "project_id": str(task.project_id),
                "agent_id": str(execution.agent_id),
                "end_reason": EndReason.RECLAIM.value,
                "ended_at": iso(now),
                "actor_type": actor.actor_type.value,
                "reason": reason,
            },
        )
        ended.append(
            {
                "execution_id": str(execution.pk),
                "task_id": str(task.pk),
                "project_id": str(task.project_id),
                "agent_id": str(execution.agent_id),
                "task_status": TaskStatus.TODO.value,
                "task_version": task.version + 1,
                "ended_at": iso(now),
                "end_reason": EndReason.RECLAIM.value,
            }
        )
    return ended


# --------------------------------------------------------------------------------------
# Membership reads
# --------------------------------------------------------------------------------------


def member_view(member: ProjectMember, agent: Agent, *, online_state: str | None = None) -> dict:
    return {
        "id": str(member.pk),
        "project_id": str(member.project_id),
        "agent_id": str(agent.pk),
        "agent_name": agent.name,
        "role": agent.role,
        "enabled": agent.enabled,
        "deleted_at": iso(agent.deleted_at),
        "online_state": online_state,
        "last_heartbeat_at": iso(agent.last_heartbeat_at),
        "created_at": iso(member.created_at),
    }


def list_members(actor: ActorContext, project_id, page: int, page_size: int) -> dict:
    """Member list of one project. Authorisation only: the role column is the agent's."""
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        raise ResourceNotFoundError("项目不存在或不可访问")
    queryset = (
        ProjectMember.objects.filter(project_id=project_id)
        .select_related("agent")
        .order_by("created_at", "id")
    )
    total = queryset.count()
    offset = (page - 1) * page_size
    rows = list(queryset[offset : offset + page_size])
    from domain.agent_service import online_state_for  # local import avoids a cycle

    items = [member_view(member, member.agent, online_state=online_state_for(member.agent)) for member in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------------------
# Membership writes
# --------------------------------------------------------------------------------------


def update_membership(
    actor: ActorContext,
    project_id,
    agent_id,
    action: str,
    payload: dict[str, Any] | None = None,
    idem_key: str | None = None,
) -> dict:
    """Frozen service entry (CONTRACT.md §5): add or remove one project authorisation."""
    if action == ACTION_ADD:
        return add_member(actor, project_id, agent_id, idem_key)
    if action == ACTION_REMOVE:
        return remove_member(actor, project_id, agent_id, payload or {}, idem_key)
    raise InvalidArgumentError(
        "不支持的成员操作", details={"action": action, "allowed": list((ACTION_ADD, ACTION_REMOVE))}
    )


def add_member(actor: ActorContext, project_id, agent_id, idem_key: str | None) -> dict:
    """Add (or ensure) one project authorisation; an existing membership is a conflict."""
    assert_in_transaction()
    request_hash = canonical_request_hash(
        operation=OP_MEMBERSHIP_ADD, resource_id=agent_id, project_id=project_id
    )

    plan = LockPlan()
    plan.add_project(project_id)
    plan.add_agent(agent_id)
    locked = acquire_plan(plan)

    project = locked.projects.get(project_id)
    if project is None:
        raise ResourceNotFoundError("项目不存在或不可访问")
    require_project_editable(project)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_MEMBERSHIP_ADD,
        key=idem_key,
        request_hash=request_hash,
        resource_type="project_member",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    if not agent.enabled:
        raise InvalidTransitionError(
            "Agent 已被禁用，不能被授权到项目",
            details={"agent_id": str(agent.pk)},
        )
    if ProjectMember.objects.filter(project_id=project_id, agent_id=agent_id).exists():
        raise MembershipConflictError(
            "该 Agent 已经是项目成员",
            details={"project_id": str(project_id), "agent_id": str(agent_id)},
        )

    try:
        member = ProjectMember.objects.create(project=project, agent=agent)
    except IntegrityError as exc:  # concurrent add of the same pair
        raise MembershipConflictError(
            "该 Agent 已经是项目成员",
            details={"project_id": str(project_id), "agent_id": str(agent_id)},
        ) from exc

    data = member_view(member, agent)
    complete_receipt(
        receipt,
        response_data=data,
        http_status=201,
        resource_type="project_member",
        resource_id=member.pk,
    )
    record_activity(
        actor,
        ACTIVITY_MEMBERSHIP_ADD,
        project_id=project_id,
        after={"agent_id": str(agent.pk), "role": agent.role, "enabled": agent.enabled},
    )
    enqueue_outbox(
        EVENT_MEMBERSHIP_ADDED,
        member.pk,
        {
            "project_id": str(project_id),
            "agent_id": str(agent.pk),
            "membership_id": str(member.pk),
            "actor_type": actor.actor_type.value,
        },
    )
    return data


def remove_member(
    actor: ActorContext,
    project_id,
    agent_id,
    payload: dict[str, Any],
    idem_key: str | None,
) -> dict:
    """Revoke one project authorisation, applying the requested execution disposition."""
    assert_in_transaction()
    disposition = _parse_disposition(payload)
    reason = _parse_reason(payload)

    request_hash = canonical_request_hash(
        operation=OP_MEMBERSHIP_REMOVE,
        body=payload,
        resource_id=agent_id,
        project_id=project_id,
    )

    plan, _membership_ids, _executions = build_revocation_plan(
        agent_id, execution_project_ids={project_id}
    )
    plan.add_project(project_id)
    locked = acquire_plan(plan)

    project = locked.projects.get(project_id)
    if project is None:
        raise ResourceNotFoundError("项目不存在或不可访问")
    require_project_editable(project)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")

    member = ProjectMember.objects.filter(project_id=project_id, agent_id=agent_id).first()
    if member is None:
        raise ResourceNotFoundError("成员关系不存在")

    scope = revalidate_revocation_scope(agent_id, plan, execution_project_ids={project_id})

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_MEMBERSHIP_REMOVE,
        key=idem_key,
        request_hash=request_hash,
        resource_type="project_member",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    if scope and disposition is None:
        raise InvalidArgumentError(
            "该成员在本项目持有有效执行，必须明确选择等待到期(WAIT)或立即回收(NOW)",
            details={"project_id": str(project_id), "agent_id": str(agent_id), "open_executions": len(scope)},
        )
    if disposition == DISPOSITION_NOW and not reason:
        raise InvalidArgumentError("立即回收必须填写原因", details={"field": "reason"})
    if (
        agent.enabled
        and agent.role == AgentRole.SUPERVISOR.value
        and project.status == ProjectStatus.ACTIVE.value
    ):
        require_enabled_supervisors_preserved({project_id}, removed_agent_ids={agent_id})

    member_id = member.pk
    member.delete()

    ended: list[dict[str, Any]] = []
    if disposition == DISPOSITION_NOW and scope:
        ended = reclaim_executions(
            actor,
            executions=locked_executions(locked, scope),
            tasks=locked.tasks,
            reason=reason,
        )

    data = {
        "membership_id": str(member_id),
        "project_id": str(project_id),
        "agent_id": str(agent_id),
        "role": agent.role,
        "disposition": disposition or DISPOSITION_WAIT,
        "reason": reason,
        "reclaimed_execution_ids": [item["execution_id"] for item in ended],
    }
    complete_receipt(
        receipt,
        response_data=data,
        http_status=200,
        resource_type="project_member",
        resource_id=member_id,
    )
    record_activity(
        actor,
        ACTIVITY_MEMBERSHIP_REMOVE,
        project_id=project_id,
        reason=reason,
        before={"agent_id": str(agent_id), "role": agent.role, "enabled": agent.enabled},
        after={
            "disposition": disposition or DISPOSITION_WAIT,
            "reclaimed_execution_count": len(ended),
            "remaining_authorised": False,
        },
    )
    enqueue_outbox(
        EVENT_MEMBERSHIP_REMOVED,
        member_id,
        {
            "project_id": str(project_id),
            "agent_id": str(agent_id),
            "membership_id": str(member_id),
            "disposition": disposition or DISPOSITION_WAIT,
            "reclaimed_execution_ids": [item["execution_id"] for item in ended],
            "actor_type": actor.actor_type.value,
            "reason": reason,
        },
    )
    return data


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------


def _parse_disposition(payload: dict[str, Any]) -> str | None:
    value = payload.get("disposition")
    if value is None:
        return None
    if not isinstance(value, str) or value.upper() not in DISPOSITIONS:
        raise InvalidArgumentError(
            "disposition 必须是 WAIT 或 NOW",
            details={"field": "disposition", "allowed": list(DISPOSITIONS)},
        )
    return value.upper()


def _parse_reason(payload: dict[str, Any]) -> str:
    value = payload.get("reason", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidArgumentError("reason 必须是字符串", details={"field": "reason"})
    value = value.strip()
    if len(value) > REASON_MAX_LENGTH:
        raise InvalidArgumentError(
            f"reason 超过长度上限 {REASON_MAX_LENGTH}",
            details={"field": "reason", "max_length": REASON_MAX_LENGTH},
        )
    return value


def _replay_payload(receipt) -> dict:
    stored = receipt.response_data
    payload = dict(stored) if isinstance(stored, dict) else {"data": stored}
    payload["idempotent_replay"] = True
    return payload


__all__ = [
    "ACTION_ADD",
    "ACTION_REMOVE",
    "ACTIVITY_EXECUTION_RECLAIM",
    "ACTIVITY_MEMBERSHIP_ADD",
    "ACTIVITY_MEMBERSHIP_REMOVE",
    "DISPOSITION_NOW",
    "DISPOSITION_WAIT",
    "DISPOSITIONS",
    "EVENT_EXECUTION_RECLAIMED",
    "EVENT_MEMBERSHIP_ADDED",
    "EVENT_MEMBERSHIP_REMOVED",
    "OP_MEMBERSHIP_ADD",
    "OP_MEMBERSHIP_REMOVE",
    "active_project_ids",
    "add_member",
    "build_revocation_plan",
    "list_members",
    "locked_executions",
    "member_view",
    "open_execution_scope",
    "project_ids_for_agent",
    "reclaim_executions",
    "remove_member",
    "require_enabled_supervisors_preserved",
    "revalidate_revocation_scope",
    "update_membership",
]
