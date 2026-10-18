"""AgentAPI submission/review/reopen routes (T20-T22)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from agentapi.deps import AgentRequestContext, agent_context, run_write
from agentapi.envelope import success
from domain.errors import IdempotencyKeyRequiredError, InvalidArgumentError
from domain.review_service import reopen_task, review_submission
from domain.submission_service import submit_result

router = APIRouter()


class SelfCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str
    passed: bool = False
    evidence: str = ""


class LinkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str


class ArtifactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    expected_version: int
    summary: str
    content: str = ""
    self_check: list[SelfCheckItem] = []
    artifacts: list[ArtifactItem] = []
    links: list[LinkItem] = []


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    expected_version: int
    decision: str
    comment: str = ""


class ReopenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    expected_version: int


def _idempotency_key(request: Request) -> str:
    raw = request.headers.get("Idempotency-Key")
    if raw is None or not raw.strip():
        raise IdempotencyKeyRequiredError()
    return raw.strip()


def _uuid(value, field: str):
    import uuid as _uuid

    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(f"字段 {field} 必须是合法 UUID", details={"field": field}) from exc


@router.post("/tasks/{task_id}/submit")
def task_submit(
    task_id: str,
    body: SubmitBody,
    request: Request,
    ctx: AgentRequestContext = Depends(agent_context),
):
    idem_key = _idempotency_key(request)
    payload = body.model_dump()
    result = run_write(
        lambda: submit_result(
            ctx.actor, task_id=_uuid(task_id, "task_id"), payload=payload, idem_key=idem_key
        )
    )
    return success(result["data"], request_id=ctx.actor.request_id)


@router.post("/tasks/{task_id}/review")
def task_review(
    task_id: str,
    body: ReviewBody,
    request: Request,
    ctx: AgentRequestContext = Depends(agent_context),
):
    idem_key = _idempotency_key(request)
    result = run_write(
        lambda: review_submission(
            ctx.actor,
            task_id=_uuid(task_id, "task_id"),
            payload=body.model_dump(),
            idem_key=idem_key,
        )
    )
    return success(result["data"], request_id=ctx.actor.request_id)


@router.post("/tasks/{task_id}/reopen")
def task_reopen(
    task_id: str,
    body: ReopenBody,
    request: Request,
    ctx: AgentRequestContext = Depends(agent_context),
):
    idem_key = _idempotency_key(request)
    result = run_write(
        lambda: reopen_task(
            ctx.actor,
            task_id=_uuid(task_id, "task_id"),
            reason=body.reason,
            expected_version=body.expected_version,
            idem_key=idem_key,
        )
    )
    return success(result["data"], request_id=ctx.actor.request_id)
