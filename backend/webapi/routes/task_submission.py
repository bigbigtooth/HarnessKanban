"""WebAPI submission/review/reopen routes (T20-T22)."""

from __future__ import annotations

from django.urls import path

from webapi.routes.task_submission_views import (
    WebTaskReopenView,
    WebTaskReviewView,
    WebTaskSubmitView,
)

urlpatterns = [
    path("tasks/<uuid:task_id>/submit", WebTaskSubmitView.as_view(), name="web-task-submit"),
    path("tasks/<uuid:task_id>/review", WebTaskReviewView.as_view(), name="web-task-review"),
    path("tasks/<uuid:task_id>/reopen", WebTaskReopenView.as_view(), name="web-task-reopen"),
]
