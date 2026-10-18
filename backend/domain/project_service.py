"""Project lifecycle services (T10) plus the authorised project read helpers.

Lifecycle is managed independently from the five task statuses: no function in this module
ever writes a task row. Draft projects can be authored but not published or claimed;
``activate`` requires at least one enabled supervisor member; ``complete`` requires at
least one non-archived task and every non-archived task DONE; ``archive`` freezes the
project read-only and remembers the previous lifecycle; ``restore`` returns to it.

Every write takes an ``Idempotency-Key``, runs through
:func:`domain.service_runner.run_request_service` (outermost transaction, bounded retry)
and records exactly one ``ActivityEvent`` and one ``OutboxEvent`` per real action. A
failure path leaves the project version/status/archived_from_status and every member row
untouched because the whole transaction rolls back.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import F

from apps.projects.models import Project, ProjectContextVersion, ProjectMember
from apps.tasks.models import Task
from domain.clock import db_now
from domain.contracts import ActorContext, ActorType, AgentRole, ProjectStatus, TaskStatus
from domain.errors import (
    InvalidArgumentError,
    InvalidTransitionError,
    LastSupervisorRequiredError,
    ResourceNotFoundError,
    RoleForbiddenError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan
from domain.permissions import (
    accessible_project_ids,
    load_actor_agent_for_read,
    load_actor_agent_locked,
)
from domain.bulk import bulk_result, parse_bulk_ids, run_bulk
from domain.presenters import project_detail, project_summary
from domain.service_runner import run_request_service

logger = logging.getLogger("domain.project_service")

OPERATION_ACTIVATE = "POST /api/web/v1/projects/{id}/activate"
OPERATION_COMPLETE = "POST /api/web/v1/projects/{id}/complete"
OPERATION_ARCHIVE = "POST /api/web/v1/projects/{id}/archive"
OPERATION_RESTORE = "POST /api/web/v1/projects/{id}/restore"
OPERATION_DELETE = "POST /api/web/v1/projects/{id}/delete"
OPERATION_BULK_DELETE = "POST /api/web/v1/projects/bulk-delete"
OPERATION_BULK_RESTORE = "POST /api/web/v1/projects/bulk-restore"

MAX_REASON_LENGTH = 2000

# Lifecycle values that can be restored to. ``ARCHIVED`` is never a restore target, so a
# corrupted ``archived_from_status`` falls back to DRAFT instead of a stuck read-only row.
RESTORABLE_STATUSES = (
    ProjectStatus.DRAFT.value,
    ProjectStatus.ACTIVE.value,
    ProjectStatus.COMPLETED.value,
)

# Statuses that block archiving (product 5.1, A21).
ARCHIVE_BLOCKING_TASK_STATUSES = (TaskStatus.IN_PROGRESS.value, TaskStatus.IN_REVIEW.value)


# --------------------------------------------------------------------------------------
# Authorised project reads (both APIs)
# --------------------------------------------------------------------------------------


def list_projects(
    actor: ActorContext,
    *,
    status_filter: str | None = None,
    include_archived: bool = True,
    deleted: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Projects the caller may read, paginated. Never fetch-then-hide across projects."""
    if status_filter is not None and status_filter not in {s.value for s in ProjectStatus}:
        raise InvalidArgumentError(
            "status 取值不合法", details={"status": status_filter, "allowed": [s.value for s in ProjectStatus]}
        )

    queryset = Project.objects.all().order_by("created_at", "id")
    if actor.actor_type is not ActorType.WEB_OPERATOR:
        agent = load_actor_agent_for_read(actor)
        queryset = queryset.filter(pk__in=accessible_project_ids(agent.pk))
    # 软删除的项目默认不可见；`deleted=true` 只看已删除的（回收站视图）。
    queryset = queryset.filter(deleted_at__isnull=not deleted)
    if status_filter is not None:
        queryset = queryset.filter(status=status_filter)
    if not include_archived and not deleted:
        queryset = queryset.exclude(status=ProjectStatus.ARCHIVED.value)

    total = queryset.count()
    offset = (page - 1) * page_size
    rows = list(queryset[offset : offset + page_size])
    return {
        "items": [project_summary(project) for project in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_project(actor: ActorContext, project_id) -> dict[str, Any]:
    """Project detail with the current context version pointer."""
    project = _load_project(project_id)
    if actor.actor_type is not ActorType.WEB_OPERATOR:
        agent = load_actor_agent_for_read(actor)
        if project.pk not in accessible_project_ids(agent.pk):
            raise ResourceNotFoundError("项目不存在或当前身份无权访问")

    from domain.context_service import latest_context_version

    data = project_detail(project)
    context = latest_context_version(project)
    data["current_context_version_id"] = None if context is None else str(context.pk)
    data["current_context_version_number"] = None if context is None else context.number
    return data


# --------------------------------------------------------------------------------------
# Lifecycle actions
# --------------------------------------------------------------------------------------


def activate_project(
    actor: ActorContext, project_id, *, expected_version: int, idem_key: str
) -> dict[str, Any]:
    """DRAFT/COMPLETED -> ACTIVE, requiring at least one enabled supervisor member."""

    def apply(project: Project) -> dict[str, Any]:
        if project.status not in (ProjectStatus.DRAFT.value, ProjectStatus.COMPLETED.value):
            raise InvalidTransitionError(
                "只有草稿或已结项项目可以启用",
                details={"project_status": project.status},
            )
        if not _has_enabled_supervisor(project):
            raise LastSupervisorRequiredError(
                "启用项目前必须至少指定一名已启用主管",
                details={"project_id": str(project.pk)},
            )
        return _transition(
            actor,
            project,
            target_status=ProjectStatus.ACTIVE.value,
            archived_from_status=None,
            action="project.activate",
            event_type="project.activated",
            reason="",
        )

    return _run_lifecycle(
        actor,
        project_id,
        operation=OPERATION_ACTIVATE,
        idem_key=idem_key,
        expected_version=expected_version,
        body={"expected_version": expected_version},
        apply=apply,
    )


def complete_project(
    actor: ActorContext, project_id, *, expected_version: int, idem_key: str
) -> dict[str, Any]:
    """ACTIVE -> COMPLETED, requiring every non-archived task to be DONE."""

    def apply(project: Project) -> dict[str, Any]:
        if project.status != ProjectStatus.ACTIVE.value:
            raise InvalidTransitionError(
                "只有进行中项目可以结项", details={"project_status": project.status}
            )
        tasks = _lock_non_archived_tasks(project)
        if not tasks:
            raise InvalidTransitionError(
                "项目必须至少有一个未归档任务才能结项",
                details={"project_id": str(project.pk), "non_archived_tasks": 0},
            )
        blocking = [task for task in tasks if task.status != TaskStatus.DONE.value]
        if blocking:
            counts: dict[str, int] = {}
            for task in blocking:
                counts[task.status] = counts.get(task.status, 0) + 1
            raise InvalidTransitionError(
                "存在未完成的任务，不能结项",
                details={"blocking_tasks": len(blocking), "statuses": counts},
            )
        return _transition(
            actor,
            project,
            target_status=ProjectStatus.COMPLETED.value,
            archived_from_status=None,
            action="project.complete",
            event_type="project.completed",
            reason="",
        )

    return _run_lifecycle(
        actor,
        project_id,
        operation=OPERATION_COMPLETE,
        idem_key=idem_key,
        expected_version=expected_version,
        body={"expected_version": expected_version},
        apply=apply,
    )


def archive_project(
    actor: ActorContext,
    project_id,
    *,
    reason: str,
    expected_version: int,
    idem_key: str,
) -> dict[str, Any]:
    """Any status -> ARCHIVED, saving ``archived_from_status``; reason is mandatory."""
    reason = _require_reason(reason)

    def apply(project: Project) -> dict[str, Any]:
        if project.status == ProjectStatus.ARCHIVED.value:
            raise InvalidTransitionError(
                "项目已归档，不能重复归档", details={"project_status": project.status}
            )
        tasks = _lock_non_archived_tasks(project)
        blocking = [task for task in tasks if task.status in ARCHIVE_BLOCKING_TASK_STATUSES]
        if blocking:
            raise InvalidTransitionError(
                "存在进行中或待审核任务，不能归档；请先回收执行或完成审核",
                details={
                    "blocking_tasks": len(blocking),
                    "task_ids": [str(task.pk) for task in blocking],
                },
            )
        return _transition(
            actor,
            project,
            target_status=ProjectStatus.ARCHIVED.value,
            archived_from_status=project.status,
            action="project.archive",
            event_type="project.archived",
            reason=reason,
        )

    return _run_lifecycle(
        actor,
        project_id,
        operation=OPERATION_ARCHIVE,
        idem_key=idem_key,
        expected_version=expected_version,
        body={"expected_version": expected_version, "reason": reason},
        apply=apply,
    )


def restore_project(
    actor: ActorContext, project_id, *, expected_version: int, idem_key: str
) -> dict[str, Any]:
    """ARCHIVED -> the saved previous lifecycle (DRAFT when absent).

    对一个被软删除的项目执行恢复，同时清空删除标记：列表与统计重新可见。
    """

    def apply(project: Project) -> dict[str, Any]:
        if project.status != ProjectStatus.ARCHIVED.value:
            raise InvalidTransitionError(
                "只有已归档项目可以恢复", details={"project_status": project.status}
            )
        was_deleted = project.deleted_at is not None
        target = project.archived_from_status
        if target not in RESTORABLE_STATUSES:
            target = ProjectStatus.DRAFT.value
        return _transition(
            actor,
            project,
            target_status=target,
            archived_from_status=None,
            action="project.restore",
            event_type="project.restored",
            reason="",
            restores_deleted=was_deleted,
        )

    return _run_lifecycle(
        actor,
        project_id,
        operation=OPERATION_RESTORE,
        idem_key=idem_key,
        expected_version=expected_version,
        body={"expected_version": expected_version},
        apply=apply,
    )


def delete_project(
    actor: ActorContext,
    project_id,
    *,
    reason: str,
    expected_version: int,
    idem_key: str,
) -> dict[str, Any]:
    """软删除项目：冻结为只读（ARCHIVED）并打上删除标记，历史一行不删。

    前置校验与归档一致：存在进行中或待审核任务时拒绝，避免删掉仍有有效执行的项目。
    """

    def apply(project: Project) -> dict[str, Any]:
        if project.deleted_at is not None:
            raise InvalidTransitionError(
                "项目已删除，不能重复删除", details={"project_id": str(project.pk)}
            )
        tasks = _lock_non_archived_tasks(project)
        blocking = [task for task in tasks if task.status in ARCHIVE_BLOCKING_TASK_STATUSES]
        if blocking:
            raise InvalidTransitionError(
                "存在进行中或待审核任务，不能删除；请先回收执行或完成审核",
                details={
                    "blocking_tasks": len(blocking),
                    "task_ids": [str(task.pk) for task in blocking],
                },
            )
        return _transition(
            actor,
            project,
            target_status=ProjectStatus.ARCHIVED.value,
            archived_from_status=(
                project.archived_from_status
                if project.status == ProjectStatus.ARCHIVED.value
                else project.status
            ),
            action="project.delete",
            event_type="project.deleted",
            reason=_require_reason(reason),
            deletes=True,
        )

    return _run_lifecycle(
        actor,
        project_id,
        operation=OPERATION_DELETE,
        idem_key=idem_key,
        expected_version=expected_version,
        body={"expected_version": expected_version, "reason": reason},
        apply=apply,
    )


def bulk_delete_projects(
    actor: ActorContext, *, payload: dict[str, Any], idem_key: str
) -> dict[str, Any]:
    """批量软删除：逐项独立事务，一项失败不影响其他项，返回逐项结果。"""
    _require_web_administrator(actor)
    ids = parse_bulk_ids(payload, "project_ids")
    reason = _require_reason(payload.get("reason"))

    def callback(project_id, _index, item_key):
        project = Project.objects.filter(pk=project_id).values("version").first()
        if project is None:
            raise ResourceNotFoundError("项目不存在")
        return delete_project(
            actor,
            project_id,
            reason=reason,
            expected_version=project["version"],
            idem_key=item_key,
        )

    outcome = run_bulk(ids, callback, key_prefix=idem_key)
    return bulk_result(actor, "project.bulk_delete", outcome)


def bulk_restore_projects(
    actor: ActorContext, *, payload: dict[str, Any], idem_key: str
) -> dict[str, Any]:
    """批量恢复：逐项独立事务，返回逐项结果。"""
    _require_web_administrator(actor)
    ids = parse_bulk_ids(payload, "project_ids")

    def callback(project_id, _index, item_key):
        project = Project.objects.filter(pk=project_id).values("version").first()
        if project is None:
            raise ResourceNotFoundError("项目不存在")
        return restore_project(
            actor, project_id, expected_version=project["version"], idem_key=item_key
        )

    outcome = run_bulk(ids, callback, key_prefix=idem_key)
    return bulk_result(actor, "project.bulk_restore", outcome)


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _run_lifecycle(
    actor: ActorContext,
    project_id,
    *,
    operation: str,
    idem_key: str,
    expected_version: int,
    body: dict[str, Any],
    apply,
) -> dict[str, Any]:
    expected_version = _require_expected_version(expected_version)

    def service() -> dict[str, Any]:
        project = Project.objects.select_for_update().filter(pk=project_id).first()
        if project is None:
            raise ResourceNotFoundError("项目不存在")
        _require_web_administrator(actor)

        request_hash = canonical_request_hash(
            operation=operation,
            body=body,
            resource_id=str(project.pk),
            project_id=str(project.pk),
        )
        # The receipt is reserved after the project lock and before any business change,
        # and completes in the same transaction (CONTRACT.md §8).
        receipt = lookup_or_reserve(
            actor_scope=actor.actor_scope(),
            operation=operation,
            key=idem_key,
            request_hash=request_hash,
            resource_type="project",
        )
        if receipt.replayed:
            return {**(receipt.response_data or {}), "idempotent_replay": True}

        if project.version != expected_version:
            raise VersionConflictError(
                details={"expected_version": expected_version, "current_version": project.version}
            )

        data = apply(project)
        complete_receipt(
            receipt, response_data=data, resource_type="project", resource_id=project.pk
        )
        return data

    return run_request_service(service)


def _transition(
    actor: ActorContext,
    project: Project,
    *,
    target_status: str,
    archived_from_status: str | None,
    action: str,
    event_type: str,
    reason: str,
    deletes: bool = False,
    restores_deleted: bool = False,
) -> dict[str, Any]:
    """Apply one lifecycle transition with a conditional UPDATE and exactly one event pair."""
    previous_status = project.status
    previous_version = project.version
    now = db_now()

    updates: dict[str, Any] = {
        "status": target_status,
        "archived_from_status": archived_from_status,
        "version": F("version") + 1,
        "updated_at": now,
    }
    if deletes:
        updates["deleted_at"] = now
        updates["deleted_reason"] = reason
    elif restores_deleted:
        updates["deleted_at"] = None
        updates["deleted_reason"] = ""

    updated = Project.objects.filter(pk=project.pk, version=previous_version).update(**updates)
    if updated != 1:
        raise VersionConflictError(
            "项目版本已变化，生命周期动作已取消",
            details={"project_id": str(project.pk), "expected_version": previous_version},
        )

    project.status = target_status
    project.archived_from_status = archived_from_status
    project.version = previous_version + 1
    project.updated_at = now
    if deletes:
        project.deleted_at = now
        project.deleted_reason = reason
    elif restores_deleted:
        project.deleted_at = None
        project.deleted_reason = ""

    record_activity(
        actor,
        action,
        project_id=project.pk,
        before={"status": previous_status, "version": previous_version},
        after={
            "status": project.status,
            "version": project.version,
            "archived_from_status": project.archived_from_status,
            "deleted": project.deleted_at is not None,
        },
        reason=reason,
    )
    enqueue_outbox(
        event_type,
        project.pk,
        {
            "project_id": str(project.pk),
            "status": project.status,
            "previous_status": previous_status,
            "version": project.version,
            "archived_from_status": project.archived_from_status,
            "deleted": project.deleted_at is not None,
        },
    )
    logger.info(
        "项目生命周期变更：project=%s %s -> %s version=%s deleted=%s",
        project.pk,
        previous_status,
        project.status,
        project.version,
        project.deleted_at is not None,
    )
    return project_detail(project)


def _lock_non_archived_tasks(project: Project) -> list[Task]:
    """Lock the project's non-archived task range before judging completion/archiving."""
    return list(
        Task.objects.select_for_update()
        .filter(project=project, is_archived=False)
        .order_by("id")
    )


def _has_enabled_supervisor(project: Project) -> bool:
    return ProjectMember.objects.filter(
        project=project,
        agent__role=AgentRole.SUPERVISOR.value,
        agent__enabled=True,
    ).exists()


def _require_web_administrator(actor: ActorContext) -> None:
    """Project lifecycle is local-administrator territory (product 2.3)."""
    if actor.actor_type is not ActorType.WEB_OPERATOR:
        raise RoleForbiddenError("项目生命周期只能由本地管理员操作")


def _require_expected_version(expected_version) -> int:
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise InvalidArgumentError(
            "缺少或非法的 expected_version", details={"field": "expected_version"}
        )
    if expected_version < 1:
        raise InvalidArgumentError(
            "expected_version 必须大于等于 1", details={"field": "expected_version"}
        )
    return expected_version


def _require_reason(reason) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidArgumentError("必须填写原因 reason", details={"field": "reason"})
    reason = reason.strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise InvalidArgumentError(
            f"reason 超过长度上限 {MAX_REASON_LENGTH}",
            details={"field": "reason", "max_length": MAX_REASON_LENGTH},
        )
    return reason


def _load_project(project_id) -> Project:
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    return project


__all__ = [
    "ARCHIVE_BLOCKING_TASK_STATUSES",
    "MAX_REASON_LENGTH",
    "OPERATION_ACTIVATE",
    "OPERATION_ARCHIVE",
    "OPERATION_COMPLETE",
    "OPERATION_RESTORE",
    "RESTORABLE_STATUSES",
    "activate_project",
    "archive_project",
    "complete_project",
    "get_project",
    "list_projects",
    "restore_project",
]


# --------------------------------------------------------------------------------------
# Project authoring (T10/T12): create and edit the descriptive fields
# --------------------------------------------------------------------------------------

OPERATION_CREATE = "POST /api/web/v1/projects"
OPERATION_UPDATE = "PATCH /api/web/v1/projects/{id}"

MAX_TITLE_LENGTH = 200
MAX_SUMMARY_LENGTH = 2000
MAX_BACKGROUND_LENGTH = 100000

# Background text is a context version input and is edited through
# ``domain.context_service.update_project_background`` so the version rule has one owner.
BACKGROUND_OWNED_FIELDS = {"background"}
EDITABLE_METADATA_FIELDS = {"title", "summary"}


def create_project(actor: ActorContext, *, payload: dict[str, Any], idem_key: str) -> dict[str, Any]:
    """Create a DRAFT project with its first immutable context version.

    New projects are always drafts; activation is a separate, supervisor-checked action, so
    creation cannot publish an unauthorised project by accident.
    """
    _require_web_administrator(actor)
    title = _require_project_title(payload.get("title"))
    summary = _optional_text(payload.get("summary"), "summary", MAX_SUMMARY_LENGTH) or ""
    background = _optional_text(payload.get("background"), "background", MAX_BACKGROUND_LENGTH) or ""

    request_hash = canonical_request_hash(
        operation=OPERATION_CREATE,
        body={"title": title, "summary": summary, "background": background},
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OPERATION_CREATE,
        key=idem_key,
        request_hash=request_hash,
        resource_type="project",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    now = db_now()
    project = Project.objects.create(
        title=title,
        summary=summary,
        background=background,
        status=ProjectStatus.DRAFT.value,
    )

    # The initial snapshot is not an append: ``record_context_version_locked`` increments
    # ``content_version``, which would leave a brand-new project at content_version 2 with a
    # gap at version 1. Create number 1 explicitly and reuse the shared refs helper so the
    # reference-list rule still has one implementation.
    from domain.context_service import current_document_version_refs

    ProjectContextVersion.objects.create(
        project=project,
        number=project.content_version,
        background_snapshot=background,
        document_version_refs=current_document_version_refs(project),
    )

    record_activity(
        actor,
        "project.created",
        project_id=project.pk,
        after={"title": title, "status": project.status},
    )
    enqueue_outbox(
        "project.created",
        project.pk,
        {"project_id": str(project.pk), "status": project.status},
    )

    result = project_detail(project)
    complete_receipt(receipt, response_data=result, resource_type="project", resource_id=project.pk)
    return {**result, "idempotent_replay": False}


def update_project_metadata(
    actor: ActorContext, project_id, *, payload: dict[str, Any], idem_key: str
) -> dict[str, Any]:
    """Edit the descriptive fields. Background text has its own endpoint-owned service path."""
    if BACKGROUND_OWNED_FIELDS & set(payload):
        raise InvalidArgumentError(
            "背景信息必须单独修改，以便生成新的上下文版本",
            details={"fields": sorted(BACKGROUND_OWNED_FIELDS & set(payload))},
        )
    unknown = sorted(set(payload) - EDITABLE_METADATA_FIELDS - {"expected_version"})
    if unknown:
        raise InvalidArgumentError("请求包含不可修改的字段", details={"fields": unknown})
    if not EDITABLE_METADATA_FIELDS & set(payload):
        raise InvalidArgumentError("没有可修改的字段", details={"editable": sorted(EDITABLE_METADATA_FIELDS)})

    expected_version = _require_expected_version(payload.get("expected_version"))
    changes: dict[str, Any] = {}
    if "title" in payload:
        changes["title"] = _require_project_title(payload.get("title"))
    if "summary" in payload:
        changes["summary"] = _optional_text(payload.get("summary"), "summary", MAX_SUMMARY_LENGTH) or ""

    request_hash = canonical_request_hash(
        operation=OPERATION_UPDATE,
        body={**changes, "expected_version": expected_version},
        resource_id=project_id,
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OPERATION_UPDATE,
        key=idem_key,
        request_hash=request_hash,
        resource_type="project",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    plan = LockPlan()
    plan.add_project(project_id)
    acting_agent = None
    if actor.actor_type is ActorType.AGENT:
        acting_agent = load_actor_agent_locked(actor)
        plan.add_agent(acting_agent.pk)
    locked = acquire_plan(plan)

    project = locked.projects.get(project_id)
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    _require_project_editor(actor, acting_agent, project)
    if project.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": project.version}
        )

    before = {field: getattr(project, field) for field in changes}
    now = db_now()
    for field, value in changes.items():
        setattr(project, field, value)
    project.updated_at = now
    project.version = project.version + 1
    project.save(update_fields=[*changes.keys(), "updated_at", "version"])

    record_activity(
        actor,
        "project.updated",
        project_id=project.pk,
        before=before,
        after=changes,
    )
    enqueue_outbox(
        "project.updated",
        project.pk,
        {"project_id": str(project.pk), "fields": sorted(changes)},
    )

    result = project_detail(project)
    complete_receipt(receipt, response_data=result, resource_type="project", resource_id=project.pk)
    return {**result, "idempotent_replay": False}


def _require_project_title(value: Any) -> str:
    if value is None or not str(value).strip():
        raise InvalidArgumentError("title 不能为空", details={"field": "title"})
    title = str(value).strip()
    if len(title) > MAX_TITLE_LENGTH:
        raise InvalidArgumentError(
            f"title 超过长度上限 {MAX_TITLE_LENGTH}", details={"field": "title"}
        )
    return title


def _optional_text(value: Any, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > max_length:
        raise InvalidArgumentError(
            f"{field} 超过长度上限 {max_length}", details={"field": field, "max_length": max_length}
        )
    return text


def _require_project_editor(actor: ActorContext, acting_agent, project: Project) -> None:
    """Local administrator, or an enabled supervisor who is a member of this project."""
    if actor.actor_type is ActorType.WEB_OPERATOR:
        _require_web_administrator(actor)
    else:
        if acting_agent is None:
            raise RoleForbiddenError("只有项目主管可以修改项目信息")
        if acting_agent.role != AgentRole.SUPERVISOR.value:
            raise RoleForbiddenError("员工不能修改项目信息")
        if not ProjectMember.objects.filter(project=project, agent=acting_agent).exists():
            raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    if project.status == ProjectStatus.ARCHIVED.value:
        raise InvalidTransitionError("项目已归档，不接受写操作", details={"project_status": project.status})
