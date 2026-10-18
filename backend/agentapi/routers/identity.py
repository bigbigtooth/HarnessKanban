"""AgentAPI identity routes: ``GET /me`` and ``POST /heartbeat`` (T07, D06).

Both routes authenticate with the ``AGENT_TOKEN`` header through ``agentapi.deps`` (which
keeps the underscore and never treats ``AGENT-TOKEN`` as the same header). ``/me`` is a pure
read; ``/heartbeat`` records the *server* receive time from the database clock and can never
extend an execution deadline or change a task state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from django.db import connections
from pydantic import BaseModel

from agentapi.deps import AgentRequestContext, agent_context, run_write
from agentapi.envelope import success
from domain import agent_service

router = APIRouter()


class HeartbeatRequest(BaseModel):
    """Body is optional; only bounded runtime info may be reported."""

    runtime: dict[str, Any] | None = None


@router.get("/me")
def read_me(request: Request, ctx: AgentRequestContext = Depends(agent_context)):
    try:
        data = agent_service.agent_self(ctx.actor, ctx.agent)
        return success(data, request_id=request.state.request_id)
    finally:
        connections.close_all()


@router.post("/heartbeat")
def beat(
    request: Request,
    body: HeartbeatRequest | None = None,
    ctx: AgentRequestContext = Depends(agent_context),
):
    runtime = None if body is None else body.runtime
    try:
        data = run_write(lambda: agent_service.heartbeat(ctx.actor, runtime))
        return success(data, request_id=request.state.request_id)
    finally:
        connections.close_all()
