"""AgentAPI file routes: authorised downloads and current-execution uploads (T11).

Uploads arrive from agent clients. The dependency set of this project deliberately does not
include ``python-multipart`` (``FastAPI``'s ``File``/``Form`` would refuse to start without
it), so the route reads the request body as raw bytes and parses ``multipart/form-data``
with the small, bounded parser below. Both encodings are accepted:

* ``multipart/form-data`` with a ``file`` part (``purpose``/``execution_id`` may be parts);
* any other content type, the body *is* the file, with metadata in the query string.

The bytes are handed to :mod:`domain.file_service`, which streams them to private staging,
validates type/size, and binds them in a short transaction. Agent uploads always run under
the authenticated Key: the execution must belong to that agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from django.conf import settings
from fastapi import APIRouter, Body, Depends, Request
from starlette.responses import FileResponse

from agentapi.deps import AgentRequestContext, agent_context
from agentapi.envelope import request_id_of, success
from domain import file_service
from domain.contracts import AttachmentPurpose
from domain.errors import (
    FileTooLargeError,
    IdempotencyKeyRequiredError,
    InvalidArgumentError,
    UnsupportedParameterError,
)
from domain.file_service import InMemoryUpload

router = APIRouter()

MULTIPART_CONTENT_TYPE = "multipart/form-data"
MULTIPART_FRAMING_SLACK_BYTES = 1024 * 1024
MAX_FIELD_LENGTH = 4096

UPLOAD_QUERY_PARAMS = {"purpose", "execution_id", "filename", "name", "allow_duplicate_name", "project_id"}
ALLOWED_UPLOAD_PURPOSES = (
    AttachmentPurpose.TASK_REFERENCE.value,
    AttachmentPurpose.EXECUTION_ARTIFACT.value,
)

_QUOTED_PARAM = re.compile(r'(?:^|;)\s*([A-Za-z0-9*_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"')
_BARE_PARAM = re.compile(r'(?:^|;)\s*([A-Za-z0-9*_-]+)\s*=\s*([^";\s]+)')


@dataclass(frozen=True)
class ParsedUpload:
    filename: str
    fields: dict[str, str]
    content: bytes

    def as_upload(self) -> InMemoryUpload:
        return InMemoryUpload(name=self.filename, content=self.content)

    def field(self, name: str, default: str | None = None) -> str | None:
        value = self.fields.get(name)
        if value is None:
            return default
        return value.strip() or default


@router.get("/files/{file_id}/download")
def download_file(request: Request, file_id: str, ctx: AgentRequestContext = Depends(agent_context)):
    """Private download for a project document or a readable task artifact."""
    target = file_service.authorise_download(ctx.actor, file_id)
    return FileResponse(
        path=str(target.path),
        media_type=target.media_type,
        headers={
            "Content-Disposition": file_service.content_disposition_value(target.original_name),
            "X-File-Id": str(target.file_id),
        },
    )


@router.post("/files")
def upload_staging_file(
    request: Request,
    body: bytes = Body(media_type="application/octet-stream"),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Upload one unbound staging file (supervisors only) for task authoring.

    Same contract as the Web ``POST /files``: returns ``file_id`` plus the signed
    ``oss_url``; the ids are bound later by ``POST /tasks`` via ``attachment_file_ids``.
    """
    parsed = parse_upload_request(request, body)
    project_id = parsed.field("project_id")
    data = file_service.upload_staging_file(
        ctx.actor,
        upload=parsed.as_upload(),
        project_id=project_id or None,
        idem_key=require_idempotency_key(request),
    )
    return success(data, request_id=request_id_of(request), status_code=201)


@router.post("/tasks/{task_id}/attachments")
def upload_task_attachment(
    request: Request,
    task_id: str,
    body: bytes = Body(media_type="application/octet-stream"),
    ctx: AgentRequestContext = Depends(agent_context),
):
    """Upload a task reference or the current execution's artifact."""
    parsed = parse_upload_request(request, body)
    purpose = (parsed.field("purpose") or "").upper()
    if purpose not in ALLOWED_UPLOAD_PURPOSES:
        raise InvalidArgumentError(
            "purpose 只接受 TASK_REFERENCE 或 EXECUTION_ARTIFACT",
            details={"field": "purpose", "value": parsed.field("purpose")},
        )
    execution_id = parsed.field("execution_id")
    if purpose == AttachmentPurpose.EXECUTION_ARTIFACT.value and not execution_id:
        raise InvalidArgumentError(
            "EXECUTION_ARTIFACT 必须携带 execution_id", details={"field": "execution_id"}
        )
    if purpose == AttachmentPurpose.TASK_REFERENCE.value and execution_id:
        raise InvalidArgumentError(
            "TASK_REFERENCE 不接受 execution_id", details={"field": "execution_id"}
        )

    data = file_service.attach_to_task(
        ctx.actor,
        task_id,
        upload=parsed.as_upload(),
        purpose=purpose,
        execution_id=execution_id,
        idem_key=require_idempotency_key(request),
    )
    return success(data, request_id=request_id_of(request), status_code=201)


# --------------------------------------------------------------------------------------
# Upload request parsing (raw body or self-parsed multipart/form-data)
# --------------------------------------------------------------------------------------


def parse_upload_request(
    request: Request, body: bytes, *, allowed_query_params: set[str] | None = None
) -> ParsedUpload:
    """Return ``(filename, fields, content)`` for one uploaded file plus metadata."""
    if not body:
        raise InvalidArgumentError("缺少上传内容", details={"field": "file"})

    content_type_header = request.headers.get("content-type") or ""
    content_type = content_type_header.split(";")[0].strip().lower()
    query_fields = {key: value for key, value in request.query_params.items()}
    check_upload_query_params(query_fields, allowed_query_params)

    limit = settings.HARNESSTASK["max_upload_bytes"]
    if content_type == MULTIPART_CONTENT_TYPE:
        if len(body) > limit + MULTIPART_FRAMING_SLACK_BYTES:
            raise FileTooLargeError(
                f"上传内容超过大小限制 {limit} 字节", details={"max_upload_bytes": limit}
            )
        filename, content, fields = split_multipart(content_type_header, body)
        merged = {**query_fields, **fields}
        return ParsedUpload(filename=filename, fields=merged, content=content)

    if len(body) > limit:
        raise FileTooLargeError(
            f"文件超过大小限制 {limit} 字节", details={"max_upload_bytes": limit}
        )
    filename = (
        query_fields.get("filename")
        or request.headers.get("x-filename")
        or request.headers.get("x-file-name")
        or ""
    )
    return ParsedUpload(filename=filename, fields=query_fields, content=body)


def check_upload_query_params(
    fields: dict[str, str], allowed: set[str] | None = None
) -> None:
    allowed = UPLOAD_QUERY_PARAMS if allowed is None else allowed
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise UnsupportedParameterError(
            "不支持的查询参数",
            details={"parameters": unknown, "allowed": sorted(allowed)},
        )


def split_multipart(content_type_header: str, body: bytes) -> tuple[str, bytes, dict[str, str]]:
    """Minimal, bounded ``multipart/form-data`` parser for a single file part."""
    boundary = multipart_boundary(content_type_header)
    delimiter = b"--" + boundary
    filename: str | None = None
    file_content: bytes | None = None
    fields: dict[str, str] = {}

    for raw_part in body.split(delimiter)[1:]:
        if raw_part.startswith(b"--"):  # closing delimiter
            break
        part = raw_part[2:] if raw_part.startswith(b"\r\n") else raw_part.lstrip(b"\n")
        header_blob, separator, content = part.partition(b"\r\n\r\n")
        if not separator:
            header_blob, separator, content = part.partition(b"\n\n")
            if not separator:
                continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]

        headers = parse_part_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        if "form-data" not in disposition.lower():
            continue
        params = disposition_params(disposition)
        field_name = params.get("name")
        part_filename = params.get("filename")
        if file_content is None and part_filename is not None:
            filename = part_filename
            file_content = content
            continue
        if field_name:
            fields.setdefault(field_name, decode_text(content))

    if file_content is None:
        raise InvalidArgumentError(
            "缺少 multipart 文件字段 file", details={"field": "file"}
        )
    return filename or "upload", file_content, fields


def multipart_boundary(content_type_header: str) -> bytes:
    params = disposition_params(content_type_header)
    boundary = params.get("boundary")
    if not boundary:
        raise InvalidArgumentError("multipart 请求缺少 boundary 参数")
    encoded = boundary.encode("utf-8")
    if len(encoded) > 70 or b"\r" in encoded or b"\n" in encoded:
        raise InvalidArgumentError("multipart boundary 不合法")
    return encoded


def parse_part_headers(header_blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in re.split(rb"\r\n|\n", header_blob):
        if not line.strip():
            continue
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        headers[name.strip().decode("latin-1").lower()] = value.strip().decode("latin-1")
    return headers


def disposition_params(header: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in _BARE_PARAM.findall(header):
        params.setdefault(key.lower(), value)
    for key, value in _QUOTED_PARAM.findall(header):
        params[key.lower()] = _unescape(value)
    for key in list(params):
        if key.endswith("*"):
            params.setdefault(key[:-1], unquote(params[key].split("''", 1)[-1]))
    return params


def decode_text(content: bytes) -> str:
    return content.decode("utf-8", "replace")[:MAX_FIELD_LENGTH]


def require_idempotency_key(request: Request) -> str:
    raw = request.headers.get("Idempotency-Key")
    if raw is None or not raw.strip():
        raise IdempotencyKeyRequiredError()
    return raw.strip()


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")
