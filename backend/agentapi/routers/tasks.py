"""AgentAPI task routes: ``read_task`` plus supervisor task authoring (T13-T15).

Agent identity always comes from the ``AGENT_TOKEN`` key through ``agentapi.deps``; a body can
never name the actor. ``read_task`` is read-only: it only parses the raw multi-value query
(never a dict, which would silently drop duplicated ``lN`` keys), filters by current
authorisation and returns the claim hint without claiming, reclaiming or touching capacity.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Body, Depends, Request

from agentapi.deps import AgentRequestContext, agent_context, run_read, run_write
from agentapi.envelope import request_id_of, success
from domain import read_task_query, task_service
from domain.contracts import parse_level_filters
from domain.errors import InvalidArgumentError
from webapi.params import (
    FORBIDDEN_IDENTITY_FIELDS,
    FORBIDDEN_TASK_PATCH_FIELDS,
    parse_uuid,
    reject_fields,
    require_idempotency_key,
)

router = APIRouter()

# Documented query surface of read_task. Anything else is rejected instead of ignored.
READ_TASK_PARAMS = {
    "status",
    "project_id",
    "claim_mode",
    "designated_agent_id",
    "assignee_agent_id",
    "q",
    "include_archived",
    "available",
    "page",
    "page_size",
}

# A body may never name the acting agent or the execution outcome.
NEVER_SETTABLE_FIELDS = FORBIDDEN_IDENTITY_FIELDS | {"created_by_agent_id"}


@router.get("/read_task/")
def read_task(request: Request, ctx: AgentRequestContext = Depends(agent_context)):
    """Read-only task list with five-level difficulty selection."""
    filters, page_number, page_size = _read_task_filters(request)
    data = run_read(
        lambda: read_task_query.list_tasks(
            ctx.actor, filters, page_number=page_number, page_size=page_size
        )
    )
    return success(data, request_id=request_id_of(request))


@router.post("/tasks")
def create_task(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
    ctx: AgentRequestContext = Depends(agent_context),
):
    body = _body(payload)
    reject_fields(body, NEVER_SETTABLE_FIELDS)
    idem_key = require_idempotency_key(request)
    result = run_write(lambda: task_service.create_task(ctx.actor, body, idem_key))
    return success(result, request_id=request_id_of(request))


@router.get("/tasks/{task_id}")
def get_task(
    request: Request,
    task_id: str,
    ctx: AgentRequestContext = Depends(agent_context),
):
    task_uuid = parse_uuid(task_id, field="task_id")
    data = run_read(lambda: read_task_query.get_task_detail(ctx.actor, task_uuid))
    return success(data, request_id=request_id_of(request))


@router.patch("/tasks/{task_id}")
def edit_task(
    request: Request,
    task_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    ctx: AgentRequestContext = Depends(agent_context),
):
    body = _body(payload)
    reject_fields(body, FORBIDDEN_TASK_PATCH_FIELDS | NEVER_SETTABLE_FIELDS)
    idem_key = require_idempotency_key(request)
    result = run_write(
        lambda: task_service.edit_task(
            ctx.actor, parse_uuid(task_id, field="task_id"), body, idem_key
        )
    )
    return success(result, request_id=request_id_of(request))


@router.post("/tasks/{task_id}/publish")
def publish_task(
    request: Request,
    task_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    ctx: AgentRequestContext = Depends(agent_context),
):
    body = _body(payload)
    reject_fields(body, NEVER_SETTABLE_FIELDS)
    idem_key = require_idempotency_key(request)
    result = run_write(
        lambda: task_service.publish_task(
            ctx.actor,
            parse_uuid(task_id, field="task_id"),
            body.get("expected_version"),
            idem_key,
        )
    )
    return success(result, request_id=request_id_of(request))


@router.post("/tasks/{task_id}/withdraw")
def withdraw_task(
    request: Request,
    task_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    ctx: AgentRequestContext = Depends(agent_context),
):
    return _task_action(request, task_id, payload, ctx, task_service.withdraw_task)


@router.post("/tasks/{task_id}/assign")
def assign_task(
    request: Request,
    task_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    ctx: AgentRequestContext = Depends(agent_context),
):
    return _task_action(request, task_id, payload, ctx, task_service.assign_task)


@router.get("/tasks/{task_id}/activity")
def task_activity(
    request: Request,
    task_id: str,
    ctx: AgentRequestContext = Depends(agent_context),
):
    pairs = _raw_query_pairs(request)
    _reject_unknown_params(pairs, allowed={"page", "page_size"})
    page_number, page_size = _page_params(pairs)
    task_uuid = parse_uuid(task_id, field="task_id")
    data = run_read(
        lambda: read_task_query.list_task_activity(
            ctx.actor, task_uuid, page_number=page_number, page_size=page_size
        )
    )
    return success(data, request_id=request_id_of(request))


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _body(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    return payload


def _task_action(request: Request, task_id: str, payload, ctx: AgentRequestContext, service):
    body = _body(payload)
    reject_fields(body, NEVER_SETTABLE_FIELDS)
    idem_key = require_idempotency_key(request)
    result = run_write(
        lambda: service(ctx.actor, parse_uuid(task_id, field="task_id"), body, idem_key)
    )
    return success(result, request_id=request_id_of(request))


def _raw_query_pairs(request: Request) -> list[tuple[str, str]]:
    """The untouched multi-value query pairs.

    ``request.query_params`` / ``dict(...)`` would collapse duplicate ``l1`` keys and hide a
    client sending two different level selections, so the raw query string is parsed here.
    """
    raw = request.url.query
    return parse_qsl(raw, keep_blank_values=True)


def _read_task_filters(request: Request):
    pairs = _raw_query_pairs(request)
    _reject_unknown_params(pairs, allowed=READ_TASK_PARAMS)
    # Authoritative level parsing (duplicated lN keys, l0/l6 and non 0/1 values -> 400).
    levels = tuple(parse_level_filters(pairs))

    values: dict[str, str] = {}
    for key, value in pairs:
        values.setdefault(key, value)

    filters = read_task_query.TaskListFilters(
        project_id=_optional_uuid(values, "project_id"),
        status=values.get("status") or None,
        claim_mode=values.get("claim_mode") or None,
        designated_agent_id=_optional_uuid(values, "designated_agent_id"),
        assignee_agent_id=_optional_uuid(values, "assignee_agent_id"),
        q=(values.get("q") or "").strip() or None,
        include_archived=_bool_param(values, "include_archived", default=False),
        levels=levels,
        available=_bool_param(values, "available", default=False),
    )
    page_number, page_size = _page_params(pairs)
    return filters, page_number, page_size


def _reject_unknown_params(pairs: list[tuple[str, str]], *, allowed: set[str]) -> None:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in allowed:
            if key in seen:
                raise InvalidArgumentError(
                    f"查询参数 {key} 重复出现", details={"parameter": key}
                )
            seen.add(key)
            continue
        if _is_level_key(key):
            # Range, duplication and value rules belong to parse_level_filters.
            continue
        raise InvalidArgumentError(
            f"不支持的查询参数 {key}", details={"parameter": key}
        )


def _is_level_key(key: str) -> bool:
    return len(key) == 2 and key[0] == "l" and key[1].isdigit()


def _optional_uuid(values: dict[str, str], name: str):
    raw = values.get(name)
    if raw is None or raw == "":
        return None
    return parse_uuid(raw, field=name)


def _bool_param(values: dict[str, str], name: str, *, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or raw == "":
        return default
    if raw.lower() in ("1", "true"):
        return True
    if raw.lower() in ("0", "false"):
        return False
    raise InvalidArgumentError(
        f"参数 {name} 必须是 true/false", details={"parameter": name, "value": raw}
    )


def _int_param(values: dict[str, str], name: str, *, default: int) -> int:
    raw = values.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidArgumentError(
            f"参数 {name} 必须是整数", details={"parameter": name, "value": raw}
        ) from exc


def _page_params(pairs: list[tuple[str, str]]) -> tuple[int, int]:
    values: dict[str, str] = {}
    for key, value in pairs:
        values.setdefault(key, value)
    page_number = _int_param(values, "page", default=1)
    page_size = _int_param(values, "page_size", default=read_task_query.DEFAULT_PAGE_SIZE)
    return page_number, page_size
