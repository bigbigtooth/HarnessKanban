"""WebAPI dashboard and activity routes (T25).

``GET /dashboard`` returns the scope-consistent global statistics for the local
administrator (every project); ``GET /activity`` returns the newest-first activity feed of
the same scope. The views only parse parameters and translate the service result into the
frozen envelope - scope, counts, visibility markers and ``as_of`` all come from
``domain.dashboard_service``.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from domain import dashboard_service
from domain.errors import InvalidArgumentError
from webapi.params import parse_pagination, parse_query, web_actor
from webapi.views_base import BaseAPIView

DASHBOARD_QUERY_PARAMS = {"project_id", "include_archived"}
ACTIVITY_QUERY_PARAMS = {"project_id", "task_id", "action", "include_archived", "page", "page_size"}


class DashboardView(BaseAPIView):
    """Global statistics for the local administrator scope."""

    def get(self, request: HttpRequest):
        def run():
            query = parse_query(request, allowed=DASHBOARD_QUERY_PARAMS)
            scope = dashboard_service.DashboardScope(
                project_id=query.get_uuid("project_id"),
                include_archived=bool(query.get_bool("include_archived", False)),
            )
            return self.ok(request, dashboard_service.get_dashboard(web_actor(request), scope))

        return self.run_read(run)


class ActivityView(BaseAPIView):
    """Recent activity of the caller's scope, newest first."""

    def get(self, request: HttpRequest):
        def run():
            query = parse_query(request, allowed=ACTIVITY_QUERY_PARAMS)
            page_number, page_size = parse_pagination(query)
            action = query.get("action")
            if action is not None:
                action = action.strip()
                if not action:
                    raise InvalidArgumentError("action 不能为空", details={"parameter": "action"})
            filters = dashboard_service.ActivityFilters(
                project_id=query.get_uuid("project_id"),
                task_id=query.get_uuid("task_id"),
                action=action,
                include_archived=bool(query.get_bool("include_archived", False)),
            )
            return self.ok(
                request,
                dashboard_service.list_activity(
                    web_actor(request),
                    filters,
                    page_number=page_number,
                    page_size=page_size,
                ),
            )

        return self.run_read(run)


urlpatterns = [
    path("dashboard", DashboardView.as_view(), name="web-dashboard"),
    path("activity", ActivityView.as_view(), name="web-activity"),
]
