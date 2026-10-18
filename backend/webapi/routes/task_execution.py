"""WebAPI execution routes: proxy claim, progress, release, reclaim."""

from __future__ import annotations

from django.urls import path

from webapi.routes.task_execution_views import (
    WebTaskClaimView,
    WebTaskProgressView,
    WebTaskReclaimView,
    WebTaskReleaseView,
)

urlpatterns = [
    path("tasks/<uuid:task_id>/claim", WebTaskClaimView.as_view(), name="web-task-claim"),
    path("tasks/<uuid:task_id>/progress", WebTaskProgressView.as_view(), name="web-task-progress"),
    path("tasks/<uuid:task_id>/release", WebTaskReleaseView.as_view(), name="web-task-release"),
    path("tasks/<uuid:task_id>/reclaim", WebTaskReclaimView.as_view(), name="web-task-reclaim"),
]
