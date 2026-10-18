"""Controlled file upload, private storage, download and purpose-scoped ownership (T11).

Rules implemented here (DEVELOP D09 / product 16 and A19):

* Only a whitelist of extensions **plus** real content sniffing is accepted. Executables,
  HTML and SVG content are rejected even when renamed; the size limit is enforced against
  the real number of streamed bytes, never against ``Content-Length``.
* The storage key is generated server-side (``uuid4().hex`` + whitelisted extension) under
  ``settings.FILE_STORAGE_ROOT``; the original filename is display metadata only and a name
  containing a path separator or ``..`` is rejected outright.
* Upload and bind are separate: bytes are streamed to a private staging directory and
  validated first, the staging ``FileAsset`` row is committed (``bound_at`` stays NULL), and
  only then does a *short* binding transaction re-verify authorisation, project editability,
  purpose and — for ``EXECUTION_ARTIFACT`` — the current execution with its fixed deadline
  through :func:`domain.permissions.current_execution_write_guard`. A failed bind leaves the
  staging asset unbound instead of creating a fake attachment.
* Access is decided by the attachment purpose plus the caller's live project membership and
  execution ownership; unauthorised resources answer 404 so object existence cannot be probed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from django.conf import settings
from django.db.models import Max, Q

from apps.files.models import AttachmentLink, FileAsset
from apps.identity.models import Agent
from apps.projects.models import Document, DocumentVersion, Project, ProjectMember
from apps.tasks.models import Task, TaskExecution, TaskSubmission
from domain import oss_storage
from domain.clock import db_now
from domain.contracts import (
    ActorContext,
    ActorType,
    AgentRole,
    AttachmentPurpose,
    TaskStatus,
)
from domain.context_service import (
    current_document_version_refs,
    record_context_version_locked,
)
from domain.errors import (
    AgentDisabledError,
    AgentTokenInvalidError,
    AttachmentLimitExceededError,
    DocumentConflictError,
    ExecutionNotCurrentError,
    ExecutionTimeoutError,
    FileTooLargeError,
    IdempotencyKeyRequiredError,
    InvalidArgumentError,
    ResourceNotFoundError,
    RoleForbiddenError,
    UnsupportedMediaTypeError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.permissions import (
    accessible_project_ids,
    current_execution_write_guard,
    load_actor_agent_locked,
    require_project_editable,
    task_is_visible_to,
)
from domain.presenters import (
    attachment_view,
    context_version_view,
    document_version_view,
    document_view,
    file_asset_view,
)
from domain.service_runner import run_request_service
from domain.transactions import assert_in_transaction

logger = logging.getLogger("domain.file_service")

# --------------------------------------------------------------------------------------
# Whitelist
# --------------------------------------------------------------------------------------

# Extension -> canonical media type (product 16, DEVELOP D09).
ALLOWED_EXTENSIONS: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}

TEXT_EXTENSIONS = (".md", ".txt")
ZIP_CONTAINER_EXTENSIONS = (".docx", ".xlsx", ".zip")

DEFAULT_MEDIA_TYPE = "application/octet-stream"

MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".docx": (b"PK\x03\x04", b"PK\x05\x06"),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06"),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
}

# Executables and scripts are refused regardless of the extension.
EXECUTABLE_SIGNATURES = (b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")

# Markup that must never be delivered as a "document" (HTML/SVG/script content).
MARKUP_SIGNATURES = (b"<!doctype html", b"<html", b"<svg", b"<script", b"<iframe", b"<object")

CHUNK_SIZE = 64 * 1024
STAGING_DIRNAME = "staging"
OBJECT_DIRNAME = "objects"
MAX_ORIGINAL_NAME_LENGTH = 200

_STORAGE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}(\.[a-z0-9]{1,8})$")

# Internal purposes; ``PROJECT_DOCUMENT`` is realised as a DocumentVersion, not a link.
PURPOSE_PROJECT_DOCUMENT = "PROJECT_DOCUMENT"
UPLOAD_PURPOSES = (PURPOSE_PROJECT_DOCUMENT, "TASK_REFERENCE", "EXECUTION_ARTIFACT")


# --------------------------------------------------------------------------------------
# Small value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InMemoryUpload:
    """Adapter-side upload source for bodies that are already in memory (AgentAPI)."""

    name: str
    content: bytes

    def chunks(self, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]


@dataclass(frozen=True)
class StagedUpload:
    original_name: str
    extension: str
    media_type: str
    size: int
    sha256: str
    storage_key: str
    staging_path: Path

    def discard(self) -> None:
        _safe_unlink(self.staging_path)


@dataclass(frozen=True)
class DownloadTarget:
    file_id: uuid.UUID
    path: Path
    original_name: str
    media_type: str
    size: int


@dataclass(frozen=True)
class UploadSpec:
    purpose: str
    project: Project
    task: Task | None = None
    document: Document | None = None
    execution: TaskExecution | None = None
    display_name: str | None = None
    allow_duplicate_name: bool = False


# --------------------------------------------------------------------------------------
# Storage helpers
# --------------------------------------------------------------------------------------


def storage_root() -> Path:
    root = Path(settings.FILE_STORAGE_ROOT)
    if not root.is_absolute():
        raise RuntimeError("FILE_STORAGE_ROOT 必须是绝对路径")
    return root


def staging_directory() -> Path:
    return _ensure_directory(storage_root() / STAGING_DIRNAME)


def object_directory() -> Path:
    return _ensure_directory(storage_root() / OBJECT_DIRNAME)


def object_path(storage_key: str) -> Path:
    """Resolve a server-generated storage key to a path inside the storage root."""
    if not _STORAGE_KEY_PATTERN.match(storage_key or ""):
        raise RuntimeError(f"非法的存储键，拒绝访问：{storage_key!r}")
    return object_directory() / storage_key


def resolve_storage_path(storage_key: str) -> Path:
    resolved = object_path(storage_key).resolve()
    root = storage_root().resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"存储路径越界：{storage_key!r}")
    return resolved


def sanitize_original_name(raw_name: Any) -> str:
    """Validate the display name; it never participates in a disk path.

    Path separators, ``..``-only names, control characters and NUL bytes are rejected so a
    crafted filename cannot influence where the bytes are written or how they are served.
    """
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise InvalidArgumentError("缺少上传文件名", details={"field": "file"})
    name = raw_name.strip()
    if "\x00" in name:
        raise InvalidArgumentError("上传文件名包含非法字符", details={"field": "file"})
    if any(separator in name for separator in ("/", "\\")):
        raise InvalidArgumentError(
            "上传文件名不得包含路径分隔符", details={"field": "file", "name": name[:120]}
        )
    if name in (".", ".."):
        raise InvalidArgumentError("上传文件名不合法", details={"field": "file"})
    if any(not character.isprintable() for character in name):
        raise InvalidArgumentError("上传文件名包含控制字符", details={"field": "file"})
    return name[:MAX_ORIGINAL_NAME_LENGTH]


def media_type_for_name(name: str) -> tuple[str, str]:
    """Whitelist check by extension. Returns ``(extension, media_type)``."""
    lowered = name.lower()
    for extension in ALLOWED_EXTENSIONS:
        if lowered.endswith(extension):
            return extension, ALLOWED_EXTENSIONS[extension]
    raise UnsupportedMediaTypeError(
        "不支持的文件类型，仅接受 Markdown/txt/pdf/docx/xlsx/png/jpg/zip",
        details={"allowed_extensions": sorted(ALLOWED_EXTENSIONS)},
    )


def sanitize_download_name(raw_name: Any) -> str:
    name = str(raw_name or "").replace("\\", "/").split("/")[-1]
    name = "".join(character for character in name if character.isprintable() and character not in '"')
    name = name.strip() or "download"
    return name[:MAX_ORIGINAL_NAME_LENGTH]


def content_disposition_value(raw_name: Any) -> str:
    """``attachment`` disposition with a sanitised filename (DEVELOP D09)."""
    name = sanitize_download_name(raw_name)
    ascii_name = (
        name.encode("ascii", "ignore").decode("ascii").replace('"', "").strip() or "download"
    )
    if name == ascii_name:
        return f'attachment; filename="{ascii_name}"'
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


# --------------------------------------------------------------------------------------
# Upload: stream + validate (never inside a business transaction)
# --------------------------------------------------------------------------------------


def stage_upload(upload, *, max_bytes: int | None = None) -> StagedUpload:
    """Stream one upload to private staging, enforcing real type and size as it arrives."""
    original_name = sanitize_original_name(getattr(upload, "name", None))
    extension, media_type = media_type_for_name(original_name)
    limit = max_bytes if max_bytes is not None else settings.HARNESSTASK["max_upload_bytes"]

    staging_path = staging_directory() / f"{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(staging_path, "wb") as sink:
            for chunk in _iter_chunks(upload):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise FileTooLargeError(
                        f"文件超过大小限制 {limit} 字节",
                        details={"max_upload_bytes": limit},
                    )
                digest.update(chunk)
                sink.write(chunk)
    except BaseException:
        _safe_unlink(staging_path)
        raise

    if size == 0:
        _safe_unlink(staging_path)
        raise InvalidArgumentError("上传内容为空", details={"field": "file"})

    try:
        verify_staged_content(staging_path, extension=extension)
    except BaseException:
        _safe_unlink(staging_path)
        raise

    return StagedUpload(
        original_name=original_name,
        extension=extension,
        media_type=media_type,
        size=size,
        sha256=digest.hexdigest(),
        storage_key=f"{uuid.uuid4().hex}{extension}",
        staging_path=staging_path,
    )


def verify_staged_content(path: Path, *, extension: str) -> None:
    """Content sniffing for an already staged file; raises 415 for a renamed/markup file."""
    with open(path, "rb") as handle:
        head = handle.read(1024)

    lowered_head = head.lower()
    if any(lowered_head.startswith(signature) for signature in EXECUTABLE_SIGNATURES):
        raise UnsupportedMediaTypeError("检测到可执行内容，拒绝上传")

    expected = MAGIC_SIGNATURES.get(extension)
    if expected is not None:
        if not any(head.startswith(signature) for signature in expected):
            raise UnsupportedMediaTypeError(
                "文件内容与扩展名不符，拒绝上传",
                details={"extension": extension},
            )
        if any(marker in lowered_head for marker in MARKUP_SIGNATURES):
            raise UnsupportedMediaTypeError("检测到 HTML/脚本内容，拒绝上传")
        return

    if extension in TEXT_EXTENSIONS:
        _verify_text_content(path)
        return

    raise UnsupportedMediaTypeError("不支持的文件类型", details={"extension": extension})


def _verify_text_content(path: Path) -> None:
    """Text documents must be UTF-8 and must not carry markup or script payloads."""
    decoder = _Utf8Stream()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            if b"\x00" in chunk:
                raise UnsupportedMediaTypeError("检测到二进制内容，拒绝作为文本文件上传")
            lowered = chunk.lower()
            if any(marker in lowered for marker in MARKUP_SIGNATURES):
                raise UnsupportedMediaTypeError("检测到 HTML/SVG/脚本内容，拒绝上传")
            if not decoder.feed(chunk):
                raise UnsupportedMediaTypeError("文本文件必须是 UTF-8 编码")
    if not decoder.finish():
        raise UnsupportedMediaTypeError("文本文件必须是 UTF-8 编码")


class _Utf8Stream:
    """Incremental UTF-8 validator that never buffers the whole document."""

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> bool:
        buffer = bytes(self._pending) + chunk
        try:
            buffer.decode("utf-8")
            self._pending = bytearray()
            return True
        except UnicodeDecodeError as exc:
            if exc.end == len(buffer) and (len(buffer) - exc.start) <= 4:
                # Possibly a multi-byte character split across chunks.
                self._pending = bytearray(buffer[exc.start :])
                return True
            return False

    def finish(self) -> bool:
        try:
            bytes(self._pending).decode("utf-8")
        except UnicodeDecodeError:
            return False
        self._pending = bytearray()
        return True


def persist_staging_asset(
    actor: ActorContext, staged: StagedUpload, *, project: Project | None
) -> FileAsset:
    """Commit the staging metadata and move the blob into the private object directory.

    This is its own short transaction: the row must survive a later failed bind so the
    unbound staging file stays discoverable for cleanup (DEVELOP D09).

    With OSS enabled the object is also mirrored to the private bucket inside this
    step: a file reported as uploaded must already have its OSS copy (and therefore a
    signable ``oss_url``), so an OSS failure fails the whole upload and leaves nothing
    behind — no local object and no row.
    """
    destination = object_path(staged.storage_key)

    def service() -> FileAsset:
        os.replace(staged.staging_path, destination)
        try:
            oss_uploaded_at = None
            if oss_storage.is_enabled():
                oss_storage.upload_local_file(
                    staged.storage_key, destination, media_type=staged.media_type
                )
                oss_uploaded_at = db_now()
            return FileAsset.objects.create(
                storage_key=staged.storage_key,
                original_name=staged.original_name,
                media_type=staged.media_type,
                size=staged.size,
                sha256=staged.sha256,
                uploader_type=actor.actor_type.value,
                uploader_agent_id=actor.agent_id if actor.actor_type is ActorType.AGENT else None,
                project=project,
                bound_at=None,
                oss_uploaded_at=oss_uploaded_at,
            )
        except BaseException:
            if oss_storage.is_enabled():
                oss_storage.delete_object(staged.storage_key)
            _safe_unlink(destination)
            raise

    return run_request_service(service)


# --------------------------------------------------------------------------------------
# Public upload entry points
# --------------------------------------------------------------------------------------


def upload_project_document(
    actor: ActorContext,
    project_id,
    *,
    upload,
    name: str | None = None,
    allow_duplicate_name: bool = False,
    idem_key: str,
) -> dict[str, Any]:
    """Upload a new project document (Document + DocumentVersion 1)."""
    payload = {
        "purpose": PURPOSE_PROJECT_DOCUMENT,
        "project_id": str(project_id),
        "name": name,
        "allow_duplicate_name": bool(allow_duplicate_name),
    }
    return upload_and_bind(actor, payload, upload, idem_key)


def add_document_version(
    actor: ActorContext, document_id, *, upload, idem_key: str
) -> dict[str, Any]:
    """Upload the next version of an existing document (number + 1, never overwritten)."""
    payload = {"purpose": PURPOSE_PROJECT_DOCUMENT, "document_id": str(document_id)}
    return upload_and_bind(actor, payload, upload, idem_key)


def attach_to_task(
    actor: ActorContext,
    task_id,
    *,
    upload,
    purpose: str,
    execution_id=None,
    idem_key: str,
) -> dict[str, Any]:
    """Upload a task reference or an execution artifact (project comes from the task)."""
    payload = {
        "purpose": purpose,
        "task_id": str(task_id),
        "execution_id": None if execution_id is None else str(execution_id),
    }
    return upload_and_bind(actor, payload, upload, idem_key)


def upload_and_bind(actor: ActorContext, payload: dict, temp_file, idem_key: str) -> dict[str, Any]:
    """Stage, validate, persist and bind one upload (CONTRACT.md §5 entry point)."""
    _require_idem_key(idem_key)
    spec = resolve_upload_spec(actor, payload)
    staged = stage_upload(temp_file)
    try:
        asset = persist_staging_asset(actor, staged, project=spec.project)
    except BaseException:
        staged.discard()
        raise
    return _bind_asset(actor, spec, asset, idem_key)


def upload_staging_file(
    actor: ActorContext,
    *,
    upload,
    project_id=None,
    idem_key: str,
) -> dict[str, Any]:
    """Upload one unbound staging file and return its view (including ``oss_url``).

    Entry point for "attach files while authoring a task": the bytes are streamed,
    validated and persisted exactly like any other upload (mirrored to OSS when
    enabled), but **no** attachment link or document version is created — the asset is
    bound later by the task-creation service via ``attachment_file_ids``. Unbound
    staging material is reclaimed by the T27 cleanup job after the retention window.

    Agents may only use this as supervisors (the same role that may author tasks);
    the Web operator is the trusted local administrator.
    """
    _require_idem_key(idem_key)
    agent = _load_actor_agent(actor)
    if agent is not None and agent.role != AgentRole.SUPERVISOR.value:
        raise RoleForbiddenError("只有主管可以暂存上传文件")

    project = None
    if project_id is not None and str(project_id).strip():
        project = _load_project(project_id)
        require_project_editable(project)

    staged = stage_upload(upload)
    try:
        asset = persist_staging_asset(actor, staged, project=project)
    except BaseException:
        staged.discard()
        raise

    surface = "agent" if actor.actor_type is ActorType.AGENT else "web"
    operation = f"POST /api/{surface}/v1/files"
    request_hash = canonical_request_hash(
        operation=operation,
        body={
            "original_name": staged.original_name,
            "size": staged.size,
            "sha256": staged.sha256,
            "project_id": None if project is None else str(project.pk),
        },
        resource_id="staging",
        project_id=None if project is None else str(project.pk),
    )

    def service() -> dict[str, Any]:
        receipt = lookup_or_reserve(
            actor_scope=actor.actor_scope(),
            operation=operation,
            key=idem_key,
            request_hash=request_hash,
            resource_type="file",
        )
        if receipt.replayed:
            # Same key, same content: drop the duplicate asset and replay the response.
            delete_staging_asset(asset)
            return {**(receipt.response_data or {}), "idempotent_replay": True}
        data = file_asset_view(asset)
        data["file_id"] = str(asset.pk)
        data["oss_url"] = oss_storage.asset_url(asset)
        complete_receipt(receipt, response_data=data, resource_type="file", resource_id=asset.pk)
        return data

    return run_request_service(service)


def bind_staged_asset(
    actor: ActorContext, payload: dict, staged_asset, idem_key: str
) -> dict[str, Any]:
    """Bind an already-staged upload.

    Upload and bind are deliberately separate phases: a bind that fails (reclaimed
    execution, deadline passed, limit reached) leaves ``bound_at`` NULL and creates no
    attachment, and the caller may retry the bind without re-uploading the bytes.
    """
    _require_idem_key(idem_key)
    asset = FileAsset.objects.filter(pk=getattr(staged_asset, "pk", None)).first()
    if asset is None:
        raise ResourceNotFoundError("上传文件不存在")
    spec = resolve_upload_spec(actor, payload)
    return _bind_asset(actor, spec, asset, idem_key)


def archive_document(actor: ActorContext, document_id, *, idem_key: str) -> dict[str, Any]:
    """Move a document out of the current reference list (``is_archived=True``).

    Nothing is deleted: the ``Document``/``DocumentVersion``/``FileAsset`` rows and the
    blobs stay readable through older context versions and formal submissions.
    """
    _require_idem_key(idem_key)
    document = _load_document(document_id)
    project_id = document.project_id
    operation = _operation_for_archive(actor)
    request_hash = canonical_request_hash(
        operation=operation,
        body={},
        resource_id=str(document.pk),
        project_id=str(project_id),
    )

    def service() -> dict[str, Any]:
        project = Project.objects.select_for_update().filter(pk=project_id).first()
        if project is None:
            raise ResourceNotFoundError("项目不存在或当前身份无权访问")
        require_project_editable(project)
        agent = load_actor_agent_locked(actor) if actor.actor_type is ActorType.AGENT else None
        _require_document_writer(agent, project)

        locked = (
            Document.objects.select_for_update().filter(pk=document.pk, project=project).first()
        )
        if locked is None:
            raise ResourceNotFoundError("文档不存在或不属于该项目")

        receipt = lookup_or_reserve(
            actor_scope=actor.actor_scope(),
            operation=operation,
            key=idem_key,
            request_hash=request_hash,
            resource_type="document",
        )
        if receipt.replayed:
            return {**(receipt.response_data or {}), "idempotent_replay": True}

        now = db_now()
        if locked.is_archived:
            data = _document_item(locked)
            data["already_archived"] = True
            complete_receipt(
                receipt, response_data=data, resource_type="document", resource_id=locked.pk
            )
            return data

        updated = Document.objects.filter(pk=locked.pk, is_archived=False).update(
            is_archived=True, updated_at=now
        )
        if updated != 1:
            raise VersionConflictError(
                "文档状态已变化，无法移出参考列表", details={"document_id": str(locked.pk)}
            )
        locked.is_archived = True
        locked.updated_at = now

        previous_content_version = project.content_version
        context = record_context_version_locked(project, now=now)
        record_activity(
            actor,
            "document.archive",
            project_id=project.pk,
            before={"document_id": str(locked.pk), "is_archived": False},
            after={
                "document_id": str(locked.pk),
                "is_archived": True,
                "content_version": project.content_version,
            },
        )
        enqueue_outbox(
            "document.archived",
            locked.pk,
            {
                "project_id": str(project.pk),
                "document_id": str(locked.pk),
                "content_version": project.content_version,
                "context_version_id": str(context.pk),
                "previous_content_version": previous_content_version,
            },
        )

        data = _document_item(locked)
        data["current_context_version"] = context_version_view(context)
        complete_receipt(
            receipt, response_data=data, resource_type="document", resource_id=locked.pk
        )
        return data

    return run_request_service(service)


def resolve_upload_spec(actor: ActorContext, payload: dict) -> UploadSpec:
    """Validate the requested target and run the pre-upload authorisation checks.

    Runs before a single byte is streamed: a rejected purpose, project, role, limit,
    execution ownership or already-expired deadline must not cost a 20 MiB upload.
    """
    purpose = str(payload.get("purpose") or "").strip().upper()
    if purpose not in UPLOAD_PURPOSES:
        raise InvalidArgumentError(
            "purpose 只接受 PROJECT_DOCUMENT、TASK_REFERENCE 或 EXECUTION_ARTIFACT",
            details={"field": "purpose", "value": payload.get("purpose")},
        )

    agent = _load_actor_agent(actor)

    if purpose == PURPOSE_PROJECT_DOCUMENT:
        document = None
        if payload.get("document_id"):
            document = _load_document(payload["document_id"])
            project = document.project
        else:
            project = _load_project(payload.get("project_id"))
        require_project_editable(project)
        _require_document_writer(agent, project)
        if document is None:
            _check_project_document_limit(project)
        return UploadSpec(
            purpose=purpose,
            project=project,
            document=document,
            display_name=_optional_name(payload.get("name")),
            allow_duplicate_name=bool(payload.get("allow_duplicate_name")),
        )

    task = _load_task(payload.get("task_id"))
    project = task.project
    require_project_editable(project)
    if task.is_archived:
        raise ResourceNotFoundError("任务已归档，不接受新的附件")

    if purpose == AttachmentPurpose.TASK_REFERENCE.value:
        _require_document_writer(agent, project)
        _check_task_reference_limit(task)
        return UploadSpec(purpose=purpose, project=project, task=task)

    execution_id = payload.get("execution_id")
    if not execution_id:
        raise InvalidArgumentError(
            "EXECUTION_ARTIFACT 必须携带 execution_id", details={"field": "execution_id"}
        )
    execution = TaskExecution.objects.filter(pk=_parse_uuid(execution_id), task=task).first()
    if execution is None:
        raise ResourceNotFoundError("执行不存在或不属于该任务")
    if agent is not None and execution.agent_id != agent.pk:
        raise ResourceNotFoundError("执行不存在或当前身份无权访问")
    _require_live_execution(task, execution)
    return UploadSpec(purpose=purpose, project=project, task=task, execution=execution)


# --------------------------------------------------------------------------------------
# Binding transaction
# --------------------------------------------------------------------------------------


def _bind_asset(
    actor: ActorContext, spec: UploadSpec, asset: FileAsset, idem_key: str
) -> dict[str, Any]:
    operation = _operation_for(spec, actor)
    resource_id = spec.document.pk if spec.document is not None else spec.project.pk
    if spec.task is not None:
        resource_id = spec.task.pk
    request_hash = canonical_request_hash(
        operation=operation,
        body={
            "purpose": spec.purpose,
            "project_id": str(spec.project.pk),
            "document_id": None if spec.document is None else str(spec.document.pk),
            "task_id": None if spec.task is None else str(spec.task.pk),
            "execution_id": None if spec.execution is None else str(spec.execution.pk),
            "name": spec.display_name,
            "allow_duplicate_name": spec.allow_duplicate_name,
            "size": asset.size,
            "sha256": asset.sha256,
            "original_name": asset.original_name,
        },
        resource_id=str(resource_id),
        project_id=str(spec.project.pk),
    )

    def service() -> dict[str, Any]:
        return _bind_locked(actor, spec, asset, idem_key, operation, request_hash)

    return run_request_service(service)


def _bind_locked(
    actor: ActorContext,
    spec: UploadSpec,
    asset: FileAsset,
    idem_key: str,
    operation: str,
    request_hash: str,
) -> dict[str, Any]:
    # Lock order: project -> agent -> task -> execution.
    project = Project.objects.select_for_update().filter(pk=spec.project.pk).first()
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    if asset.project_id is not None and asset.project_id != project.pk:
        raise ResourceNotFoundError("上传文件不属于该项目")
    require_project_editable(project)

    agent: Agent | None = None
    if actor.actor_type is ActorType.AGENT:
        agent = load_actor_agent_locked(actor)

    task: Task | None = None
    document: Document | None = None
    execution: TaskExecution | None = None

    if spec.purpose == PURPOSE_PROJECT_DOCUMENT:
        _require_document_writer(agent, project)
        if spec.document is not None:
            document = (
                Document.objects.select_for_update()
                .filter(pk=spec.document.pk, project=project)
                .first()
            )
            if document is None:
                raise ResourceNotFoundError("文档不存在或不属于该项目")
            if document.is_archived:
                raise ResourceNotFoundError("文档已移出当前参考列表，不能追加版本")
    else:
        task = (
            Task.objects.select_for_update().filter(pk=spec.task.pk, project=project).first()
        )
        if task is None:
            raise ResourceNotFoundError("任务不存在或不属于该项目")
        if task.is_archived:
            raise ResourceNotFoundError("任务已归档，不接受新的附件")
        if spec.purpose == AttachmentPurpose.TASK_REFERENCE.value:
            _require_document_writer(agent, project)
        else:
            execution = (
                TaskExecution.objects.select_for_update()
                .filter(pk=spec.execution.pk, task=task)
                .first()
            )
            if execution is None:
                raise ResourceNotFoundError("执行不存在或不属于该任务")
            if agent is not None and execution.agent_id != agent.pk:
                raise ResourceNotFoundError("执行不存在或当前身份无权访问")
            # Authoritative deadline/ownership/current-execution guard under the lock.
            current_execution_write_guard(
                actor,
                task=task,
                execution_id=execution.pk,
                now=db_now(),
                allow_web_operator=True,
            )

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="file",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    now = db_now()
    bound = FileAsset.objects.filter(pk=asset.pk, bound_at__isnull=True).update(bound_at=now)
    if bound != 1:
        raise VersionConflictError(
            "该上传文件已被绑定，不能重复绑定", details={"file_id": str(asset.pk)}
        )
    asset.bound_at = now

    if spec.purpose == PURPOSE_PROJECT_DOCUMENT:
        data = _bind_project_document(actor, project, document, asset, spec, now=now)
    else:
        data = _bind_task_attachment(actor, project, task, execution, asset, spec, now=now)

    complete_receipt(receipt, response_data=data, resource_type="file", resource_id=asset.pk)
    return data


def _bind_project_document(
    actor: ActorContext,
    project: Project,
    document: Document | None,
    asset: FileAsset,
    spec: UploadSpec,
    *,
    now: datetime,
) -> dict[str, Any]:
    previous_content_version = project.content_version

    if document is None:
        _check_project_document_limit(project)
        name = spec.display_name or asset.original_name
        if not spec.allow_duplicate_name and Document.objects.filter(
            project=project, is_archived=False, name__iexact=name
        ).exists():
            raise DocumentConflictError(
                "同名文档已存在：请显式选择上传新版本或新建同名文档",
                details={"name": name, "project_id": str(project.pk)},
            )
        document = Document.objects.create(project=project, name=name)
        version = _create_document_version(actor, document, asset, number=1)
        Document.objects.filter(pk=document.pk).update(current_version=version, updated_at=now)
        document.current_version = version
        action = "document.create"
        event_type = "document.created"
    else:
        version = _create_document_version(actor, document, asset)
        Document.objects.filter(pk=document.pk).update(current_version=version, updated_at=now)
        document.current_version = version
        action = "document.version.create"
        event_type = "document.version_created"

    context = record_context_version_locked(project, now=now)
    record_activity(
        actor,
        action,
        project_id=project.pk,
        before={"content_version": previous_content_version, "document_version": version.number - 1},
        after={
            "content_version": project.content_version,
            "document_id": str(document.pk),
            "document_version": version.number,
            "size": asset.size,
            "media_type": asset.media_type,
            "sha256": asset.sha256,
        },
    )
    enqueue_outbox(
        event_type,
        document.pk,
        {
            "project_id": str(project.pk),
            "document_id": str(document.pk),
            "document_version_id": str(version.pk),
            "number": version.number,
            "content_version": project.content_version,
        },
    )
    data = document_view(document)
    data["current_version"] = document_version_view(version)
    data["current_context_version"] = context_version_view(context)
    return data


def _bind_task_attachment(
    actor: ActorContext,
    project: Project,
    task: Task,
    execution: TaskExecution | None,
    asset: FileAsset,
    spec: UploadSpec,
    *,
    now: datetime,
) -> dict[str, Any]:
    if spec.purpose == AttachmentPurpose.TASK_REFERENCE.value:
        _check_task_reference_limit(task)

    link = AttachmentLink.objects.create(
        file_asset=asset,
        project=project,
        task=task,
        execution=execution,
        submission=None,
        purpose=spec.purpose,
    )
    record_activity(
        actor,
        "attachment.upload",
        project_id=project.pk,
        task_id=task.pk,
        execution_id=None if execution is None else execution.pk,
        before={},
        after={
            "purpose": spec.purpose,
            "file_id": str(asset.pk),
            "original_name": asset.original_name,
            "size": asset.size,
            "media_type": asset.media_type,
            "sha256": asset.sha256,
        },
    )
    enqueue_outbox(
        "attachment.bound",
        link.pk,
        {
            "project_id": str(project.pk),
            "task_id": str(task.pk),
            "execution_id": None if execution is None else str(execution.pk),
            "file_id": str(asset.pk),
            "purpose": spec.purpose,
        },
    )
    logger.info(
        "附件已绑定：task=%s purpose=%s file=%s", task.pk, spec.purpose, asset.pk
    )
    return attachment_view(link)


# --------------------------------------------------------------------------------------
# Submission artifacts (T20 binds these; the reference is frozen, never replaced)
# --------------------------------------------------------------------------------------


def execution_artifact_link(asset: FileAsset, execution: TaskExecution) -> AttachmentLink | None:
    """The ``EXECUTION_ARTIFACT`` link that proves an upload came from this execution."""
    return AttachmentLink.objects.filter(
        file_asset=asset,
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        execution=execution,
    ).first()


def bind_submission_artifact_locked(
    asset: FileAsset,
    *,
    submission: TaskSubmission,
    execution: TaskExecution | None = None,
    now: datetime | None = None,
) -> AttachmentLink:
    """Attach a frozen file to a formal submission (called by the submission service).

    Must run inside the submission's transaction with the execution/submission already
    locked. Append-only: an existing submission keeps its links, so a frozen reference can
    never be replaced by a later upload.
    """
    assert_in_transaction()
    now = now or db_now()
    limit = settings.HARNESSTASK["max_submission_artifacts"]
    current = AttachmentLink.objects.filter(
        submission=submission, purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value
    ).count()
    if current >= limit:
        raise AttachmentLimitExceededError(
            "提交产物数量超过限制",
            details={"limit": limit, "current": current, "submission_id": str(submission.pk)},
        )

    link = AttachmentLink.objects.create(
        file_asset=asset,
        project=submission.task.project,
        task=submission.task,
        execution=execution or submission.execution,
        submission=submission,
        purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value,
    )
    if asset.bound_at is None:
        FileAsset.objects.filter(pk=asset.pk, bound_at__isnull=True).update(bound_at=now)
        asset.bound_at = now
    return link


# --------------------------------------------------------------------------------------
# Reads (both APIs)
# --------------------------------------------------------------------------------------


def list_project_documents(
    actor: ActorContext, project_id, *, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    """Current reference documents of a project; every member may read them."""
    project = _load_project(project_id)
    agent = _load_actor_agent(actor)
    if agent is not None and not _is_project_member(agent, project.pk):
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")

    queryset = (
        Document.objects.filter(project=project, is_archived=False)
        .select_related("current_version", "current_version__file_asset")
        .order_by("created_at", "id")
    )
    total = queryset.count()
    offset = (page - 1) * page_size
    items = [_document_item(document) for document in queryset[offset : offset + page_size]]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_document_detail(actor: ActorContext, document_id) -> dict[str, Any]:
    """Document detail with the full, append-only version history."""
    document = _load_document(document_id)
    agent = _load_actor_agent(actor)
    if agent is not None and not _is_project_member(agent, document.project_id):
        raise ResourceNotFoundError("文档不存在或当前身份无权访问")

    versions = (
        DocumentVersion.objects.filter(document=document)
        .select_related("file_asset")
        .order_by("-number", "-created_at")
    )
    data = _document_item(document)
    data["versions"] = [document_version_view(version) for version in versions]
    return data


def list_task_attachments(
    actor: ActorContext, task_id, *, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    """Attachment links of a task that this caller may read."""
    task = _load_task(task_id)
    agent = _load_actor_agent(actor)
    if agent is not None:
        project_ids = accessible_project_ids(agent.pk)
        if not task_is_visible_to(agent, task, project_ids):
            raise ResourceNotFoundError("任务不存在或当前身份无权访问")

    queryset = AttachmentLink.objects.filter(task=task)
    if agent is not None and agent.role != AgentRole.SUPERVISOR.value:
        own_execution_artifact_ids = list(
            AttachmentLink.objects.filter(
                task=task,
                purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
                execution__agent=agent,
            ).values_list("pk", flat=True)
        )
        queryset = queryset.filter(
            Q(purpose__in=(AttachmentPurpose.TASK_REFERENCE.value, AttachmentPurpose.SUBMISSION_ARTIFACT.value))
            | Q(pk__in=own_execution_artifact_ids)
        )

    queryset = queryset.select_related("file_asset").order_by("created_at", "id")
    total = queryset.count()
    offset = (page - 1) * page_size
    items = [attachment_view(link) for link in queryset[offset : offset + page_size]]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def authorise_download(actor: ActorContext, file_id) -> DownloadTarget:
    """Authorise a download and resolve the private path (never returned to the client)."""
    asset = FileAsset.objects.filter(pk=_parse_uuid(file_id)).first()
    if asset is None:
        raise ResourceNotFoundError("文件不存在或当前身份无权访问")

    if actor.actor_type is not ActorType.WEB_OPERATOR:
        agent = _load_actor_agent(actor)
        if agent is None or not _asset_is_readable(asset, agent):
            raise ResourceNotFoundError("文件不存在或当前身份无权访问")

    path = resolve_storage_path(asset.storage_key)
    if not path.is_file():
        logger.error("文件记录缺少磁盘内容：file=%s key=%s", asset.pk, asset.storage_key)
        raise ResourceNotFoundError("文件内容不可用，请联系管理员")
    return DownloadTarget(
        file_id=asset.pk,
        path=path,
        original_name=sanitize_download_name(asset.original_name),
        media_type=asset.media_type or DEFAULT_MEDIA_TYPE,
        size=asset.size,
    )


def _asset_is_readable(asset: FileAsset, agent: Agent) -> bool:
    versions = list(
        DocumentVersion.objects.filter(file_asset=asset).select_related("document")
    )
    for version in versions:
        if _is_project_member(agent, version.document.project_id):
            return True

    links = list(
        AttachmentLink.objects.filter(file_asset=asset)
        .select_related("task", "execution")
    )
    for link in links:
        if _link_is_readable(link, agent):
            return True

    if not versions and not links:
        # Staging material: only the uploading Key may read its own unbound upload.
        return asset.uploader_agent_id is not None and asset.uploader_agent_id == agent.pk
    return False


def _link_is_readable(link: AttachmentLink, agent: Agent) -> bool:
    if not _is_project_member(agent, link.project_id):
        return False
    if link.purpose == AttachmentPurpose.TASK_REFERENCE.value:
        return _task_is_readable(link.task, agent)
    if link.purpose == AttachmentPurpose.EXECUTION_ARTIFACT.value:
        if link.execution is None:
            return False
        if link.execution.agent_id == agent.pk:
            return True
        return agent.role == AgentRole.SUPERVISOR.value
    if link.purpose == AttachmentPurpose.SUBMISSION_ARTIFACT.value:
        task = link.task or (None if link.submission_id is None else link.submission.task)
        return task is not None and _task_is_readable(task, agent)
    return False


def _task_is_readable(task: Task | None, agent: Agent) -> bool:
    if task is None:
        return False
    return task_is_visible_to(agent, task, accessible_project_ids(agent.pk))


# --------------------------------------------------------------------------------------
# Cleanup helpers (used by the T27 jobs; never delete referenced evidence)
# --------------------------------------------------------------------------------------


def is_staging_asset(asset: FileAsset) -> bool:
    """True for material with no document version and no attachment link."""
    if asset.bound_at is not None:
        return False
    if DocumentVersion.objects.filter(file_asset=asset).exists():
        return False
    if AttachmentLink.objects.filter(file_asset=asset).exists():
        return False
    return True


def find_unreferenced_staging_assets(*, older_than: datetime, limit: int = 200) -> list[FileAsset]:
    """Staging assets older than a cutoff with no business reference at all."""
    candidates = list(
        FileAsset.objects.filter(bound_at__isnull=True, created_at__lt=older_than)
        .order_by("created_at", "id")[:limit]
    )
    return [asset for asset in candidates if is_staging_asset(asset)]


def delete_staging_asset(asset: FileAsset) -> bool:
    """Idempotently remove one unreferenced staging asset row and its private blob.

    Re-verifies every reference first, removes the blob before the row so a failed unlink
    stays retryable, and never touches history (DEVELOP D09). The OSS copy (if any) is
    removed best-effort: a stale OSS object without a row is inert and unsigned.
    """
    assert_in_transaction()
    if not is_staging_asset(asset):
        logger.info("跳过非暂存或无引用校验失败的文件：%s", asset.pk)
        return False
    _safe_unlink(resolve_storage_path(asset.storage_key))
    oss_storage.delete_object(asset.storage_key)
    FileAsset.objects.filter(pk=asset.pk, bound_at__isnull=True).delete()
    return True


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _create_document_version(
    actor: ActorContext, document: Document, asset: FileAsset, *, number: int | None = None
) -> DocumentVersion:
    if number is None:
        highest = (
            DocumentVersion.objects.filter(document=document).aggregate(Max("number"))[
                "number__max"
            ]
            or 0
        )
        number = highest + 1
    return DocumentVersion.objects.create(
        document=document,
        number=number,
        file_asset=asset,
        uploaded_by_type=actor.actor_type.value,
        uploaded_by_agent_id=actor.agent_id if actor.actor_type is ActorType.AGENT else None,
    )


def _document_item(document: Document) -> dict[str, Any]:
    data = document_view(document)
    data["current_version"] = (
        None if document.current_version is None else document_version_view(document.current_version)
    )
    return data


def _operation_for(spec: UploadSpec, actor: ActorContext) -> str:
    surface = "agent" if actor.actor_type is ActorType.AGENT else "web"
    if spec.purpose == PURPOSE_PROJECT_DOCUMENT:
        if spec.document is not None:
            return f"POST /api/{surface}/v1/documents/{{id}}/versions"
        return f"POST /api/{surface}/v1/projects/{{id}}/documents"
    return f"POST /api/{surface}/v1/tasks/{{id}}/attachments"


def _operation_for_archive(actor: ActorContext) -> str:
    surface = "agent" if actor.actor_type is ActorType.AGENT else "web"
    return f"DELETE /api/{surface}/v1/documents/{{id}}"


def _load_actor_agent(actor: ActorContext) -> Agent | None:
    """Non-locking identity read for pre-checks and GET entry points."""
    if actor.actor_type is ActorType.WEB_OPERATOR:
        return None
    if actor.actor_type is not ActorType.AGENT or actor.agent_id is None:
        raise RoleForbiddenError("该操作需要本地管理员或 Agent 身份")
    agent = Agent.objects.filter(pk=actor.agent_id).first()
    if agent is None:
        raise ResourceNotFoundError("当前身份不可用")
    if not agent.enabled:
        raise AgentDisabledError()
    if actor.key_version is not None and actor.key_version != agent.key_version:
        raise AgentTokenInvalidError("认证后 Key 已被重置，拒绝旧请求继续写入")
    return agent


def _require_document_writer(agent: Agent | None, project: Project) -> None:
    """Project members may read documents; only supervisors (or the Web operator) write."""
    if agent is None:
        return
    if not _is_project_member(agent, project.pk):
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    if agent.role != AgentRole.SUPERVISOR.value:
        raise RoleForbiddenError("员工不能修改项目参考文档或任务参考附件")


def _is_project_member(agent: Agent, project_id) -> bool:
    return ProjectMember.objects.filter(agent=agent, project_id=project_id).exists()


def _require_live_execution(task: Task, execution: TaskExecution) -> None:
    """Upload-start deadline check (repeated authoritatively inside the bind transaction)."""
    if task.status != TaskStatus.IN_PROGRESS.value or task.active_execution_id != execution.pk:
        raise ExecutionNotCurrentError(
            "任务当前不是进行中，缺少可写入的当前执行",
            details={"task_status": task.status},
        )
    if execution.ended_at is not None:
        raise ExecutionNotCurrentError("当前执行已经结束")
    now = db_now()
    if now >= execution.execution_deadline_at:
        raise ExecutionTimeoutError(
            details={
                "execution_deadline_at": execution.execution_deadline_at.isoformat(),
                "now": now.isoformat(),
            }
        )


def _check_project_document_limit(project: Project) -> None:
    limit = settings.HARNESSTASK["max_project_documents"]
    current = Document.objects.filter(project=project, is_archived=False).count()
    if current >= limit:
        raise AttachmentLimitExceededError(
            "项目当前文档数量超过限制",
            details={"limit": limit, "current": current, "project_id": str(project.pk)},
        )


def _check_task_reference_limit(task: Task) -> None:
    limit = settings.HARNESSTASK["max_task_reference_attachments"]
    current = AttachmentLink.objects.filter(
        task=task, purpose=AttachmentPurpose.TASK_REFERENCE.value
    ).count()
    if current >= limit:
        raise AttachmentLimitExceededError(
            "任务参考附件数量超过限制",
            details={"limit": limit, "current": current, "task_id": str(task.pk)},
        )


def _load_project(project_id) -> Project:
    project = Project.objects.filter(pk=_parse_uuid(project_id)).first()
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    return project


def _load_document(document_id) -> Document:
    document = (
        Document.objects.filter(pk=_parse_uuid(document_id))
        .select_related("current_version", "current_version__file_asset")
        .first()
    )
    if document is None:
        raise ResourceNotFoundError("文档不存在或当前身份无权访问")
    return document


def _load_task(task_id) -> Task:
    task = Task.objects.filter(pk=_parse_uuid(task_id)).first()
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    return task


def _parse_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ResourceNotFoundError("资源不存在或当前身份无权访问") from exc


def _optional_name(raw: Any) -> str | None:
    if raw is None:
        return None
    name = sanitize_original_name(raw)
    return name


def _require_idem_key(idem_key: str) -> str:
    if not isinstance(idem_key, str) or not idem_key.strip():
        raise IdempotencyKeyRequiredError()
    return idem_key.strip()


def _iter_chunks(upload, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    chunks = getattr(upload, "chunks", None)
    if callable(chunks):
        yield from chunks(chunk_size)
        return
    reader = getattr(upload, "read", None)
    if callable(reader):
        while True:
            data = reader(chunk_size)
            if not data:
                return
            yield data
        return
    if isinstance(upload, (bytes, bytearray)):
        data = bytes(upload)
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]
        return
    raise InvalidArgumentError("无法读取上传内容", details={"field": "file"})


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_unlink(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - filesystem fault path
        logger.warning("删除暂存文件失败：%s（%s）", path, exc)


__all__ = [
    "ALLOWED_EXTENSIONS",
    "DEFAULT_MEDIA_TYPE",
    "DownloadTarget",
    "InMemoryUpload",
    "PURPOSE_PROJECT_DOCUMENT",
    "StagedUpload",
    "UploadSpec",
    "add_document_version",
    "archive_document",
    "attach_to_task",
    "authorise_download",
    "bind_staged_asset",
    "bind_submission_artifact_locked",
    "content_disposition_value",
    "current_document_version_refs",
    "delete_staging_asset",
    "execution_artifact_link",
    "find_unreferenced_staging_assets",
    "get_document_detail",
    "is_staging_asset",
    "list_project_documents",
    "list_task_attachments",
    "media_type_for_name",
    "object_path",
    "persist_staging_asset",
    "resolve_storage_path",
    "resolve_upload_spec",
    "sanitize_download_name",
    "sanitize_original_name",
    "stage_upload",
    "staging_directory",
    "storage_root",
    "upload_and_bind",
    "upload_project_document",
    "upload_staging_file",
    "verify_staged_content",
]
