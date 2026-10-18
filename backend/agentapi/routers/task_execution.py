"""AgentAPI execution routes: get_task, progress, release, reclaim.

``POST /get_task/`` is the only public claim contract. A GET on the same path is answered
with 405 by the router (no route is registered for it), so link prefetching or caching can
never claim a task.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from agentapi.deps import AgentRequestContext, agent_context, raw_query_pairs, run_write
from agentapi.envelope import success
from domain.claim_service import ClaimRequest, claim_task
from domain.contracts import EndReason, parse_level_filters
from domain.errors import IdempotencyKeyRequiredError, InvalidArgumentError
from domain.execution_service import end_execution, update_progress

router = APIRouter()

SUPPORTED_CLAIM_QUERY_KEYS = {"l1", "l2", "l3", "l4", "l5", "project_id"}


class GetTaskBody(BaseModel):
    """Empty object claims automatically; ``task_id`` + ``expected_version`` claims one task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    expected_version: int | None = None


class ProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    message: str
    percent: int | None = None


class EndExecutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    expected_version: int | None = None
    reason: str = ""


def _idempotency_key(request: Request) -> str:
    raw = request.headers.get("Idempotency-Key")
    if raw is None or not raw.strip():
        raise IdempotencyKeyRequiredError()
    return raw.strip()


def _parse_uuid(value, field: str):
    import uuid as _uuid

    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(f"字段 {field} 必须是合法 UUID", details={"field": field}) from exc


def _claim_scope(request: Request):
    pairs = raw_query_pairs(request)
    unknown = sorted({key for key, _ in pairs if key not in SUPPORTED_CLAIM_QUERY_KEYS})
    if unknown:
        raise InvalidArgumentError(
            "get_task 不支持该查询参数", details={"unsupported": unknown}
        )
    levels = parse_level_filters([(key, value) for key, value in pairs if key.startswith("l")])
    project_values = [value for key, value in pairs if key == "project_id"]
    if len(project_values) > 1:
        raise InvalidArgumentError("查询参数 project_id 重复出现", details={"parameter": "project_id"})
    project_id = _parse_uuid(project_values[0], "project_id") if project_values else None
    return levels, project_id


@router.post("/get_task/")
def get_task(
    request: Request,
    body: GetTaskBody | None = None,
    ctx: AgentRequestContext = Depends(agent_context),
):
    idem_key = _idempotency_key(request)
    levels, project_id = _claim_scope(request)
    payload = body or GetTaskBody()

    task_id = _parse_uuid(payload.task_id, "task_id") if payload.task_id else None
    if task_id is not None and payload.expected_version is None:
        raise InvalidArgumentError("指定 task_id 领取必须携带 expected_version")

    claim_request = ClaimRequest(
        levels=levels,
        project_id=project_id,
        task_id=task_id,
        expected_version=payload.expected_version,
        source="agent",
        fields={"task_id": payload.task_id, "expected_version": payload.expected_version},
    )
    result = run_write(
        lambda: claim_task(ctx.actor, claim_request, idem_key), global_claim_lock=True
    )
    if result.get("data") is None:
        return success(None, request_id=ctx.actor.request_id)
    data = dict(result["data"])
    data["reason"] = result.get("reason")
    return success(data, request_id=ctx.actor.request_id)


@router.post("/tasks/{task_id}/progress")
def task_progress(
    task_id: str,
    body: ProgressBody,
    request: Request,
    ctx: AgentRequestContext = Depends(agent_context),
):
    idem_key = _idempotency_key(request)
    result = run_write(
        lambda: update_progress(
            ctx.actor,
            task_id=_parse_uuid(task_id, "task_id"),
            execution_id=_parse_uuid(body.execution_id, "execution_id"),
            message=body.message,
            percent=body.percent,
            idem_key=idem_key,
        )
    )
    return success(result["data"], request_id=ctx.actor.request_id)


class _AgentEndBody(EndExecutionBody):
    pass


def _end(task_id: str, body: EndExecutionBody, request: Request, ctx, reason: EndReason):
    idem_key = _idempotency_key(request)
    result = run_write(
        lambda: end_execution(
            ctx.actor,
            task_id=_parse_uuid(task_id, "task_id"),
            execution_id=_parse_uuid(body.execution_id, "execution_id"),
            reason=reason,
            expected_version=body.expected_version,
            reason_text=body.reason,
            idem_key=idem_key,
        ),
        global_claim_lock=False,
    )
    return success(result["data"], request_id=ctx.actor.request_id)


@router.post("/tasks/{task_id}/release")
def task_release(
    task_id: str,
    body: _AgentEndBody,
    request: Request,
    ctx: AgentRequestContext = Depends(agent_context),
):
    return _end(task_id, body, request, ctx, EndReason.RELEASE)


@router.post("/tasks/{task_id}/reclaim")
def task_reclaim(
    task_id: str,
    body: _AgentEndBody,
    request: Request,
    ctx: AgentRequestContext = Depends(agent_context),
):
    return _end(task_id, body, request, ctx, EndReason.RECLAIM)
