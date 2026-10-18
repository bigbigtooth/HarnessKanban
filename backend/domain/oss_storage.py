"""Aliyun OSS object storage access (upload, signed GET URLs, delete).

The bucket stays **private**: nothing is ever published with a public ACL. External
access happens exclusively through presigned GET URLs whose lifetime is bounded by
``OSS_URL_TTL_SECONDS`` (capped at Aliyun's 7-day maximum for long-lived AccessKeys).
That keeps the existing permission model intact — a URL proves possession of the link,
and every business read still goes through :func:`domain.file_service.authorise_download`.

Design notes:

* The client is created lazily on first use and cached per process; startup never
  performs network I/O, so an unreachable OSS cannot break process boot or migrations.
* Every oss2 failure is wrapped in :class:`domain.errors.OssUnavailableError` so both
  HTTP adapters serialise it as ``503 OSS_UNAVAILABLE`` and callers retry with the
  **same** Idempotency-Key.
* ``oss2`` is imported inside the accessor: with ``OSS_ENABLED=false`` the dependency
  is never even touched, matching the offline default of the suite.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from django.conf import settings

from domain.errors import OssUnavailableError

logger = logging.getLogger("domain.oss_storage")

# 阿里云对长期 AccessKey 签名 URL 的硬上限（7 天），与 env 校验保持一致。
MAX_URL_TTL_SECONDS = 604800

_client_lock = threading.Lock()
_bucket_client = None


def is_enabled() -> bool:
    """True when uploads should be mirrored to OSS and URLs can be signed."""
    return bool(settings.OSS.get("enabled"))


def _bucket():
    """Return the process-wide ``oss2.Bucket`` client, building it once."""
    global _bucket_client
    if _bucket_client is not None:
        return _bucket_client
    with _client_lock:
        if _bucket_client is None:
            config = settings.OSS
            try:
                import oss2
            except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
                raise OssUnavailableError("服务端缺少 oss2 依赖，无法访问对象存储") from exc
            auth = oss2.Auth(config["access_key_id"], config["access_key_secret"])
            _bucket_client = oss2.Bucket(auth, config["endpoint"], config["bucket"])
    return _bucket_client


def reset_client() -> None:
    """Drop the cached client (used by tests that flip settings between cases)."""
    global _bucket_client
    with _client_lock:
        _bucket_client = None


def upload_local_file(storage_key: str, path: Path, *, media_type: str | None = None) -> None:
    """Push one already-validated local object into OSS under its storage key.

    Raises :class:`OssUnavailableError` on any failure so the caller can fail the
    whole upload request — with OSS enabled, a file without its OSS copy must never
    be reported as uploaded.
    """
    if not is_enabled():
        return
    headers = {"x-oss-object-acl": "private"}
    if media_type:
        headers["Content-Type"] = media_type
    try:
        bucket = _bucket()
        bucket.put_object_from_file(storage_key, str(path), headers=headers)
    except Exception as exc:
        logger.warning("OSS 上传失败：key=%s error=%s", storage_key, exc)
        raise OssUnavailableError(
            "文件上传对象存储失败，请按原幂等键重试",
            details={"storage_key": storage_key},
        ) from exc


def sign_get_url(storage_key: str, *, ttl_seconds: int | None = None) -> str | None:
    """Presigned GET URL for one storage key, or ``None`` when OSS is disabled."""
    if not is_enabled():
        return None
    config = settings.OSS
    ttl = config.get("url_ttl_seconds") if ttl_seconds is None else ttl_seconds
    ttl = max(60, min(int(ttl), MAX_URL_TTL_SECONDS))
    try:
        return _bucket().sign_url("GET", storage_key, ttl)
    except Exception as exc:
        logger.warning("OSS 签名失败：key=%s error=%s", storage_key, exc)
        raise OssUnavailableError(
            "对象存储签名失败，请稍后重试",
            details={"storage_key": storage_key},
        ) from exc


def delete_object(storage_key: str) -> None:
    """Best-effort OSS delete; local cleanup must never fail because of OSS."""
    if not is_enabled():
        return
    try:
        _bucket().delete_object(storage_key)
    except Exception as exc:  # pragma: no cover - network fault path
        logger.warning("OSS 删除失败（忽略，等待下次清理）：key=%s error=%s", storage_key, exc)


def asset_url(asset) -> str | None:
    """Signed OSS URL for a :class:`apps.files.models.FileAsset`, if it has one.

    URLs are signed on demand so every response carries a fresh, full-TTL link;
    the asset qualifies only when its OSS copy actually landed (``oss_uploaded_at``).
    """
    if not is_enabled() or asset is None or not getattr(asset, "oss_uploaded_at", None):
        return None
    return sign_get_url(asset.storage_key)


__all__ = [
    "MAX_URL_TTL_SECONDS",
    "asset_url",
    "delete_object",
    "is_enabled",
    "reset_client",
    "sign_get_url",
    "upload_local_file",
]
