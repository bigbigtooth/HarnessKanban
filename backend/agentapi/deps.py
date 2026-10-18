"""AgentAPI dependencies and the synchronous request bridge.

FastAPI sync routes run in a worker thread. Authentication, the domain transaction, result
evaluation and connection cleanup all happen inside that one call, because two separate
sync dependencies are not guaranteed to share the same thread.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import TypeVar

from fastapi import Request
from django.db import connections

from apps.identity.models import Agent
from domain.clock import db_now
from domain.contracts import ActorContext, ActorType
from domain.errors import AgentTokenMissingError
from domain.permissions import authenticate_agent_token
from domain.service_runner import run_request_service

logger = logging.getLogger("agentapi.deps")

T = TypeVar("T")

AGENT_TOKEN_HEADER = "AGENT_TOKEN"


@dataclass
class AgentRequestContext:
    actor: ActorContext
    agent: Agent

    @property
    def agent_id(self) -> uuid.UUID:
        return self.agent.pk


def read_agent_token(request: Request) -> str | None:
    """Read ``AGENT_TOKEN`` without the underscore/hyphen automatic conversion.

    Starlette's header lookup is case-insensitive but treats ``agent_token`` and
    ``agent-token`` as different names, which is exactly what product 12.1 requires: a
    proxy that rewrites the header must cause an authentication failure, not a silent
    acceptance.
    """
    value = request.headers.get(AGENT_TOKEN_HEADER)
    if value is None:
        # Explicitly probe the common misspelling so the failure is attributable.
        if request.headers.get("AGENT-TOKEN") is not None:
            raise AgentTokenMissingError(
                "请求头必须是 AGENT_TOKEN，不接受 AGENT-TOKEN",
                details={"received_header": "AGENT-TOKEN"},
            )
    return value


def agent_context(request: Request) -> Generator[AgentRequestContext, None, None]:
    """FastAPI dependency: authenticate, yield the actor, then always clean up."""
    try:
        token = read_agent_token(request)
        agent = authenticate_agent_token(token)
        actor = ActorContext(
            actor_type=ActorType.AGENT,
            request_id=getattr(request.state, "request_id", str(uuid.uuid4())),
            agent_id=agent.pk,
            key_version=agent.key_version,
        )
        yield AgentRequestContext(actor=actor, agent=agent)
    finally:
        # Connection cleanup must happen on the failure path too.
        connections.close_all()


def run_write(callback: Callable[[], T], *, global_claim_lock: bool = False) -> T:
    return run_request_service(callback, global_claim_lock=global_claim_lock)


def run_read(callback: Callable[[], T]) -> T:
    return callback()


def now_utc():
    return db_now()


def raw_query_pairs(request: Request) -> list[tuple[str, str]]:
    """Untouched ``key=value`` pairs from the raw query string.

    ``request.query_params`` is a multi-dict that would silently collapse duplicated
    ``l1=1&l1=0``; the level parser must see the duplicates so it can reject them.
    """
    from urllib.parse import parse_qsl

    query = request.scope.get("query_string", b"")
    if isinstance(query, bytes):
        query = query.decode("utf-8", errors="replace")
    return parse_qsl(query, keep_blank_values=True)
