"""WebAPI URL routes.

Endpoints are registered only per ``docs/CONTRACT.md``; there is no legacy alias, no
"directly set status" endpoint, and no renew endpoint. Each route module is owned by a
single task so parallel work never writes the same file.
"""

from __future__ import annotations

from webapi.routes import (
    agents,
    auth,
    dashboard,
    documents,
    files,
    ops,
    projects,
    projects_lifecycle,
    settings,
    task_execution,
    task_submission,
    tasks,
)

ROUTE_MODULES = (
    auth,
    agents,
    projects,
    projects_lifecycle,
    documents,
    tasks,
    task_execution,
    task_submission,
    files,
    dashboard,
    ops,
    settings,
)

urlpatterns: list = []
for module in ROUTE_MODULES:
    urlpatterns += list(getattr(module, "urlpatterns", []))

__all__ = ["urlpatterns", "ROUTE_MODULES"]
