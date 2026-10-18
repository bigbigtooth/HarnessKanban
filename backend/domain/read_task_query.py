"""Read-only task queries: list, five-level selection, detail, activity and claim eligibility.

Everything in this module is strictly read-only (DEVELOP D11): a list or detail request must
never claim, reclaim, expire or touch a capacity slot, and it must not take the singleton
write lock. Both APIs share these functions so the WebAPI board and ``read_task`` cannot
disagree about visibility, ordering, pagination or the ``claimable_for_me`` hint.

``claim_eligibility`` is the single reusable eligibility rule. The AgentAPI list uses it to
render a hint; the claim service must re-use it (inside the global lock) before it really
reserves a slot, so list hints and the atomic claim agree on the same conditions.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.projects.models import Project
from apps.tasks.models import Task
from domain.contracts import (
    DIFFICULTY_TIMEOUT_SECONDS,
    ActorType,
    ClaimMode,
    ProjectStatus,
    TaskStatus,
    selected_level_labels,
)
from domain.errors import InvalidArgumentError, ResourceNotFoundError
from domain.permissions import (
    accessible_project_ids,
    load_actor_agent_locked,
    require_task_readable,
    visible_tasks_queryset,
)
from domain.presenters import (
    activity_view,
    execution_view,
    page,
    review_view,
    submission_view,
    task_detail,
    task_summary,
    uuid_str,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 200

# Machine-readable refusal codes for `claim_eligibility`. They mirror the CONTRACT error
# codes where one exists so a caller can turn the reason into a DomainError directly.
REASON_PROJECT_NOT_ACTIVE = "PROJECT_NOT_ACTIVE"
REASON_TASK_ARCHIVED = "TASK_ARCHIVED"
REASON_TASK_NOT_TODO = "TASK_NOT_TODO"
REASON_NOT_DESIGNATED = "NOT_DESIGNATED"
REASON_AGENT_DISABLED = "AGENT_DISABLED"
REASON_NOT_A_MEMBER = "NOT_A_MEMBER"
REASON_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"

#: ``status=ALL`` 表示不按状态过滤（管理面要看全部五态）。Agent 侧的默认仍是待办。
ALL_STATUSES = "ALL"

REASON_MESSAGES: dict[str, str] = {
    REASON_PROJECT_NOT_ACTIVE: "项目当前生命周期不可领取",
    REASON_TASK_ARCHIVED: "任务已归档",
    REASON_TASK_NOT_TODO: "任务当前不是待办状态",
    REASON_NOT_DESIGNATED: "任务指定了其他 Agent",
    REASON_AGENT_DISABLED: "当前 Agent 已被禁用",
    REASON_NOT_A_MEMBER: "当前 Agent 不是该项目成员",
    REASON_CAPACITY_EXCEEDED: "当前 Agent 可用容量已满",
}


@dataclass(frozen=True)
class TaskListFilters:
    """Normalised list filters.

    ``levels`` is the Agent side five-level selection: ``None`` means "no level parameter"
    (the WebAPI ``difficulty`` integer filter is a different parameter), an empty tuple means
    an all-zero selection that must return an empty list instead of falling back to L1-L5.
    """

    project_id: uuid.UUID | None = None
    status: str | None = None
    difficulty: int | None = None
    claim_mode: str | None = None
    designated_agent_id: uuid.UUID | None = None
    assignee_agent_id: uuid.UUID | None = None
    q: str | None = None
    include_archived: bool = False
    levels: tuple[int, ...] | None = None
    available: bool = False

    def validate(self) -> None:
        if self.status is not None and self.status not in {
            *(status.value for status in TaskStatus),
            ALL_STATUSES,
        }:
            raise InvalidArgumentError(
                "status 取值不合法", details={"parameter": "status", "value": self.status}
            )
        if self.difficulty is not None and self.difficulty not in DIFFICULTY_TIMEOUT_SECONDS:
            raise InvalidArgumentError(
                "difficulty 必须是 1 到 5 的整数",
                details={"parameter": "difficulty", "value": self.difficulty},
            )
        if self.claim_mode is not None and self.claim_mode not in {mode.value for mode in ClaimMode}:
            raise InvalidArgumentError(
                "claim_mode 取值不合法", details={"parameter": "claim_mode", "value": self.claim_mode}
            )
        if self.levels is not None:
            for level in self.levels:
                if level not in DIFFICULTY_TIMEOUT_SECONDS:
                    raise InvalidArgumentError(
                        "等级参数超出 1 到 5", details={"parameter": f"l{level}"}
                    )
        if self.q is not None and len(self.q) > MAX_QUERY_LENGTH:
            raise InvalidArgumentError(
                f"q 超过长度上限 {MAX_QUERY_LENGTH}",
                details={"parameter": "q", "max_length": MAX_QUERY_LENGTH},
            )
        if self.available:
            if self.status is not None and self.status != TaskStatus.TODO.value:
                raise InvalidArgumentError(
                    "available=true 只适用于待办任务，不能与其它 status 同时使用",
                    details={"parameter": "status", "value": self.status},
                )
            if self.include_archived:
                raise InvalidArgumentError(
                    "available=true 不接受 include_archived", details={"parameter": "include_archived"}
                )
            if self.levels is None:
                raise InvalidArgumentError(
                    "available 只用于 Agent 的等级筛选列表", details={"parameter": "available"}
                )


# --------------------------------------------------------------------------------------
# Claim eligibility (the single reusable rule)
# --------------------------------------------------------------------------------------


def claim_eligibility(
    task: Task,
    agent: Agent,
    *,
    member_project_ids: Collection[uuid.UUID],
    available_capacity: int | None,
    project_status: str | None = None,
) -> dict:
    """Decide whether ``agent`` may claim ``task`` right now.

    Returns ``{"eligible": bool, "reason": str | None, "message": str}`` where ``reason`` is
    one of the ``REASON_*`` codes and is ``None`` when the agent is eligible.

    ``member_project_ids`` and ``available_capacity`` are passed in so a list request can
    evaluate many tasks from one membership/capacity snapshot. ``project_status`` is optional
    only so a list request can supply a prefetched map; when it is omitted the current value is
    read from the database.

    Condition order (first failure wins): project ACTIVE, task not archived, task TODO,
    designation (OPEN or designated to this agent), agent enabled, membership, capacity.
    """
    if project_status is None:
        project_status = project_status_of(task.project_id)

    reason: str | None = None
    if project_status != ProjectStatus.ACTIVE.value:
        reason = REASON_PROJECT_NOT_ACTIVE
    elif task.is_archived:
        reason = REASON_TASK_ARCHIVED
    elif task.status != TaskStatus.TODO.value:
        reason = REASON_TASK_NOT_TODO
    elif task.claim_mode == ClaimMode.ASSIGNED.value and not _is_designated(task, agent):
        reason = REASON_NOT_DESIGNATED
    elif not agent.enabled:
        reason = REASON_AGENT_DISABLED
    elif task.project_id not in member_project_ids:
        reason = REASON_NOT_A_MEMBER
    elif available_capacity is not None and available_capacity <= 0:
        reason = REASON_CAPACITY_EXCEEDED

    return {
        "eligible": reason is None,
        "reason": reason,
        "message": "" if reason is None else REASON_MESSAGES[reason],
    }


def _is_designated(task: Task, agent: Agent) -> bool:
    return task.designated_agent_id is not None and task.designated_agent_id == agent.pk


def project_status_of(project_id) -> str | None:
    return Project.objects.filter(pk=project_id).values_list("status", flat=True).first()


# --------------------------------------------------------------------------------------
# Read scope
# --------------------------------------------------------------------------------------


def reader_scope(actor) -> tuple[Agent | None, set]:
    """Return ``(agent, member_project_ids)`` for a read request.

    An Agent request re-reads its own row under the transaction (a disabled agent or a
    revoked key cannot keep reading); a Web operator request reads every project.
    """
    if actor.actor_type is ActorType.AGENT:
        agent = load_actor_agent_locked(actor)
        return agent, accessible_project_ids(agent.pk)
    # 已软删除的项目连同它的任务一起从可读范围消失。
    return None, set(
        Project.objects.filter(deleted_at__isnull=True).values_list("pk", flat=True)
    )


def visible_task_or_404(actor, task_id) -> Task:
    """Load one task and apply the shared visibility rule (404 when not readable)."""
    task = Task.objects.filter(pk=task_id).first()
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    require_task_readable(actor, task)
    return task


# --------------------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------------------


def list_tasks(
    actor,
    filters: TaskListFilters,
    *,
    page_number: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Paged, stably ordered task list.

    Response: ``items``/``total``/``page``/``page_size`` plus, for Agent requests,
    ``selected_levels`` and ``available_capacity``. Never mutates business state.
    """
    if page_number < 1:
        raise InvalidArgumentError("page 必须大于等于 1", details={"page": page_number})
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise InvalidArgumentError(
            f"page_size 必须在 1 到 {MAX_PAGE_SIZE} 之间", details={"page_size": page_size}
        )
    filters.validate()
    # A read-only transaction: it keeps ``select_for_update`` in the visibility helper legal
    # on MySQL and commits nothing. No write lock is taken, so reads never block a claim.
    with transaction.atomic():
        return _list_tasks(actor, filters, page_number=page_number, page_size=page_size)


def _list_tasks(actor, filters: TaskListFilters, *, page_number: int, page_size: int) -> dict:
    agent, member_project_ids = reader_scope(actor)
    available_capacity = None if agent is None else agent.available_capacity
    queryset = visible_tasks_queryset(actor, member_project_ids)

    if filters.project_id is not None:
        # An unauthorised project filter can only ever narrow to nothing; it is never an
        # existence oracle because the base scope already excludes foreign projects.
        queryset = queryset.filter(project_id=filters.project_id)
    if filters.levels is not None:
        if not filters.levels:
            # All-zero selection: a valid, empty list response, never a silent "all levels".
            return _list_response(
                [], 0, page_number, page_size, filters, agent, available_capacity
            )
        queryset = queryset.filter(difficulty__in=list(filters.levels))
    if filters.difficulty is not None:
        queryset = queryset.filter(difficulty=filters.difficulty)
    if filters.claim_mode is not None:
        queryset = queryset.filter(claim_mode=filters.claim_mode)
    if filters.designated_agent_id is not None:
        queryset = queryset.filter(designated_agent_id=filters.designated_agent_id)
    if filters.assignee_agent_id is not None:
        queryset = queryset.filter(assignee_agent_id=filters.assignee_agent_id)
    if filters.q:
        queryset = queryset.filter(
            Q(title__icontains=filters.q) | Q(description__icontains=filters.q)
        )

    include_archived = filters.include_archived
    status_value = filters.status or TaskStatus.TODO.value
    if filters.available:
        # available=true only narrows the *task's* own claim conditions. Capacity is never
        # part of it: a task stays readable when the caller has no free slot.
        status_value = TaskStatus.TODO.value
        include_archived = False
        queryset = queryset.filter(
            project_id__in=Project.objects.filter(status=ProjectStatus.ACTIVE.value).values_list(
                "pk", flat=True
            )
        )
        if agent is not None:
            queryset = queryset.filter(
                Q(claim_mode=ClaimMode.OPEN.value) | Q(designated_agent_id=agent.pk)
            )

    if status_value != ALL_STATUSES:
        queryset = queryset.filter(status=status_value)
    if not include_archived:
        queryset = queryset.filter(is_archived=False)

    total = queryset.count()
    offset = (page_number - 1) * page_size
    rows = list(
        queryset.order_by("sort_order", "created_at", "id")[offset : offset + page_size]
    )
    project_statuses = _project_status_map({row.project_id for row in rows})

    items = [
        task_list_item(
            task,
            agent=agent,
            member_project_ids=member_project_ids,
            project_status=project_statuses.get(task.project_id),
            available_capacity=available_capacity,
        )
        for task in rows
    ]
    return _list_response(items, total, page_number, page_size, filters, agent, available_capacity)


def _list_response(
    items: list[dict],
    total: int,
    page_number: int,
    page_size: int,
    filters: TaskListFilters,
    agent: Agent | None,
    available_capacity: int | None,
) -> dict:
    data = page(items, total, page_number, page_size)
    if filters.levels is not None:
        data["selected_levels"] = selected_level_labels(list(filters.levels))
    if agent is not None:
        data["available_capacity"] = available_capacity
    return data


def _project_status_map(project_ids: set) -> dict:
    if not project_ids:
        return {}
    return dict(
        Project.objects.filter(pk__in=project_ids).values_list("pk", "status")
    )


def task_list_item(
    task: Task,
    *,
    agent: Agent | None,
    member_project_ids: set | None = None,
    project_status: str | None = None,
    available_capacity: int | None = None,
) -> dict:
    """``task_summary`` plus the list extras both APIs render."""
    item = task_summary(task)
    item["description"] = task.description
    item["acceptance_criteria"] = task.acceptance_criteria
    item["execution_timeout_seconds"] = DIFFICULTY_TIMEOUT_SECONDS.get(task.difficulty)
    if agent is not None:
        eligibility = claim_eligibility(
            task,
            agent,
            member_project_ids=member_project_ids if member_project_ids is not None else set(),
            available_capacity=available_capacity,
            project_status=project_status,
        )
        item["claimable_for_me"] = eligibility["eligible"]
    return item


# --------------------------------------------------------------------------------------
# Detail and activity
# --------------------------------------------------------------------------------------


def get_task_detail(actor, task_id) -> dict:
    """Full task detail: requirements, current execution/submission and history summaries."""
    with transaction.atomic():
        task = visible_task_or_404(actor, task_id)
        return task_detail_payload(task)


def task_detail_payload(task: Task) -> dict:
    """Detail payload shared by GET /tasks/{id} and every write action response."""
    data = task_detail(task)
    data["created_by_agent_id"] = uuid_str(task.created_by_agent_id)
    data["current_execution"] = (
        execution_view(task.active_execution) if task.active_execution_id else None
    )
    data["current_submission"] = (
        submission_view(task.current_submission) if task.current_submission_id else None
    )

    reviews = {review.submission_id: review for review in task.reviews.all()}
    data["execution_history"] = [
        {**execution_view(execution), "has_submission": hasattr(execution, "submission")}
        for execution in task.executions.order_by("sequence", "id")
    ]
    data["submission_history"] = [
        {
            **submission_view(submission),
            "review": _review_of(submission, reviews),
        }
        for submission in task.submissions.order_by("submitted_at", "id")
    ]
    data["review_history"] = [review_view(review) for review in task.reviews.order_by("created_at", "id")]
    return data


def _review_of(submission, reviews: dict):
    review = reviews.get(submission.pk)
    if review is None and hasattr(submission, "review"):
        review = submission.review
    return None if review is None else review_view(review)


def list_task_activity(actor, task_id, *, page_number: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """Append-only activity history for one readable task, newest last."""
    if page_number < 1:
        raise InvalidArgumentError("page 必须大于等于 1", details={"page": page_number})
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise InvalidArgumentError(
            f"page_size 必须在 1 到 {MAX_PAGE_SIZE} 之间", details={"page_size": page_size}
        )
    with transaction.atomic():
        task = visible_task_or_404(actor, task_id)
        queryset = ActivityEvent.objects.filter(task_id=task.pk).order_by("created_at", "id")
        total = queryset.count()
        offset = (page_number - 1) * page_size
        items = [activity_view(event) for event in queryset[offset : offset + page_size]]
        return page(items, total, page_number, page_size)
