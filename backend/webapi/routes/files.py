"""WebAPI file routes: private download and task attachments (T11).

Downloads stream through the API with a permission check per attachment purpose and never
expose the storage path. Task attachment uploads accept ``purpose`` (TASK_REFERENCE or
EXECUTION_ARTIFACT, the latter with a required ``execution_id``) and take the project from
the task (product 13/16, DEVELOP D09).
"""

from __future__ import annotations

from django.http import FileResponse, HttpRequest
from django.urls import path
from rest_framework.views import APIView

from domain import file_service
from domain.contracts import AttachmentPurpose
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

# Purposes this endpoint may create. SUBMISSION_ARTIFACT is frozen by the submission
# service only, so it can never be forged through an upload.
ALLOWED_UPLOAD_PURPOSES = (
    AttachmentPurpose.TASK_REFERENCE.value,
    AttachmentPurpose.EXECUTION_ARTIFACT.value,
)


def _upload_from_request(request):
    upload = request.FILES.get("file") or request.FILES.get("upload")
    if upload is None:
        candidates = list(request.FILES.values())
        upload = candidates[0] if len(candidates) == 1 else None
    if upload is None:
        raise InvalidArgumentError("缺少 multipart 文件字段 file", details={"field": "file"})
    return upload


def _purpose_field(request) -> str:
    raw = request.POST.get("purpose") or request.GET.get("purpose")
    if not raw or not str(raw).strip():
        raise InvalidArgumentError("缺少必需字段 purpose", details={"field": "purpose"})
    purpose = str(raw).strip().upper()
    if purpose not in ALLOWED_UPLOAD_PURPOSES:
        raise InvalidArgumentError(
            "purpose 只接受 TASK_REFERENCE 或 EXECUTION_ARTIFACT",
            details={"field": "purpose", "value": raw, "allowed": list(ALLOWED_UPLOAD_PURPOSES)},
        )
    return purpose


def _execution_id_field(request, purpose: str):
    raw = request.POST.get("execution_id") or request.GET.get("execution_id")
    if purpose == AttachmentPurpose.EXECUTION_ARTIFACT.value:
        if not raw or not str(raw).strip():
            raise InvalidArgumentError(
                "EXECUTION_ARTIFACT 必须携带 execution_id", details={"field": "execution_id"}
            )
        return parse_uuid(str(raw).strip(), field="execution_id")
    if raw and str(raw).strip():
        raise InvalidArgumentError(
            "TASK_REFERENCE 不接受 execution_id", details={"field": "execution_id"}
        )
    return None


class FileDownloadView(APIView):
    """Authorised private download; the storage path is never part of the response."""

    def get(self, request, file_id):
        target = file_service.authorise_download(
            web_actor(request), parse_uuid(file_id, field="file_id")
        )
        response = FileResponse(open(target.path, "rb"), content_type=target.media_type)
        response["Content-Disposition"] = file_service.content_disposition_value(
            target.original_name
        )
        response["X-File-Id"] = str(target.file_id)
        return response


class StagingFileUploadView(APIView):
    """``POST /files``: upload one unbound staging file for task authoring.

    Returns ``file_id`` plus a signed ``oss_url`` (when OSS is enabled); the caller
    passes the ids to ``POST /tasks`` as ``attachment_file_ids``, where they are bound
    and their OSS URLs are written into the task prompt. Unbound staging files are
    reclaimed by the cleanup job after the retention window.
    """

    def post(self, request: HttpRequest):
        upload = _upload_from_request(request)
        raw_project_id = (
            request.POST.get("project_id") or request.GET.get("project_id") or ""
        ).strip()
        data = file_service.upload_staging_file(
            web_actor(request),
            upload=upload,
            project_id=raw_project_id or None,
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id(), http_status=201)


class TaskAttachmentListView(APIView):
    """GET the attachment links of a task the caller may read / POST a new attachment."""

    def get(self, request, task_id):
        query = parse_query(request, allowed=LIST_QUERY_PARAMS)
        page, page_size = parse_pagination(query)
        data = file_service.list_task_attachments(
            web_actor(request),
            parse_uuid(task_id, field="task_id"),
            page=page,
            page_size=page_size,
        )
        return success_response(data, request_id=get_request_id())

    def post(self, request, task_id):
        purpose = _purpose_field(request)
        upload = _upload_from_request(request)
        data = file_service.attach_to_task(
            web_actor(request),
            parse_uuid(task_id, field="task_id"),
            upload=upload,
            purpose=purpose,
            execution_id=_execution_id_field(request, purpose),
            idem_key=require_idempotency_key(request),
        )
        return success_response(data, request_id=get_request_id(), http_status=201)


urlpatterns = [
    path("files", StagingFileUploadView.as_view(), name="staging-file-upload"),
    path("files/<str:file_id>/download", FileDownloadView.as_view(), name="file-download"),
    path(
        "tasks/<str:task_id>/attachments",
        TaskAttachmentListView.as_view(),
        name="task-attachments",
    ),
]
