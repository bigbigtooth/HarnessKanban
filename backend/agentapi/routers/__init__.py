"""AgentAPI router modules. One owner per module keeps parallel work conflict-free."""

from __future__ import annotations

from agentapi.routers import (
    dashboard,
    files,
    identity,
    projects,
    task_execution,
    task_submission,
    tasks,
)

# ``/get_task/`` registration order matters only for OpenAPI output; paths never overlap.
ROUTE_MODULES = (
    identity,
    projects,
    tasks,
    task_execution,
    task_submission,
    files,
    dashboard,
)
