"""WebAPI document routes: project documents, document versions, removal (T11/T12).

Uploads are ``multipart/form-data`` on the private local surface; the domain service
streams the bytes to private staging, validates type and real size, and only then binds in
a short transaction. Removing a document moves it out of the current reference list and
never deletes rows or blobs (product 5.2/16, DEVELOP D09).
"""

from __future__ import annotations

from django.urls import path
from rest_framework.views import APIView

from domain import file_service
from domain.errors import InvalidArgumentError
from webapi.context import get_request_id
from webapi.envelope import success_response
from webapi.params import (
    parse_pagination,
    parse_query,
    parse_uuid,
    require_idempotency_key,
    web_actor,
)

LIST_QUERY_PARAMS = {"page", "page_size"}


def _upload_from_request(request):
    """One multipart file per upload; ``file`` is the documented field name."""
    upload = request.FILES.get("file") or request.FILES.get("upload")
    if upload is None:
        candidates = list(request.FILES.values())
        upload = candidates[0] if len(candidates) == 1 else None
    if upload is None:
        raise InvalidArgumentError(
            "缺少 multipart 文件字段 file", details={"field": "file"}
        )
    return upload


def _boolean_field(raw, *, field: str) -> bool:
    if raw is None:
        return False
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    raise InvalidArgumentError(
        f"字段 {field} 必须是布尔值", details={"field": field, "value": raw}
    )


class ProjectDocumentListView(APIView):
    """GET current reference documents / POST a new document."""

    def get(self, request, project_id):
        query = parse_query(request, allowed=LIST_QUERY_PARAMS)
        page, page_size = parse_pagination(query)
        data = file_service.list_project_documents(
            web_actor(request),
            parse_uuid(project_id, field="project_id"),
            page=page,
            page_size=page_size,
        )
        return success_response(data, request_id=get_request_id())

    def post(self, request, project_id):
        upload = _upload_from_request(request)
        data = file_service.upload_project_document(
            web_actor(request),
            parse_uuid(project_id, field="project_id"),
            upload=upload,
            name=request.POST.get("name"),
            allow_duplicate_name=_boolean_field(
                request.POST.get("allow_duplicate_name"), field="allow_duplicate_name"
            ),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id(), http_status=201)


class DocumentDetailView(APIView):
    """GET document detail with version history / DELETE (move out of the reference list)."""

    def get(self, request, document_id):
        data = file_service.get_document_detail(
            web_actor(request), parse_uuid(document_id, field="document_id")
        )
        return success_response(data, request_id=get_request_id())

    def delete(self, request, document_id):
        data = file_service.archive_document(
            web_actor(request),
            parse_uuid(document_id, field="document_id"),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id())


class DocumentVersionView(APIView):
    """POST the next version of an existing document."""

    def post(self, request, document_id):
        upload = _upload_from_request(request)
        data = file_service.add_document_version(
            web_actor(request),
            parse_uuid(document_id, field="document_id"),
            upload=upload,
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id(), http_status=201)


urlpatterns = [
    path(
        "projects/<str:project_id>/documents",
        ProjectDocumentListView.as_view(),
        name="project-documents",
    ),
    path("documents/<str:document_id>", DocumentDetailView.as_view(), name="document-detail"),
    path(
        "documents/<str:document_id>/versions",
        DocumentVersionView.as_view(),
        name="document-versions",
    ),
]
