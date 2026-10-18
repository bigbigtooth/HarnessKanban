"""Project background edits and immutable context versions (T12, DEVELOP D08/D09).

A project's context is its background text plus the current reference document versions.
Both change through this module: the background via ``update_project_background`` and the
reference list via :mod:`domain.file_service`. Every change increments
``Project.content_version`` and appends a *new* ``ProjectContextVersion`` whose ``number``
equals that content version. Rows are append-only, so a claimed execution's frozen context
stays readable after the project moved on (product 5.2, 11; A05).

``current_context_for_claim`` is the exact entry point the claim task (T18) calls to freeze
the context of a new execution.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import F

from apps.projects.models import DocumentVersion, Project, ProjectContextVersion
from domain.clock import db_now
from domain.contracts import ActorContext, ActorType
from domain.errors import InvalidArgumentError, ResourceNotFoundError, VersionConflictError
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.permissions import require_project_editable, require_project_supervisor
from domain.presenters import context_version_view, project_detail
from domain.service_runner import run_request_service
from domain.transactions import assert_in_transaction

logger = logging.getLogger("domain.context_service")

OPERATION_UPDATE_BACKGROUND_AGENT = "PATCH /api/agent/v1/projects/{id}"
OPERATION_UPDATE_BACKGROUND_WEB = "PATCH /api/web/v1/projects/{id}"

MAX_BACKGROUND_LENGTH = 100_000

# Ref dicts are frozen in this exact shape (product 5.2, 11.1).
REF_KEYS = ("document_id", "document_version_id", "number", "name")


# --------------------------------------------------------------------------------------
# Immutable context versions
# --------------------------------------------------------------------------------------


def current_document_version_refs(project: Project) -> list[dict[str, Any]]:
    """Current reference list: the *current* version of every non-archived document.

    Moved-out documents (``is_archived=True``) are deliberately absent, which is exactly
    what makes a later context version differ from an older one.
    """
    rows = (
        DocumentVersion.objects.filter(
            document__project=project,
            document__is_archived=False,
            document__current_version=F("pk"),
        )
        .select_related("document")
        .order_by("document__created_at", "document_id")
    )
    return [
        {
            "document_id": str(version.document_id),
            "document_version_id": str(version.pk),
            "number": version.number,
            "name": version.document.name,
        }
        for version in rows
    ]


def latest_context_version(project: Project) -> ProjectContextVersion | None:
    return (
        ProjectContextVersion.objects.filter(project=project)
        .order_by("-number", "-created_at")
        .first()
    )


def current_context_for_claim(
    project: Project,
) -> tuple[ProjectContextVersion | None, list[dict[str, Any]]]:
    """Freeze value for a new execution: current context version + document refs.

    Call inside the claim transaction after the project row is locked (T18). Returns
    ``(context_version_row, refs)`` where ``refs`` is the frozen reference list of that
    version. When a project has no context row yet the reference list is computed live and
    ``None`` is returned as the version reference.
    """
    context = latest_context_version(project)
    if context is None:
        return None, current_document_version_refs(project)
    return context, list(context.document_version_refs or [])


def build_context_snapshot(
    project: Project, *, using_locked_project: bool = False
) -> tuple[ProjectContextVersion | None, list[dict[str, Any]]]:
    """``current_context_for_claim`` with an explicit statement about the caller's lock.

    ``using_locked_project=True`` means the caller already holds the project row lock and
    no extra locking read is issued; ``False`` re-reads the project with
    ``select_for_update()`` (must therefore run inside a transaction).
    """
    if not using_locked_project:
        locked = Project.objects.select_for_update().filter(pk=project.pk).first()
        if locked is not None:
            project = locked
    return current_context_for_claim(project)


def record_context_version_locked(
    project: Project, *, now=None, background: str | None = None
) -> ProjectContextVersion:
    """Append one immutable context version and advance ``content_version``.

    The caller holds the project row lock and is inside the outermost transaction. The
    project ``version``/``content_version`` (and an edited background, when passed) update
    is a CAS on the values read under lock, so a lost lock aborts the whole transaction
    instead of silently rewriting history.
    """
    assert_in_transaction()
    now = now or db_now()
    previous_content_version = project.content_version
    new_content_version = (previous_content_version or 0) + 1

    changes: dict[str, Any] = {
        "content_version": new_content_version,
        "version": F("version") + 1,
        "updated_at": now,
    }
    if background is not None:
        changes["background"] = background

    updated = Project.objects.filter(
        pk=project.pk,
        version=project.version,
        content_version=previous_content_version,
    ).update(**changes)
    if updated != 1:
        raise VersionConflictError(
            "项目内容版本已变化，无法追加新的上下文版本",
            details={"project_id": str(project.pk), "content_version": previous_content_version},
        )

    project.content_version = new_content_version
    project.version = project.version + 1
    project.updated_at = now
    if background is not None:
        project.background = background

    context = ProjectContextVersion.objects.create(
        project=project,
        number=new_content_version,
        background_snapshot=project.background or "",
        document_version_refs=current_document_version_refs(project),
    )
    logger.info(
        "项目上下文版本已追加：project=%s number=%s documents=%s",
        project.pk,
        context.number,
        len(context.document_version_refs or []),
    )
    return context


# --------------------------------------------------------------------------------------
# Background edits
# --------------------------------------------------------------------------------------


def update_project_background(
    actor: ActorContext,
    project_id,
    *,
    background: str,
    expected_version: int,
    idem_key: str,
) -> dict[str, Any]:
    """Edit the background text and append a new context version (T12).

    Web operator (local administrator) or a supervisor member may edit. Lifecycle and
    member rows are never touched here, and an archived project rejects the edit.
    """
    if not isinstance(background, str):
        raise InvalidArgumentError("字段 background 必须是字符串", details={"field": "background"})
    if len(background) > MAX_BACKGROUND_LENGTH:
        raise InvalidArgumentError(
            f"字段 background 超过长度上限 {MAX_BACKGROUND_LENGTH}",
            details={"field": "background", "max_length": MAX_BACKGROUND_LENGTH},
        )
    expected_version = _require_expected_version(expected_version)
    operation = _background_operation(actor)

    def service() -> dict[str, Any]:
        project = _load_locked_project(project_id)
        require_project_supervisor(actor, project)

        request_hash = canonical_request_hash(
            operation=operation,
            body={"background": background, "expected_version": expected_version},
            resource_id=str(project.pk),
            project_id=str(project.pk),
        )
        receipt = lookup_or_reserve(
            actor_scope=actor.actor_scope(),
            operation=operation,
            key=idem_key,
            request_hash=request_hash,
            resource_type="project",
        )
        if receipt.replayed:
            return {**(receipt.response_data or {}), "idempotent_replay": True}

        require_project_editable(project)
        if project.version != expected_version:
            raise VersionConflictError(
                details={"expected_version": expected_version, "current_version": project.version}
            )

        now = db_now()
        before = {
            "background_version": project.content_version,
            "version": project.version,
        }
        context = record_context_version_locked(project, now=now, background=background)

        after = {
            "background_version": project.content_version,
            "version": project.version,
            "context_version_number": context.number,
            "document_ref_count": len(context.document_version_refs or []),
        }
        record_activity(
            actor,
            "project.update_background",
            project_id=project.pk,
            before=before,
            after=after,
        )
        enqueue_outbox(
            "project.context_updated",
            project.pk,
            {
                "project_id": str(project.pk),
                "content_version": project.content_version,
                "context_version_id": str(context.pk),
                "document_ref_count": len(context.document_version_refs or []),
                "source": "background",
            },
        )

        data = project_detail(project)
        data["current_context_version"] = context_version_view(context)
        complete_receipt(
            receipt, response_data=data, resource_type="project", resource_id=project.pk
        )
        return data

    return run_request_service(service)


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _background_operation(actor: ActorContext) -> str:
    if actor.actor_type is ActorType.AGENT:
        return OPERATION_UPDATE_BACKGROUND_AGENT
    return OPERATION_UPDATE_BACKGROUND_WEB


def _load_locked_project(project_id) -> Project:
    project = Project.objects.select_for_update().filter(pk=project_id).first()
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    return project


def _require_expected_version(expected_version) -> int:
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise InvalidArgumentError(
            "缺少或非法的 expected_version", details={"field": "expected_version"}
        )
    if expected_version < 1:
        raise InvalidArgumentError("expected_version 必须大于等于 1")
    return expected_version
