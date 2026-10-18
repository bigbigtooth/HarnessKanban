"""Atomic claim under the singleton global lock (DEVELOP D12 / product 7.2, 14.2).

Everything happens inside one outermost transaction that starts by really updating
``TaskClaimLock.id=1``. Candidate discovery, expired-slot recovery, capacity reservation,
the task CAS and the receipt all commit together, so a claim can never leave a reserved
slot without an execution, or an execution without a claimed task.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from django.db.models import Case, F, IntegerField, Q, When

from apps.identity.models import Agent
from apps.projects.models import ProjectMember
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now
from domain.contracts import (
    OP_CLAIM_AGENT,
    OP_CLAIM_WEB,
    ActorContext,
    ActorType,
    AgentRole,
    ClaimMode,
    DIFFICULTY_TIMEOUT_SECONDS,
    EndReason,
    ProjectStatus,
    TaskStatus,
)
from domain.errors import (
    CapacityExceededError,
    InvalidArgumentError,
    ResourceNotFoundError,
    TaskAlreadyClaimedError,
    TaskDifficultyMismatchError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.execution import finish_execution_locked, reset_in_progress_task_locked
from domain.idempotency import Receipt, canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan, require
from domain.permissions import (
    accessible_project_ids,
    load_actor_agent_locked,
    require_membership,
)
from domain.presenters import execution_view, iso, task_summary

logger = logging.getLogger("domain.claim")

NO_MATCHING_TASK = "NO_MATCHING_TASK"


@dataclass
class ClaimRequest:
    """A normalised claim request. ``levels`` may be empty (all-zero selection)."""

    levels: list[int]
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    expected_version: int | None = None
    target_agent_id: uuid.UUID | None = None
    source: str = "agent"
    compensation_limit: int = 100
    fields: dict = field(default_factory=dict)

    @property
    def operation(self) -> str:
        return OP_CLAIM_WEB if self.source == "web" else OP_CLAIM_AGENT


def claim_task(actor: ActorContext, request: ClaimRequest, idem_key: str) -> dict:
    """Claim at most one task. Must be called inside ``run_request_service(global_claim_lock=True)``."""
    agent = _resolve_claiming_agent(actor, request)
    request_hash = canonical_request_hash(
        operation=request.operation,
        body={key: value for key, value in request.fields.items()},
        resource_id=request.task_id,
        project_id=request.project_id,
        levels=request.levels,
    )

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=request.operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay(agent, receipt)

    now = db_now()
    result = _claim_new(actor, agent, request, now)
    if result.empty:
        _write_events(actor, agent, None, None, reason=NO_MATCHING_TASK)
        complete_receipt(
            receipt,
            response_data={"data": None, "reason": NO_MATCHING_TASK},
            resource_type="task",
            resource_id=None,
        )
        return {"data": None, "reason": NO_MATCHING_TASK, "idempotent_replay": False}

    task, execution = result.task, result.execution
    payload = _claim_payload(task, execution, agent)
    _write_events(actor, agent, task, execution, reason="claimed")
    complete_receipt(
        receipt,
        response_data=payload,
        resource_type="task",
        resource_id=task.pk,
    )
    return {**payload, "idempotent_replay": False}


def _write_events(
    actor: ActorContext, agent: Agent, task: Task | None, execution: TaskExecution | None, *, reason: str
) -> None:
    """Record exactly one activity event and (for a real claim) one outbox event."""
    if task is None or execution is None:
        record_activity(actor, "claim.no_matching_task", reason=reason)
        return
    record_activity(
        actor,
        "claimed",
        task_id=task.pk,
        project_id=task.project_id,
        execution_id=execution.pk,
        before={"status": TaskStatus.TODO.value},
        after={
            "status": TaskStatus.IN_PROGRESS.value,
            "assignee_agent_id": str(agent.pk),
            "execution_id": str(execution.pk),
        },
        reason=reason,
    )
    enqueue_outbox(
        "task.claimed",
        task.pk,
        {
            "task_id": str(task.pk),
            "project_id": str(task.project_id),
            "execution_id": str(execution.pk),
            "agent_id": str(agent.pk),
        },
    )


@dataclass
class _ClaimOutcome:
    task: Task | None = None
    execution: TaskExecution | None = None

    @property
    def empty(self) -> bool:
        return self.task is None


def _resolve_claiming_agent(actor: ActorContext, request: ClaimRequest) -> Agent:
    if actor.actor_type is ActorType.AGENT:
        if request.target_agent_id is not None and request.target_agent_id != actor.agent_id:
            # Body-supplied identity is never trusted.
            raise InvalidArgumentError("请求体不能指定其他 Agent 作为领取者")
        return load_actor_agent_locked(actor)

    if actor.actor_type is ActorType.WEB_OPERATOR:
        if request.target_agent_id is None:
            raise InvalidArgumentError("Web 代领必须显式提供 agent_id")
        agent = Agent.objects.select_for_update().filter(pk=request.target_agent_id).first()
        if agent is None:
            raise ResourceNotFoundError("指定的 Agent 不存在")
        if not agent.enabled:
            raise InvalidArgumentError("指定的 Agent 已被禁用", details={"agent_id": str(agent.pk)})
        return agent

    raise InvalidArgumentError("system 来源不能发起领取")


def _replay(agent: Agent, receipt: Receipt) -> dict:
    """Return the original receipt after re-checking current read authorisation.

    The original execution must NOT still be valid, and a later reassignment or project
    closure must not hide a historical success the caller may still read.
    """
    response = receipt.response_data or {"data": None, "reason": NO_MATCHING_TASK}
    resource_id = receipt.resource_id
    if resource_id is not None:
        task = Task.objects.filter(pk=resource_id).first()
        if task is None:
            raise ResourceNotFoundError("原领取任务不存在或不可访问")
        member_projects = accessible_project_ids(agent.pk)
        if task.project_id not in member_projects:
            raise ResourceNotFoundError("原领取任务不存在或不可访问")
        if task.status == TaskStatus.PREPARING.value and agent.role != AgentRole.SUPERVISOR.value:
            raise ResourceNotFoundError("原领取任务不存在或不可访问")
    if isinstance(response, dict) and "data" in response and response.get("data") is None:
        return {"data": None, "reason": response.get("reason", NO_MATCHING_TASK), "idempotent_replay": True}
    return {**response, "idempotent_replay": True}


def _claim_new(actor: ActorContext, agent: Agent, request: ClaimRequest, now) -> _ClaimOutcome:
    member_project_ids = accessible_project_ids(agent.pk)
    if not member_project_ids:
        if request.task_id is not None:
            raise ResourceNotFoundError("任务不存在或当前身份无权访问")
        return _ClaimOutcome()

    if request.project_id is not None:
        if request.project_id not in member_project_ids:
            raise ResourceNotFoundError("项目不存在或当前身份无权访问")
        scope_project_ids = {request.project_id}
    else:
        scope_project_ids = set(member_project_ids)

    # --- bounded discovery under the global lock, before any row lock is taken ---------
    own_expired = list(
        TaskExecution.objects.filter(
            agent_id=agent.pk, ended_at__isnull=True, execution_deadline_at__lte=now
        )
        .order_by("execution_deadline_at")
        .values_list("id", flat=True)
    )
    compensation = _compensation_candidates(
        scope_project_ids, now, limit=request.compensation_limit, exclude_agent=agent.pk
    )
    candidate_query = _candidate_queryset(agent, request, scope_project_ids)
    candidate_ids = list(candidate_query.values_list("id", flat=True)[: request.compensation_limit])

    # --- plan the complete lock set, then lock by category ----------------------------
    # A specified task is always planned even when it does not match the level filters, so
    # the caller gets 409 TASK_DIFFICULTY_MISMATCH rather than a misleading 404.
    plan_task_ids = list(candidate_ids)
    if request.task_id is not None and request.task_id not in plan_task_ids:
        plan_task_ids.append(request.task_id)

    plan = _build_plan(own_expired, compensation, plan_task_ids, agent, scope_project_ids)
    locked = acquire_plan(plan)
    _validate_plan(plan, locked)

    # --- system recovery of timed-out executions (own slots first, unbounded in scope) --
    recovered = 0
    recovered += _end_executions(actor, own_expired, locked, now, reason=EndReason.TIMEOUT)
    recovered += _end_executions(actor, compensation, locked, now, reason=EndReason.TIMEOUT)
    if recovered:
        logger.info("领取前补偿回收 %s 个超时执行（来源 system）", recovered)

    locked_agent = locked.agents.get(agent.pk)
    if locked_agent is None:
        raise VersionConflictError("Agent 状态已变化，请重试")
    # Recovery above released slots on this agent's row; re-read the authoritative value
    # while still holding the agent lock instead of trusting the pre-recovery snapshot.
    locked_agent.refresh_from_db(
        fields=["reserved_execution_count", "max_concurrency", "enabled", "role", "version"]
    )
    if not locked_agent.enabled:
        raise ResourceNotFoundError("Agent 已被禁用")
    agent = locked_agent

    # --- candidate selection -----------------------------------------------------------
    # Tasks whose expired execution was just recovered become TODO inside this same
    # transaction and may be claimed now, but only if they were already in the lock plan.
    recovered_task_ids = [
        locked.executions[execution_id].task_id
        for execution_id in (*own_expired, *compensation)
        if execution_id in locked.executions
    ]

    if request.task_id is not None:
        task = _resolve_specified_task(agent, request, locked, scope_project_ids)
    else:
        task = _select_automatic_candidate(
            agent, request, candidate_ids, locked, scope_project_ids, recovered_task_ids
        )
        if task is None:
            return _ClaimOutcome()

    if task is None:
        return _ClaimOutcome()

    _revalidate_membership(agent, task)

    # No candidate at all is an empty success even when capacity is full; capacity is only
    # checked once a real candidate exists.
    if agent.reserved_execution_count >= agent.max_concurrency:
        raise CapacityExceededError(
            details={
                "max_concurrency": agent.max_concurrency,
                "reserved_execution_count": agent.reserved_execution_count,
            }
        )

    reserved = Agent.objects.filter(
        pk=agent.pk, enabled=True, reserved_execution_count__lt=F("max_concurrency")
    ).update(reserved_execution_count=F("reserved_execution_count") + 1)
    if reserved != 1:
        raise CapacityExceededError(
            details={"max_concurrency": agent.max_concurrency}
        )

    execution = _create_execution(task, agent, now)

    cas = Task.objects.filter(
        pk=task.pk,
        status=TaskStatus.TODO.value,
        version=task.version,
        active_execution__isnull=True,
    ).update(
        status=TaskStatus.IN_PROGRESS.value,
        assignee_agent_id=agent.pk,
        active_execution_id=execution.pk,
        claimed_at=now,
        updated_at=now,
        version=F("version") + 1,
    )
    if cas != 1:
        # Rolls back the execution row and the reserved slot with the whole transaction.
        raise TaskAlreadyClaimedError(details={"task_id": str(task.pk)})

    task.refresh_from_db()
    return _ClaimOutcome(task=task, execution=execution)


def _candidate_queryset(agent: Agent, request: ClaimRequest, scope_project_ids):
    queryset = (
        Task.objects.select_related("project")
        .filter(
            project_id__in=scope_project_ids,
            status=TaskStatus.TODO.value,
            is_archived=False,
            project__status=ProjectStatus.ACTIVE.value,
        )
        .filter(Q(claim_mode=ClaimMode.OPEN.value) | Q(designated_agent_id=agent.pk))
    )
    if request.levels:
        queryset = queryset.filter(difficulty__in=request.levels)
    else:
        queryset = queryset.none()
    if request.task_id is not None:
        queryset = queryset.filter(pk=request.task_id)
    return queryset.annotate(
        # A task that matches because it is designated to this agent is considered first;
        # the OPEN branch only matches through the claim-mode OR above.
        designated_first=Case(
            When(designated_agent_id=agent.pk, then=0), default=1, output_field=IntegerField()
        )
    ).order_by("designated_first", "sort_order", "created_at", "id")


def _compensation_candidates(scope_project_ids, now, *, limit: int, exclude_agent):
    """Bounded, in-scope timed-out executions. Never grants arbitrary reclaim rights."""
    return list(
        TaskExecution.objects.filter(
            ended_at__isnull=True,
            execution_deadline_at__lte=now,
            task__project_id__in=scope_project_ids,
        )
        .exclude(agent_id=exclude_agent)
        .order_by("execution_deadline_at", "id")
        .values_list("id", flat=True)[:limit]
    )


def _build_plan(own_expired, compensation, candidate_ids, agent: Agent, scope_project_ids) -> LockPlan:
    """Plan the complete lock set up front.

    Only projects that are actually referenced by a candidate or a recovered execution are
    locked (plus the claiming agent). Locks are never extended after the plan was
    acquired, so a task lock is never followed by a newly discovered project/agent lock.
    """
    plan = LockPlan()
    plan.add_agent(agent.pk)

    def absorb(execution_ids) -> None:
        if not execution_ids:
            return
        rows = TaskExecution.objects.filter(pk__in=execution_ids).values(
            "id", "task_id", "agent_id", "task__project_id"
        )
        for row in rows:
            plan.add_execution(row["id"])
            plan.add_task(row["task_id"])
            plan.add_agent(row["agent_id"])
            plan.add_project(row["task__project_id"])

    absorb(own_expired)
    absorb(compensation)

    if candidate_ids:
        for row in Task.objects.filter(pk__in=candidate_ids).values(
            "id", "project_id", "designated_agent_id"
        ):
            plan.add_task(row["id"])
            plan.add_project(row["project_id"])
            plan.add_agent(row["designated_agent_id"])
    return plan


def _validate_plan(plan: LockPlan, locked) -> None:
    """Re-read the relations after locking; a changed relation invalidates the plan.

    This only *verifies* membership of the already-acquired plan. Extending the plan here
    would mean holding a task lock while fetching an unplanned agent or project lock,
    which is forbidden.
    """
    for execution_id in plan.execution_ids:
        execution = locked.executions.get(execution_id)
        require(execution is not None, f"执行 {execution_id} 未在锁计划内")
        require(plan.contains_task(execution.task_id), f"执行 {execution_id} 所属任务不在锁计划内")
        require(plan.contains_agent(execution.agent_id), f"执行 {execution_id} 的执行者不在锁计划内")
        require(
            plan.contains_project(execution.task.project_id),
            f"执行 {execution_id} 所属项目不在锁计划内",
        )
    for task_id in plan.task_ids:
        task = locked.tasks.get(task_id)
        require(task is not None, f"任务 {task_id} 未在锁计划内")
        require(plan.contains_project(task.project_id), f"任务 {task_id} 所属项目不在锁计划内")
        require(
            plan.contains_agent(task.designated_agent_id),
            f"任务 {task_id} 的指定 Agent 不在锁计划内",
        )
    for agent_id in plan.agent_ids:
        require(locked.agents.get(agent_id) is not None, f"Agent {agent_id} 未在锁计划内")
    for project_id in plan.project_ids:
        require(locked.projects.get(project_id) is not None, f"项目 {project_id} 未在锁计划内")


def _end_executions(actor: ActorContext, execution_ids, locked, now, *, reason: EndReason) -> int:
    ended_count = 0
    system_actor = ActorContext(
        actor_type=ActorType.SYSTEM, request_id=actor.request_id, web_operator_note=None
    )
    for execution_id in execution_ids:
        execution = locked.executions.get(execution_id)
        if execution is None or execution.ended_at is not None:
            continue
        task = locked.tasks.get(execution.task_id)
        if task is None:
            continue

        ended = finish_execution_locked(execution, ended_at=now, end_reason=reason.value)
        if not ended:
            continue
        ended_count += 1
        execution.ended_at = now
        execution.end_reason = reason.value

        if task.status == TaskStatus.IN_PROGRESS.value and task.active_execution_id == execution.pk:
            reset_in_progress_task_locked(task, expected_execution_id=execution.pk, now=now)
            task.refresh_from_db()
            record_activity(
                system_actor,
                "task.timeout_recovered",
                task_id=task.pk,
                project_id=task.project_id,
                execution_id=execution.pk,
                before={"status": TaskStatus.IN_PROGRESS.value},
                after={"status": TaskStatus.TODO.value},
                reason="固定执行时限到期，system 规则回收",
            )
            enqueue_outbox(
                "task.timeout_recovered",
                task.pk,
                {"task_id": str(task.pk), "execution_id": str(execution.pk)},
            )
    return ended_count


def _resolve_specified_task(agent, request: ClaimRequest, locked, scope_project_ids) -> Task:
    task = locked.tasks.get(request.task_id)
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    if task.project_id not in scope_project_ids:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    if request.expected_version is None:
        raise InvalidArgumentError("指定 task_id 领取必须携带 expected_version")
    if request.levels and task.difficulty not in request.levels:
        raise TaskDifficultyMismatchError(
            details={"task_difficulty": task.difficulty, "selected_levels": request.levels}
        )
    if not request.levels:
        # An all-zero level selection can never match a concrete task.
        raise TaskDifficultyMismatchError(details={"selected_levels": []})
    if task.status != TaskStatus.TODO.value or task.is_archived:
        raise TaskAlreadyClaimedError(details={"task_status": task.status})
    if task.version != request.expected_version:
        raise VersionConflictError(
            details={"expected_version": request.expected_version, "current_version": task.version}
        )
    if task.claim_mode == ClaimMode.ASSIGNED.value and task.designated_agent_id != agent.pk:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    return task


def _select_automatic_candidate(
    agent, request: ClaimRequest, candidate_ids, locked, scope_project_ids, extra_task_ids=()
):
    """Pick one claimable TODO task from the locked plan in server order."""
    ordered_ids = list(candidate_ids) + [task_id for task_id in extra_task_ids if task_id not in set(candidate_ids)]
    seen = set()
    for candidate_id in ordered_ids:
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        task = locked.tasks.get(candidate_id)
        if task is None:
            continue
        if not _is_claimable_now(agent, request, task, scope_project_ids):
            continue
        return task
    return None


def _is_claimable_now(agent: Agent, request: ClaimRequest, task: Task, scope_project_ids) -> bool:
    if task.status != TaskStatus.TODO.value or task.is_archived:
        return False
    if task.project_id not in scope_project_ids:
        return False
    if task.project.status != ProjectStatus.ACTIVE.value:
        return False
    if request.levels and task.difficulty not in request.levels:
        return False
    if not request.levels:
        return False
    if task.claim_mode == ClaimMode.ASSIGNED.value and task.designated_agent_id != agent.pk:
        return False
    return True


def _revalidate_membership(agent: Agent, task: Task) -> None:
    """Re-check authorisation after the project lock is held.

    A concurrent revocation must already hold this project row, so re-reading here closes
    the window between the authorisation check and the write.
    """
    if not ProjectMember.objects.filter(project_id=task.project_id, agent_id=agent.pk).exists():
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")


def _create_execution(task: Task, agent: Agent, now) -> TaskExecution:
    last_sequence = (
        TaskExecution.objects.filter(task=task).order_by("-sequence").values_list("sequence", flat=True).first()
    )
    sequence = (last_sequence or 0) + 1
    timeout_seconds = DIFFICULTY_TIMEOUT_SECONDS[task.difficulty]
    context_version, document_refs = _context_snapshot(task)

    from datetime import timedelta

    return TaskExecution.objects.create(
        task=task,
        agent=agent,
        sequence=sequence,
        difficulty_snapshot=task.difficulty,
        execution_timeout_seconds=timeout_seconds,
        requirements_snapshot={
            "title": task.title,
            "description": task.description,
            "acceptance_criteria": task.acceptance_criteria,
            "difficulty": task.difficulty,
            "requirements_version": task.requirements_version,
            "project_context_version": None if context_version is None else context_version.number,
            "document_version_refs": document_refs,
        },
        context_version=context_version,
        claimed_at=now,
        execution_deadline_at=now + timedelta(seconds=timeout_seconds),
    )


def _context_snapshot(task: Task):
    """Freeze the project context version at claim time.

    Delegates to the single implementation in ``domain.context_service`` so the snapshot
    rule cannot drift between the claim path and the context module.
    """
    from domain.context_service import current_context_for_claim

    return current_context_for_claim(task.project)


def _claim_payload(task: Task, execution: TaskExecution, agent: Agent) -> dict:
    data = {
        "task_id": str(task.pk),
        "project_id": str(task.project_id),
        "number": task.number,
        "display_number": f"TASK-{task.number:04d}",
        "title": task.title,
        "description": task.description,
        "acceptance_criteria": task.acceptance_criteria,
        "difficulty": task.difficulty,
        "status": task.status,
        "claim_mode": task.claim_mode,
        "version": task.version,
        "requirements_version": task.requirements_version,
        "assignee_agent_id": str(agent.pk),
        "execution_id": str(execution.pk),
        "claimed_at": iso(execution.claimed_at),
        "execution_timeout_seconds": execution.execution_timeout_seconds,
        "execution_deadline_at": iso(execution.execution_deadline_at),
        "requirements_snapshot": execution.requirements_snapshot,
        "execution": execution_view(execution),
        "task": task_summary(task),
    }
    return {"data": data}


def web_claim_request(
    *, agent_id, task_id, expected_version: int | None, levels: list[int] | None = None, fields=None
) -> ClaimRequest:
    """Web operator proxy claim for one explicit task."""
    return ClaimRequest(
        levels=levels if levels is not None else [1, 2, 3, 4, 5],
        task_id=task_id,
        expected_version=expected_version,
        target_agent_id=agent_id,
        source="web",
        fields=fields or {},
    )
