"""WebAPI project lifecycle routes: activate/complete/archive/restore (T10).

Lifecycle is driven only by these action endpoints; the local WebAPI is the
administrator surface for it (product 2.3/5.1, product 13). Every action requires an
``Idempotency-Key`` and delegates to :mod:`domain.project_service`, which owns the
transaction, the version check and the single activity/outbox pair per real action.
"""

from __future__ import annotations

from django.urls import path
from rest_framework.views import APIView

from domain.errors import InvalidArgumentError
from domain.project_service import (
    activate_project,
    archive_project,
    complete_project,
    delete_project,
    restore_project,
)
from webapi.context import get_request_id
from webapi.envelope import success_response
from webapi.params import parse_json_body, parse_uuid, require_idempotency_key, web_actor


def _expected_version(payload: dict) -> int:
    value = payload.get("expected_version")
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError(
            "缺少或非法的 expected_version", details={"field": "expected_version"}
        )
    if value < 1:
        raise InvalidArgumentError(
            "expected_version 必须大于等于 1", details={"field": "expected_version"}
        )
    return value


class ProjectActivateView(APIView):
    """DRAFT/COMPLETED -> ACTIVE; requires an enabled supervisor member."""

    def post(self, request, project_id):
        actor = web_actor(request)
        payload = parse_json_body(request)
        data = activate_project(
            actor,
            parse_uuid(project_id, field="project_id"),
            expected_version=_expected_version(payload),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id())


class ProjectCompleteView(APIView):
    """ACTIVE -> COMPLETED; requires every non-archived task to be DONE."""

    def post(self, request, project_id):
        actor = web_actor(request)
        payload = parse_json_body(request)
        data = complete_project(
            actor,
            parse_uuid(project_id, field="project_id"),
            expected_version=_expected_version(payload),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id())


class ProjectArchiveView(APIView):
    """Any lifecycle -> ARCHIVED, saving ``archived_from_status``; reason is mandatory."""

    def post(self, request, project_id):
        actor = web_actor(request)
        payload = parse_json_body(request)
        data = archive_project(
            actor,
            parse_uuid(project_id, field="project_id"),
            reason=payload.get("reason"),
            expected_version=_expected_version(payload),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id())


class ProjectDeleteView(APIView):
    """Any lifecycle -> ARCHIVED + 删除标记（软删除）；reason is mandatory."""

    def post(self, request, project_id):
        actor = web_actor(request)
        payload = parse_json_body(request)
        data = delete_project(
            actor,
            parse_uuid(project_id, field="project_id"),
            reason=payload.get("reason"),
            expected_version=_expected_version(payload),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id())


class ProjectRestoreView(APIView):
    """ARCHIVED -> the saved previous lifecycle (DRAFT when absent)."""

    def post(self, request, project_id):
        actor = web_actor(request)
        payload = parse_json_body(request)
        data = restore_project(
            actor,
            parse_uuid(project_id, field="project_id"),
            expected_version=_expected_version(payload),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id())


urlpatterns = [
    path("projects/<str:project_id>/activate", ProjectActivateView.as_view(), name="project-activate"),
    path("projects/<str:project_id>/complete", ProjectCompleteView.as_view(), name="project-complete"),
    path("projects/<str:project_id>/archive", ProjectArchiveView.as_view(), name="project-archive"),
    path("projects/<str:project_id>/delete", ProjectDeleteView.as_view(), name="project-delete"),
    path("projects/<str:project_id>/restore", ProjectRestoreView.as_view(), name="project-restore"),
]
