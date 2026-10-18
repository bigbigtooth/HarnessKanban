"""Unified visibility, role and current-execution write guard (DEVELOP D06/D09/D13).

Every read entry point starts from the accessible project scope; nothing fetches all rows
and hides them later in the UI. Unauthorised resources answer 404 (never 403) so object
existence cannot be enumerated. The write guard is shared by progress, attachment binding
and submission so all three agree on current execution, ownership, fixed deadline and key
version.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apps.identity.models import Agent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskExecution
from domain.contracts import ActorContext, ActorType, AgentRole, ProjectStatus, TaskStatus
from domain.errors import (
    AgentDisabledError,
    AgentTokenInvalidError,
    ExecutionNotCurrentError,
    ExecutionTimeoutError,
    ResourceNotFoundError,
    RoleForbiddenError,
    VersionConflictError,
)
from domain.security import hash_agent_key

logger = logging.getLogger("domain.permissions")


# --------------------------------------------------------------------------------------
# Agent identity
# --------------------------------------------------------------------------------------


def authenticate_agent_token(raw_token: str | None) -> Agent:
    """Resolve a request token to an enabled Agent.

    Order: header present, digest recognisable, agent enabled. A rotated or revoked key
    therefore fails immediately, and the comparison is over digests only.
    """
    from domain.errors import AgentTokenMissingError

    if not raw_token:
        raise AgentTokenMissingError()
    digest = hash_agent_key(raw_token)
    agent = Agent.objects.filter(key_hash=digest).first()
    if agent is None:
        raise AgentTokenInvalidError()
    if not agent.enabled:
        raise AgentDisabledError()
    return agent


def _verify_actor_agent(actor: ActorContext, agent: Agent | None) -> Agent:
    """Shared identity checks applied to a freshly read agent row."""
    if agent is None:
        raise AgentTokenInvalidError()
    if not agent.enabled:
        raise AgentDisabledError()
    if actor.key_version is not None and actor.key_version != agent.key_version:
        raise AgentTokenInvalidError(
            "认证后 Key 已被重置，拒绝旧请求继续写入",
            details={
                "authenticated_key_version": actor.key_version,
                "current_key_version": agent.key_version,
            },
        )
    return agent


def load_actor_agent_locked(actor: ActorContext) -> Agent:
    """Re-read the authenticated agent under the transaction's protection.

    Detects a key reset or a disable that happened between authentication and the write.
    Requires the caller to be inside a transaction: on MySQL ``SELECT ... FOR UPDATE``
    outside one is an error, and SQLite silently ignores the lock, so a write path must
    never rely on this without an explicit transaction.
    """
    if actor.actor_type is not ActorType.AGENT or actor.agent_id is None:
        raise RoleForbiddenError("该操作需要 Agent 身份")
    agent = Agent.objects.select_for_update().filter(pk=actor.agent_id).first()
    return _verify_actor_agent(actor, agent)


def load_actor_agent_for_read(actor: ActorContext) -> Agent:
    """Read-only re-check for read paths: same checks, without taking a row lock.

    Reads must not require a transaction (and must not block claims by holding a lock on the
    agent row). A read path only needs a fresh view of ``enabled`` and the key version; the
    strong "no write after revocation" guarantee is enforced by
    :func:`load_actor_agent_locked` on every write path.
    """
    if actor.actor_type is not ActorType.AGENT or actor.agent_id is None:
        raise RoleForbiddenError("该操作需要 Agent 身份")
    agent = Agent.objects.filter(pk=actor.agent_id).first()
    return _verify_actor_agent(actor, agent)


def require_supervisor(agent: Agent) -> None:
    if agent.role != AgentRole.SUPERVISOR.value:
        raise RoleForbiddenError("员工不能执行该操作")


# --------------------------------------------------------------------------------------
# Project scope
# --------------------------------------------------------------------------------------


def accessible_project_ids(agent_id) -> set:
    """Agent 可访问的项目：成员授权 ∩ 未被软删除的项目。"""
    return set(
        ProjectMember.objects.filter(
            agent_id=agent_id, project__deleted_at__isnull=True
        ).values_list("project_id", flat=True)
    )


def require_membership(agent_id, project_id) -> None:
    if not ProjectMember.objects.filter(agent_id=agent_id, project_id=project_id).exists():
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")


def supervisor_project_ids(agent: Agent) -> set:
    """Projects where this agent currently holds the supervisor role."""
    if agent.role != AgentRole.SUPERVISOR.value or not agent.enabled:
        return set()
    return accessible_project_ids(agent.pk)


def require_project_supervisor(actor: ActorContext, project: Project) -> Agent | None:
    """Return the acting agent when the request is an Agent request with supervisor role."""
    if actor.actor_type is ActorType.WEB_OPERATOR:
        # The local WebAPI has no login: it acts as the trusted local administrator.
        return None
    agent = load_actor_agent_locked(actor)
    require_supervisor(agent)
    if not ProjectMember.objects.filter(agent=agent, project=project).exists():
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    return agent


def require_project_editable(project: Project) -> None:
    if project.status == ProjectStatus.ARCHIVED.value:
        raise ResourceNotFoundError("项目已归档，不接受写操作")


def require_project_active(project: Project) -> None:
    if project.status != ProjectStatus.ACTIVE.value:
        from domain.errors import ProjectNotActiveError

        raise ProjectNotActiveError(details={"project_status": project.status})


# --------------------------------------------------------------------------------------
# Task visibility
# --------------------------------------------------------------------------------------


def task_is_visible_to(agent: Agent | None, task: Task, project_ids: set) -> bool:
    """Workers never see PREPARING tasks; PREPARING is supervisor-only content."""
    if task.project_id not in project_ids:
        return False
    if task.status == TaskStatus.PREPARING.value:
        return agent is not None and agent.role == AgentRole.SUPERVISOR.value
    return True


def visible_tasks_queryset(actor: ActorContext, project_ids: set):
    """Base queryset for every task read path; never fetch-then-hide."""
    queryset = Task.objects.filter(project_id__in=project_ids)
    if actor.actor_type is ActorType.WEB_OPERATOR:
        return queryset
    agent = load_actor_agent_locked(actor)
    if agent.role != AgentRole.SUPERVISOR.value:
        queryset = queryset.exclude(status=TaskStatus.PREPARING.value)
    return queryset


def require_task_readable(actor: ActorContext, task: Task) -> Agent | None:
    """404 for a non-member, for a worker reading a PREPARING task, and for archived reads."""
    if actor.actor_type is ActorType.WEB_OPERATOR:
        return None
    agent = load_actor_agent_locked(actor)
    project_ids = accessible_project_ids(agent.pk)
    if not task_is_visible_to(agent, task, project_ids):
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    return agent


def require_task_supervisor(actor: ActorContext, task: Task) -> Agent | None:
    if actor.actor_type is ActorType.WEB_OPERATOR:
        return None
    agent = load_actor_agent_locked(actor)
    require_supervisor(agent)
    require_membership(agent.pk, task.project_id)
    return agent


# --------------------------------------------------------------------------------------
# Current execution write guard
# --------------------------------------------------------------------------------------


def current_execution_write_guard(
    actor: ActorContext,
    *,
    task: Task,
    execution_id,
    now: datetime,
    allow_web_operator: bool = False,
) -> TaskExecution:
    """Shared guard for progress, attachment binding and submission.

    Checks the current execution id, ownership, that it has not ended, that the database
    clock is still before the fixed deadline, and that an agent request still carries the
    current key version.
    """
    if actor.actor_type is ActorType.WEB_OPERATOR and allow_web_operator:
        acting_agent = None
    elif actor.actor_type is ActorType.WEB_OPERATOR:
        raise RoleForbiddenError("该操作不支持 Web 代操作")
    else:
        acting_agent = load_actor_agent_locked(actor)
        require_membership(acting_agent.pk, task.project_id)

    if task.status != TaskStatus.IN_PROGRESS.value:
        raise ExecutionNotCurrentError(
            "任务当前不是进行中，缺少可写入的当前执行",
            details={"task_status": task.status},
        )
    if task.active_execution_id is None or str(task.active_execution_id) != str(execution_id):
        raise ExecutionNotCurrentError(
            "执行 ID 不是当前有效执行",
            details={"current_execution_id": _str(task.active_execution_id)},
        )

    execution = TaskExecution.objects.select_for_update().filter(pk=task.active_execution_id).first()
    if execution is None:
        raise ExecutionNotCurrentError("当前执行记录不存在")

    if acting_agent is not None and execution.agent_id != acting_agent.pk:
        raise ExecutionNotCurrentError("当前执行属于其他 Agent")

    if execution.ended_at is not None:
        raise ExecutionNotCurrentError("当前执行已经结束")

    if execution.task_id != task.pk:
        raise VersionConflictError("执行记录与任务不匹配", details={"task_id": str(task.pk)})

    if now >= execution.execution_deadline_at:
        raise ExecutionTimeoutError(
            details={
                "execution_deadline_at": execution.execution_deadline_at.isoformat(),
                "now": now.isoformat(),
            }
        )
    return execution


def execution_owner_ids(execution: TaskExecution) -> set:
    return {execution.agent_id}


def _str(value) -> str | None:
    return None if value is None else str(value)
