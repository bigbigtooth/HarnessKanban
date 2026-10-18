"""Temporary file cleanup (DEVELOP D09, product 16).

Only staging material is removable: a ``FileAsset`` that never gained a document version or
an attachment link and is older than the retention window. Historical delivery evidence is
never deleted by age.

The database reference is authoritative. The row is removed inside the transaction with a
``bound_at IS NULL`` guard, so a bind that commits concurrently wins the race and the file
survives; the disk file is removed only after that transaction commits, because an external
unlink cannot be rolled back.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.files.models import AttachmentLink, FileAsset
from apps.projects.models import DocumentVersion
from domain.clock import db_now
from domain.contracts import ActorType
from domain.events import record_activity
from domain.contracts import ActorContext

logger = logging.getLogger("domain.file_cleanup")


def cleanup_unreferenced_temp_files(*, limit: int = 200, now=None) -> dict:
    """Delete aged, unreferenced staging files. Returns a per-run summary."""
    now = now or db_now()
    cutoff = now - timedelta(hours=settings.HARNESSTASK["temp_file_retention_hours"])

    candidates = list(
        FileAsset.objects.filter(bound_at__isnull=True, created_at__lt=cutoff)
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:limit]
    )

    removed_keys: list[str] = []
    marked_bound = 0
    for asset_id in candidates:
        asset = FileAsset.objects.select_for_update().filter(pk=asset_id).first()
        if asset is None or asset.bound_at is not None:
            continue
        if _is_referenced(asset):
            # Referenced but never flagged: close the bookkeeping gap instead of deleting
            # evidence that a document version or attachment already points at.
            FileAsset.objects.filter(pk=asset.pk, bound_at__isnull=True).update(bound_at=now)
            marked_bound += 1
            continue

        deleted, _ = FileAsset.objects.filter(pk=asset.pk, bound_at__isnull=True).delete()
        if deleted:
            removed_keys.append(asset.storage_key)

    if removed_keys:
        # External deletion is idempotent and must not run inside the rollback scope.
        transaction.on_commit(lambda keys=tuple(removed_keys): _unlink_files(keys))

    return {
        "scanned": len(candidates),
        "deleted": len(removed_keys),
        "marked_bound": marked_bound,
    }


def _is_referenced(asset: FileAsset) -> bool:
    if DocumentVersion.objects.filter(file_asset=asset).exists():
        return True
    if AttachmentLink.objects.filter(file_asset=asset).exists():
        return True
    return _referenced_by_context_snapshot(asset)


def _referenced_by_context_snapshot(asset: FileAsset) -> bool:
    """A context version freezes document version ids; check the file behind them."""
    from apps.projects.models import ProjectContextVersion

    version_ids = list(DocumentVersion.objects.filter(file_asset=asset).values_list("id", flat=True))
    if not version_ids:
        return False
    wanted = {str(version_id) for version_id in version_ids}
    for refs in ProjectContextVersion.objects.values_list("document_version_refs", flat=True).iterator():
        for ref in refs or []:
            if isinstance(ref, dict) and str(ref.get("document_version_id")) in wanted:
                return True
    return False


def _unlink_files(storage_keys: tuple[str, ...]) -> None:
    for key in storage_keys:
        for path in _candidate_paths(key):
            try:
                path.unlink()
            except FileNotFoundError:
                # Deleting an already-absent file is a successful cleanup.
                continue
            except OSError:
                logger.exception("删除临时文件失败，保留待下次清理：%s", key)


def _candidate_paths(storage_key: str) -> list[Path]:
    """Resolve an unbound asset in either the staging or the object directory."""
    root = Path(settings.FILE_STORAGE_ROOT)
    paths = []
    for directory_name in _directory_names():
        candidate = root / directory_name / storage_key
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            logger.error("拒绝删除存储根目录之外的路径：%s", candidate)
            continue
        paths.append(candidate)
    return paths


def _directory_names() -> tuple[str, ...]:
    names: list[str] = []
    try:
        from domain.file_service import object_directory, staging_directory

        names = [staging_directory().name, object_directory().name]
    except Exception:  # pragma: no cover - fall back to scanning the root itself
        logger.warning("无法解析文件服务目录，改为在存储根目录内查找")
    if not names:
        names = [""]
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def sweep_orphan_staging_files(*, limit: int = 200) -> dict:
    """Remove staged files on disk that never got a database row at all (bounded).

    A crashed upload can leave a file behind before the metadata row exists. Those files
    cannot be referenced by anything, so an aged-file sweep is safe.
    """
    removed = 0
    for directory_name in _directory_names():
        directory = Path(settings.FILE_STORAGE_ROOT) / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir())[:limit]:
            if not path.is_file():
                continue
            if FileAsset.objects.filter(storage_key=path.name).exists():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.exception("删除孤立暂存文件失败：%s", path)
    return {"orphans_removed": removed}


def system_actor(request_id: str) -> ActorContext:
    return ActorContext(actor_type=ActorType.SYSTEM, request_id=request_id)


def record_cleanup_event(count: int) -> None:  # pragma: no cover - used by jobs
    record_activity(system_actor("temp-file-cleanup"), "file.cleanup", reason=f"清理 {count} 个临时文件")
