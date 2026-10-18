"""Global dashboard statistics and recent activity (T25, DEVELOP D17).

Scope is the **first** step, never a filter applied after aggregation:

* the authorised project set (the local administrator sees every project, an agent only its
  member projects) plus the actor's task visibility build the base queryset, and every
  figure - task counts, workload, exceptions - is derived from that one queryset;
* a worker never sees PREPARING content, so its PREPARING count is 0 and the payload marks
  the scope ``PUBLISHED_ONLY`` (product 9.2 / A03). A supervisor sees the real counts;
* the five status counts, the five difficulty counts, the task total, the archived delta
  and the overdue count come from **one conditional aggregate**, so they cannot describe two
  different instants (D17). The remaining figures (per-project breakdown, workload,
  exceptions) are derived from the same base queryset with the same ``as_of`` inside one
  read transaction; each of them says why it needs its own statement rather than counting
  tasks a second way.

History is never a task: executions, submissions and reviews feed the workload and exception
figures only, never the task counts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskExecution
from domain.agent_service import (
    ONLINE_ACTIVE_STATES,
    ONLINE_NEVER_CONNECTED,
    ONLINE_OFFLINE,
    online_state_for,
)
from domain.clock import db_now
from domain.contracts import (
    DIFFICULTY_LEVELS,
    ActorContext,
    ActorType,
    AgentRole,
    ClaimMode,
    ProjectStatus,
    TaskStatus,
    display_number,
)
from domain.errors import InvalidArgumentError, ResourceNotFoundError
from domain.permissions import require_membership, visible_tasks_queryset
from domain.presenters import activity_view, iso, page, uuid_str
from domain.read_task_query import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, reader_scope

# Task visibility markers returned inside ``scope`` (product 9.2).
TASK_VISIBILITY_FULL = "FULL"
TASK_VISIBILITY_PUBLISHED_ONLY = "PUBLISHED_ONLY"

# Empty scope: a rate is never reported as 100% and never invented.
NO_TASKS_REASON = "NO_TASKS"

# Machine-readable exception reasons, mirroring the ``REASON_*`` style of read_task_query.
REASON_EXECUTION_OVERDUE = "EXECUTION_OVERDUE"
REASON_DESIGNATED_AGENT_DISABLED = "DESIGNATED_AGENT_DISABLED"
REASON_DESIGNATED_AGENT_NOT_MEMBER = "DESIGNATED_AGENT_NOT_MEMBER"

EXCEPTION_OVERDUE_RECLAIM = "OVERDUE_RECLAIM_REQUIRED"
EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT = "UNAVAILABLE_DESIGNATED_AGENT"
EXCEPTION_MIXED = "OVERDUE_RECLAIM_REQUIRED+UNAVAILABLE_DESIGNATED_AGENT"

# The exception list is capped; ``exception_counts`` always carries the true totals.
EXCEPTION_LIMIT = 20

ACTIVITY_ACTION_MAX_LENGTH = 64


@dataclass(frozen=True)
class DashboardScope:
    """Applied scope of one dashboard request.

    ``project_id`` narrows to a single authorised project (an agent asking for a project it
    is not a member of gets 404); ``include_archived`` re-includes archived projects and
    archived tasks, which are excluded by default.
    """

    project_id: uuid.UUID | None = None
    include_archived: bool = False


@dataclass(frozen=True)
class ActivityFilters:
    """Filters of the global activity feed."""

    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    action: str | None = None
    include_archived: bool = False

    def validate(self) -> None:
        if self.action is not None:
            action = self.action.strip()
            if not action:
                raise InvalidArgumentError("action 不能为空", details={"parameter": "action"})
            if len(action) > ACTIVITY_ACTION_MAX_LENGTH:
                raise InvalidArgumentError(
                    f"action 超过长度上限 {ACTIVITY_ACTION_MAX_LENGTH}",
                    details={"parameter": "action", "max_length": ACTIVITY_ACTION_MAX_LENGTH},
                )


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


def get_dashboard(actor: ActorContext, scope: DashboardScope | None = None) -> dict:
    """Scope-consistent dashboard payload (CONTRACT.md §5 ``get_dashboard``).

    Read-only: no business row is written, no capacity slot is touched and the singleton
    claim lock is not taken.
    """
    scope = scope or DashboardScope()
    # ``visible_tasks_queryset`` re-reads the caller under the transaction (a revoked key or
    # a disabled agent must not keep reading), which needs the surrounding transaction that
    # ``list_tasks`` opens for the same reason.
    with transaction.atomic():
        return _dashboard(actor, scope)


def list_activity(
    actor: ActorContext,
    filters: ActivityFilters | None = None,
    *,
    page_number: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Newest-first activity feed restricted to the caller's scope.

    The WebAPI's local administrator sees every project plus the agent-level events that
    carry no project; an agent request is restricted to its member projects and never sees
    another agent's project-less lifecycle events.
    """
    if page_number < 1:
        raise InvalidArgumentError("page 必须大于等于 1", details={"page": page_number})
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise InvalidArgumentError(
            f"page_size 必须在 1 到 {MAX_PAGE_SIZE} 之间", details={"page_size": page_size}
        )
    filters = filters or ActivityFilters()
    filters.validate()
    with transaction.atomic():
        return _list_activity(actor, filters, page_number=page_number, page_size=page_size)


# --------------------------------------------------------------------------------------
# Scope resolution
# --------------------------------------------------------------------------------------


def _require_project_access(agent: Agent | None, project_id: uuid.UUID) -> None:
    """An unauthorised project filter is a 404, never a partial answer."""
    if agent is not None:
        require_membership(agent.pk, project_id)
        return
    if not Project.objects.filter(pk=project_id).exists():
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")


def _scoped_project_ids(agent: Agent | None, member_project_ids: set, project_id) -> set:
    if project_id is None:
        return set(member_project_ids)
    _require_project_access(agent, project_id)
    return {project_id}


def _task_visibility(agent: Agent | None) -> str:
    if agent is not None and agent.role != AgentRole.SUPERVISOR.value:
        return TASK_VISIBILITY_PUBLISHED_ONLY
    return TASK_VISIBILITY_FULL


# --------------------------------------------------------------------------------------
# Conditional-count predicates (shared by every aggregate in this module)
# --------------------------------------------------------------------------------------


def _visible_q(counted_project_ids: set) -> Q:
    """The default archive rule: non-archived tasks of non-archived projects."""
    return Q(is_archived=False) & Q(project_id__in=list(counted_project_ids))


def _archived_q(counted_project_ids: set) -> Q:
    """Exactly the rows the archive toggle adds back."""
    return Q(is_archived=True) | ~Q(project_id__in=list(counted_project_ids))


def _summary_counts(*, include_archived: bool, visible_q: Q, archived_q: Q, as_of) -> dict[str, Count]:
    """Total, archived delta, overdue and the five status counts as conditional counts."""
    scoped = (lambda condition: condition) if include_archived else (
        lambda condition: condition & visible_q
    )
    counts: dict[str, Count] = {
        "task_total": Count("id") if include_archived else Count("id", filter=visible_q),
        # How many tasks the toggle adds: archived tasks plus tasks of archived projects.
        "archived_task_total": Count("id", filter=archived_q),
        # Overdue (product 9.2): past its planned due time and not finished yet.
        "overdue_task_total": Count(
            "id",
            filter=scoped(
                Q(due_at__isnull=False, due_at__lte=as_of) & ~Q(status=TaskStatus.DONE.value)
            ),
        ),
    }
    for status in TaskStatus:
        counts[f"status_{status.value}"] = Count("id", filter=scoped(Q(status=status.value)))
    return counts


def _difficulty_counts(*, include_archived: bool, visible_q: Q) -> dict[str, Count]:
    counts: dict[str, Count] = {}
    for level in DIFFICULTY_LEVELS:
        condition = Q(difficulty=level)
        counts[f"difficulty_{level}"] = Count(
            "id", filter=condition if include_archived else condition & visible_q
        )
    return counts


# --------------------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------------------


def _dashboard(actor: ActorContext, scope: DashboardScope) -> dict:
    # One database timestamp for the whole payload: every deadline comparison and every
    # derived figure below is measured against this instant, never against a fresh clock.
    as_of = db_now()

    agent, member_project_ids = reader_scope(actor)
    scope_project_ids = _scoped_project_ids(agent, member_project_ids, scope.project_id)

    project_rows = list(
        Project.objects.filter(pk__in=scope_project_ids).order_by("title", "id")
    )
    counted_project_ids = {
        row.pk
        for row in project_rows
        if scope.include_archived or row.status != ProjectStatus.ARCHIVED.value
    }
    archived_project_total = len(project_rows) - len(counted_project_ids)

    base = visible_tasks_queryset(actor, scope_project_ids)
    visible_q = _visible_q(counted_project_ids)
    archived_q = _archived_q(counted_project_ids)

    # ONE statement for the five statuses, the five difficulties, the total, the archived
    # delta and the overdue count: no second time-point can enter these numbers.
    aggregate = base.aggregate(
        **_summary_counts(
            include_archived=scope.include_archived,
            visible_q=visible_q,
            archived_q=archived_q,
            as_of=as_of,
        ),
        **_difficulty_counts(
            include_archived=scope.include_archived, visible_q=visible_q
        ),
    )

    status_counts = {status.value: aggregate[f"status_{status.value}"] for status in TaskStatus}
    difficulty = {str(level): aggregate[f"difficulty_{level}"] for level in DIFFICULTY_LEVELS}
    task_total = aggregate["task_total"]
    completion = _completion(status_counts[TaskStatus.DONE.value], task_total)

    project_counts = {status.value: 0 for status in ProjectStatus}
    for row in project_rows:
        if row.pk in counted_project_ids:
            project_counts[row.status] = project_counts.get(row.status, 0) + 1

    workload = _workload(
        base,
        scope_project_ids=scope_project_ids,
        as_of=as_of,
        include_archived=scope.include_archived,
        visible_q=visible_q,
    )
    exceptions = _exceptions(
        base,
        as_of=as_of,
        include_archived=scope.include_archived,
        visible_q=visible_q,
    )
    projects = _project_breakdown(
        base,
        project_rows=project_rows,
        counted_project_ids=counted_project_ids,
        include_archived=scope.include_archived,
        visible_q=visible_q,
        archived_q=archived_q,
        as_of=as_of,
    )

    payload: dict[str, Any] = {
        "as_of": iso(as_of),
        "scope": {
            "actor_type": actor.actor_type.value,
            "agent_id": uuid_str(agent.pk) if agent is not None else None,
            "role": agent.role if agent is not None else None,
            "project_id": uuid_str(scope.project_id),
            "project_ids": sorted(str(pk) for pk in scope_project_ids),
            "project_count": len(scope_project_ids),
            "include_archived": scope.include_archived,
            "task_visibility": _task_visibility(agent),
            "archived_project_total": archived_project_total,
        },
        "task_total": task_total,
        "task_status_counts": status_counts,
        "difficulty_counts": difficulty,
        # Tasks excluded by the default archive rule: the toggle adds exactly these.
        "archived_task_total": aggregate["archived_task_total"],
        "overdue_task_total": aggregate["overdue_task_total"],
        "completion": completion,
        "completion_rate": completion["rate"],
        "project_counts": project_counts,
        "project_total": sum(project_counts.values()),
        "agent_counts": workload["agent_counts"],
        "execution_counts": workload["execution_counts"],
        "exception_counts": exceptions["counts"],
        "exceptions": exceptions["items"],
        "projects": projects,
        "agents": workload["agents"],
    }
    return payload


def _completion(done: int, total: int) -> dict[str, Any]:
    """DONE / visible tasks; an empty scope reports "no tasks" instead of 100%."""
    if total <= 0:
        return {
            "done": 0,
            "total": 0,
            "rate": None,
            "rate_available": False,
            "reason": NO_TASKS_REASON,
        }
    return {
        "done": done,
        "total": total,
        "rate": round(done / total, 4),
        "rate_available": True,
        "reason": None,
    }


# --------------------------------------------------------------------------------------
# Workload (agents, executions, capacity)
# --------------------------------------------------------------------------------------


def _workload(
    base,
    *,
    scope_project_ids: set,
    as_of,
    include_archived: bool,
    visible_q: Q,
) -> dict[str, Any]:
    """Agent connectivity, effective executions and occupied slots, all inside the scope.

    Needs its own statements because it counts executions and agents, not tasks. It is still
    derived from the same base task queryset and the same ``as_of``:

    * an execution counts only when its task is in the scoped task queryset, so an agent's
      work in other projects can never enter the caller's totals;
    * an execution past its deadline that has not been reclaimed is NOT working but still
      occupies its slot, so ``effective`` and ``waiting_reclaim`` are reported separately
      alongside the agent's authoritative ``reserved_execution_count``.
    """
    member_agent_ids = set(
        ProjectMember.objects.filter(project_id__in=scope_project_ids)
        .exclude(agent__deleted_at__isnull=False)
        .values_list("agent_id", flat=True)
    )
    open_executions = TaskExecution.objects.filter(task__in=base, ended_at__isnull=True)
    # An agent that still holds an execution inside the scope stays visible even if it was
    # removed from the project afterwards; only its in-scope work is ever counted.
    # 软删除的 Agent 不进入看板的 Agent 行（其未结束执行仍计入执行与容量口径）。
    holder_agent_ids = set(
        open_executions.exclude(agent__deleted_at__isnull=False)
        .values_list("agent_id", flat=True)
        .distinct()
    )
    scoped_agent_ids = member_agent_ids | holder_agent_ids

    agents = list(
        Agent.objects.filter(pk__in=scoped_agent_ids).order_by("name", "id")
    )
    per_agent = {
        row["agent_id"]: row
        for row in open_executions.values("agent_id").annotate(
            open_count=Count("id"),
            effective_count=Count("id", filter=Q(execution_deadline_at__gt=as_of)),
            waiting_count=Count("id", filter=Q(execution_deadline_at__lte=as_of)),
        )
    }
    totals = open_executions.aggregate(
        open_count=Count("id"),
        effective_count=Count("id", filter=Q(execution_deadline_at__gt=as_of)),
        waiting_count=Count("id", filter=Q(execution_deadline_at__lte=as_of)),
    )

    agent_counts = {
        "total": len(agents),
        "enabled": 0,
        "disabled": 0,
        "online": 0,
        "offline": 0,
        "never_connected": 0,
        "working": 0,
    }
    agent_items: list[dict[str, Any]] = []
    for agent in agents:
        counters = per_agent.get(agent.pk, {})
        effective_count = counters.get("effective_count", 0)
        # Shared product 4.3 display state: disabled wins, then heartbeat connectivity.
        state = online_state_for(agent, now=as_of, effective_execution_count=effective_count)
        if agent.enabled:
            agent_counts["enabled"] += 1
        else:
            agent_counts["disabled"] += 1
        if state in ONLINE_ACTIVE_STATES:
            agent_counts["online"] += 1
        elif state == ONLINE_NEVER_CONNECTED:
            agent_counts["never_connected"] += 1
        elif state == ONLINE_OFFLINE:
            agent_counts["offline"] += 1
        if effective_count > 0:
            agent_counts["working"] += 1
        agent_items.append(
            {
                "agent_id": str(agent.pk),
                "name": agent.name,
                "role": agent.role,
                "enabled": agent.enabled,
                "online_state": state,
                "last_heartbeat_at": iso(agent.last_heartbeat_at),
                "effective_executions": effective_count,
                "waiting_reclaim_executions": counters.get("waiting_count", 0),
                "reserved_slots": agent.reserved_execution_count,
                "max_concurrency": agent.max_concurrency,
                "available_capacity": agent.available_capacity,
            }
        )

    return {
        "agent_counts": agent_counts,
        "execution_counts": {
            "open_executions": totals["open_count"],
            "effective_executions": totals["effective_count"],
            "waiting_reclaim_executions": totals["waiting_count"],
            # Authoritative occupied slots (Agent.reserved_execution_count), reported next to
            # the derived open count so drift - and "waiting for reclaim" - stays visible.
            "reserved_slots": sum(agent.reserved_execution_count for agent in agents),
            "capacity_total": sum(agent.max_concurrency for agent in agents),
            "capacity_available": sum(agent.available_capacity for agent in agents),
        },
        "agents": agent_items,
    }


# --------------------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------------------


def _member_pair_exists():
    return Exists(
        ProjectMember.objects.filter(
            project_id=OuterRef("project_id"), agent_id=OuterRef("designated_agent_id")
        )
    )


def _exceptions(base, *, as_of, include_archived: bool, visible_q: Q) -> dict[str, Any]:
    """Actionable tasks: timed-out executions awaiting reclaim and unusable designated agents.

    Derived from the same base task queryset as the counts (the task counts and this list
    must agree about which tasks exist); the counts themselves come from ``count()`` on the
    same two querysets that produce the items, so the list can never contradict them.
    """
    overdue = base.filter(
        status=TaskStatus.IN_PROGRESS.value,
        active_execution__ended_at__isnull=True,
        active_execution__execution_deadline_at__lte=as_of,
    )
    unavailable = base.filter(
        claim_mode=ClaimMode.ASSIGNED.value, designated_agent_id__isnull=False
    ).filter(Q(designated_agent__enabled=False) | ~_member_pair_exists())
    if not include_archived:
        overdue = overdue.filter(visible_q)
        unavailable = unavailable.filter(visible_q)

    overdue_total = overdue.count()
    unavailable_total = unavailable.count()

    entries: dict[uuid.UUID, dict[str, Any]] = {}
    overdue_rows = list(
        overdue.select_related("active_execution", "designated_agent").order_by(
            "active_execution__execution_deadline_at", "created_at", "id"
        )[:EXCEPTION_LIMIT]
    )
    for task in overdue_rows:
        execution = task.active_execution
        entry = _exception_entry(task, as_of=as_of)
        entry["reasons"].append(REASON_EXECUTION_OVERDUE)
        if execution is not None:
            entry["execution_deadline_at"] = iso(execution.execution_deadline_at)
            entry["overdue_seconds"] = int(
                (as_of - execution.execution_deadline_at).total_seconds()
            )
            entry["_deadline"] = execution.execution_deadline_at
        entries[task.pk] = entry

    unavailable_rows = list(
        unavailable.select_related("designated_agent").order_by("created_at", "id")[
            :EXCEPTION_LIMIT
        ]
    )
    member_pairs = _member_pairs(unavailable_rows)
    for task in unavailable_rows:
        entry = entries.get(task.pk)
        if entry is None:
            entry = _exception_entry(task, as_of=as_of)
            entries[task.pk] = entry
        if not task.designated_agent.enabled:
            entry["reasons"].append(REASON_DESIGNATED_AGENT_DISABLED)
        if (task.project_id, task.designated_agent_id) not in member_pairs:
            entry["reasons"].append(REASON_DESIGNATED_AGENT_NOT_MEMBER)

    ordered = sorted(entries.values(), key=_exception_sort_key)[:EXCEPTION_LIMIT]
    items = []
    for entry in ordered:
        item = {
            key: value for key, value in entry.items() if not key.startswith("_")
        }
        item["reason"] = item["reasons"][0] if item["reasons"] else None
        item["kind"] = _exception_kind(item["reasons"])
        items.append(item)

    return {
        "counts": {
            EXCEPTION_OVERDUE_RECLAIM: overdue_total,
            EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT: unavailable_total,
            "total": overdue_total + unavailable_total,
            "returned": len(items),
            "limit": EXCEPTION_LIMIT,
        },
        "items": items,
    }


def _exception_entry(task: Task, *, as_of) -> dict[str, Any]:
    designated = task.designated_agent
    return {
        "task_id": str(task.pk),
        "project_id": str(task.project_id),
        "number": task.number,
        "display_number": display_number(task.number),
        "title": task.title,
        "status": task.status,
        "difficulty": task.difficulty,
        "claim_mode": task.claim_mode,
        "designated_agent_id": uuid_str(task.designated_agent_id),
        "designated_agent_name": None if designated is None else designated.name,
        "assignee_agent_id": uuid_str(task.assignee_agent_id),
        "claimed_at": iso(task.claimed_at),
        "execution_deadline_at": None,
        "overdue_seconds": None,
        "reasons": [],
        # Internal ordering keys, stripped before the payload leaves this module.
        "_deadline": None,
        "_created": task.created_at,
    }


def _member_pairs(rows: list[Task]) -> set[tuple]:
    """Which of the listed designated agents are still members, in one query."""
    pairs = {(task.project_id, task.designated_agent_id) for task in rows}
    if not pairs:
        return set()
    condition = Q()
    for project_id, agent_id in pairs:
        condition |= Q(project_id=project_id, agent_id=agent_id)
    return set(
        ProjectMember.objects.filter(condition).values_list("project_id", "agent_id")
    )


def _exception_sort_key(entry: dict[str, Any]) -> tuple:
    # Timed-out executions first (they block a slot right now), then oldest deadline.
    deadline = entry["_deadline"] or datetime.max.replace(tzinfo=timezone.utc)
    overdue_first = 0 if REASON_EXECUTION_OVERDUE in entry["reasons"] else 1
    return (overdue_first, deadline, entry["_created"], entry["task_id"])


def _exception_kind(reasons: list[str]) -> str | None:
    overdue = REASON_EXECUTION_OVERDUE in reasons
    designated = REASON_DESIGNATED_AGENT_DISABLED in reasons or (
        REASON_DESIGNATED_AGENT_NOT_MEMBER in reasons
    )
    if overdue and designated:
        return EXCEPTION_MIXED
    if overdue:
        return EXCEPTION_OVERDUE_RECLAIM
    if designated:
        return EXCEPTION_UNAVAILABLE_DESIGNATED_AGENT
    return None


# --------------------------------------------------------------------------------------
# Per-project breakdown
# --------------------------------------------------------------------------------------


def _project_breakdown(
    base,
    *,
    project_rows: list[Project],
    counted_project_ids: set,
    include_archived: bool,
    visible_q: Q,
    archived_q: Q,
    as_of,
) -> list[dict[str, Any]]:
    """Five-state counts, completion rate and pending-review per project.

    One grouped statement derived from the same base queryset (a single statement cannot
    return both the global totals and a per-project group-by); projects are listed from the
    scope rows, so a project with no task still reports explicit zeros instead of vanishing.
    """
    rows = base.values("project_id").annotate(
        **_summary_counts(
            include_archived=include_archived,
            visible_q=visible_q,
            archived_q=archived_q,
            as_of=as_of,
        )
    )
    per_project = {row["project_id"]: row for row in rows}

    items: list[dict[str, Any]] = []
    for project in project_rows:
        if project.pk not in counted_project_ids:
            continue
        row = per_project.get(project.pk)
        status_counts = {
            status.value: 0 if row is None else row[f"status_{status.value}"]
            for status in TaskStatus
        }
        total = 0 if row is None else row["task_total"]
        completion = _completion(status_counts[TaskStatus.DONE.value], total)
        items.append(
            {
                "project_id": str(project.pk),
                "title": project.title,
                "status": project.status,
                "task_total": total,
                "task_status_counts": status_counts,
                "archived_task_total": 0 if row is None else row["archived_task_total"],
                "overdue_task_total": 0 if row is None else row["overdue_task_total"],
                "pending_review": status_counts[TaskStatus.IN_REVIEW.value],
                "completion": completion,
                "completion_rate": completion["rate"],
            }
        )
    return items


# --------------------------------------------------------------------------------------
# Activity feed
# --------------------------------------------------------------------------------------


def _list_activity(
    actor: ActorContext, filters: ActivityFilters, *, page_number: int, page_size: int
) -> dict:
    agent, member_project_ids = reader_scope(actor)
    scope_project_ids = _scoped_project_ids(agent, member_project_ids, filters.project_id)

    if actor.actor_type is ActorType.WEB_OPERATOR and filters.project_id is None:
        # The trusted local administrator also sees agent-level events without a project;
        # an explicit project filter narrows to that project's events only.
        queryset = ActivityEvent.objects.filter(
            Q(project_id__in=scope_project_ids) | Q(project_id__isnull=True)
        )
    else:
        queryset = ActivityEvent.objects.filter(project_id__in=scope_project_ids)
    if filters.task_id is not None:
        queryset = queryset.filter(task_id=filters.task_id)
    if filters.action:
        queryset = queryset.filter(action=filters.action.strip())
    if not filters.include_archived:
        # Archive scope matches the dashboard default; project-less events stay visible.
        queryset = queryset.exclude(project__status=ProjectStatus.ARCHIVED.value)

    queryset = queryset.order_by("-created_at", "-id")
    total = queryset.count()
    offset = (page_number - 1) * page_size
    items = [activity_view(event) for event in queryset[offset : offset + page_size]]
    return page(items, total, page_number, page_size)
