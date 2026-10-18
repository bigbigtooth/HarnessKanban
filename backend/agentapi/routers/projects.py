"""AgentAPI project routes: authorised projects, background edits and documents (T08/T12).

An agent sees only the projects it is authorised for. ``PATCH /projects/{id}`` edits the
background text only — never the lifecycle, members, title or versions — and each edit
appends an immutable context version through :mod:`domain.context_service`. Document
uploads and document versions are supervisor (or local administrator) operations.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request

from agentapi.deps import AgentRequestContext, agent_context
from agentapi.envelope import request_id_of, success
from agentapi.routers.files import (
    parse_upload_request,
    require_idempotency_key,
)
from domain import file_service
from domain.context_service import update_project_background
from domain.errors import InvalidArgumentError, UnsupportedParameterError
from domain.project_service import get_project, list_projects

router = APIRouter()

LIST_QUERY_PARAMS = {"page", "page_size", "status", "include_archived"}
DOCUMENT_QUERY_PARAMS = {"page", "page_size"}
DOCUMENT_UPLOAD_QUERY_PARAMS = {"filename", "name", "allow_duplicate_name"}

PROJECT_PATCH_ALLOWED = {"background", "expected_version"}
# Lifecycle and membership change only through the local WebAPI actions.
PROJECT_PATCH_FORBIDDEN = {
    "status",
    "archived_from_status",
    "content_version",
    "version",
    "members",
    "member_agent_ids",
    "agents",
    "lifecycle",
    "title",
    "summary",
    "is_archived",
}
IDENTITY_FIELDS = {
    "agent_id",
    "actor_agent_id",
    "assignee_agent_id",
    "reviewer_agent_id",
    "submitted_by_agent_id",
    "project_id",
    "key",
    "key_version",
}


@router.get("/projects")
def list_authorised_projects(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
    include_archived: bool = Query(default=True),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Projects this Key is authorised for (never the whole database)."""
    _reject_unknown_query_params(request, LIST_QUERY_PARAMS)
    data = list_projects(
        ctx.actor,
        status_filter=status,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )
    return success(data, request_id=request_id_of(request))


@router.get("/projects/{project_id}")
def get_authorised_project(
    request: Request, project_id: str, ctx: AgentRequestContext = Depends(agent_context)
):
    """Background plus the current context version pointer."""
    data = get_project(ctx.actor, project_id)
    return success(data, request_id=request_id_of(request))


@router.patch("/projects/{project_id}")
def patch_project_background(
    request: Request,
    project_id: str,
    payload: dict = Body(...),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Supervisor background edit; lifecycle and members are not writable here."""
    _reject_unsupported_patch_fields(payload)
    background = payload.get("background")
    if not isinstance(background, str):
        raise InvalidArgumentError("缺少字段 background", details={"field": "background"})
    expected_version = payload.get("expected_version")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise InvalidArgumentError(
            "缺少或非法的 expected_version", details={"field": "expected_version"}
        )

    data = update_project_background(
        ctx.actor,
        project_id,
        background=background,
        expected_version=expected_version,
        idem_key=require_idempotency_key(request),
    )
    return success(data, request_id=request_id_of(request))


@router.get("/projects/{project_id}/documents")
def list_project_documents(
    request: Request,
    project_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Current reference documents of an authorised project."""
    _reject_unknown_query_params(request, DOCUMENT_QUERY_PARAMS)
    data = file_service.list_project_documents(
        ctx.actor, project_id, page=page, page_size=page_size
    )
    return success(data, request_id=request_id_of(request))


@router.post("/projects/{project_id}/documents")
def upload_project_document(
    request: Request,
    project_id: str,
    body: bytes = Body(media_type="application/octet-stream"),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Supervisor upload of a new project document."""
    parsed = parse_upload_request(request, body, allowed_query_params=DOCUMENT_UPLOAD_QUERY_PARAMS)
    data = file_service.upload_project_document(
        ctx.actor,
        project_id,
        upload=parsed.as_upload(),
        name=parsed.field("name"),
        allow_duplicate_name=_boolean_field(
            parsed.field("allow_duplicate_name"), field="allow_duplicate_name"
        ),
        idem_key=require_idempotency_key(request),
    )
    return success(data, request_id=request_id_of(request), status_code=201)


@router.post("/documents/{document_id}/versions")
def add_document_version(
    request: Request,
    document_id: str,
    body: bytes = Body(media_type="application/octet-stream"),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Supervisor upload of the next document version (old versions stay readable)."""
    parsed = parse_upload_request(request, body, allowed_query_params=DOCUMENT_UPLOAD_QUERY_PARAMS)
    data = file_service.add_document_version(
        ctx.actor,
        document_id,
        upload=parsed.as_upload(),
        idem_key=require_idempotency_key(request),
    )
    return success(data, request_id=request_id_of(request), status_code=201)


# --------------------------------------------------------------------------------------
# Payload validation helpers
# --------------------------------------------------------------------------------------


def _reject_unknown_query_params(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params.keys()) - allowed)
    if unknown:
        raise UnsupportedParameterError(
            "不支持的查询参数", details={"parameters": unknown, "allowed": sorted(allowed)}
        )


def _reject_unsupported_patch_fields(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    identity = sorted(set(payload) & IDENTITY_FIELDS)
    if identity:
        raise InvalidArgumentError(
            "请求体不得包含身份或资源字段，身份来自 AGENT_TOKEN",
            details={"fields": identity},
        )
    forbidden = sorted(set(payload) & PROJECT_PATCH_FORBIDDEN)
    if forbidden:
        raise InvalidArgumentError(
            "项目生命周期、成员和标题只能通过 Web 管理端点修改",
            details={"fields": forbidden},
        )
    unknown = sorted(set(payload) - PROJECT_PATCH_ALLOWED)
    if unknown:
        raise UnsupportedParameterError("请求包含不支持的字段", details={"fields": unknown})


def _boolean_field(raw: str | None, *, field: str) -> bool:
    if raw is None:
        return False
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    raise InvalidArgumentError(
        f"字段 {field} 必须是布尔值", details={"field": field, "value": raw}
    )
