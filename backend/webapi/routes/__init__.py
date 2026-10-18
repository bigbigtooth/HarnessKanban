"""WebAPI route modules. Each module owns its own path list (single writer per module)."""

from __future__ import annotations

from webapi.routes import agents, dashboard, files, projects, tasks

ROUTE_MODULES = (agents, projects, tasks, files, dashboard)
