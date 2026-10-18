"""AgentAPI dashboard route: authorised-project statistics only (T25).

The caller is authenticated from ``AGENT_TOKEN``; the payload is built from
``domain.dashboard_service`` with the agent's frontier as the scope, so a project the agent
is not a member of can never contribute a count, an agent figure or an exception. Asking
for such a project explicitly is a 404, never a partial answer.
"""

from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request

from agentapi.deps import AgentRequestContext, agent_context, run_read
from agentapi.envelope import request_id_of, success
from domain import dashboard_service
from domain.errors import InvalidArgumentError
from webapi.params import parse_uuid

router = APIRouter()

# Documented query surface of the dashboard. Anything else is rejected, not ignored.
DASHBOARD_QUERY_PARAMS = {"project_id", "include_archived"}


@router.get("/dashboard")
def dashboard(request: Request, ctx: AgentRequestContext = Depends(agent_context)):
    """Scope-consistent statistics of the caller's authorised projects."""
    scope = _scope_from_query(request)
    data = run_read(lambda: dashboard_service.get_dashboard(ctx.actor, scope))
    return success(data, request_id=request_id_of(request))


def _scope_from_query(request: Request) -> dashboard_service.DashboardScope:
    pairs = parse_qsl(request.url.query, keep_blank_values=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in DASHBOARD_QUERY_PARAMS:
            raise InvalidArgumentError(
                f"不支持的查询参数 {key}", details={"parameter": key}
            )
        if key in values:
            raise InvalidArgumentError(
                f"查询参数 {key} 重复出现", details={"parameter": key}
            )
        values[key] = value

    project_id = values.get("project_id") or None
    return dashboard_service.DashboardScope(
        project_id=None if project_id is None else parse_uuid(project_id, field="project_id"),
        include_archived=_bool_param(values, "include_archived", default=False),
    )


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
