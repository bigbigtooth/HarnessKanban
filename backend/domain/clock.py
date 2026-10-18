"""Database clock adapter (DEVELOP D05).

Business deadline decisions must use the database clock, never a process clock, and never
a timestamp cached before the locks were acquired. ``db_now`` is therefore called after
the relevant rows are locked and inside the surrounding transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from django.conf import settings
from django.db import connections

_override: ContextVar[datetime | None] = ContextVar("db_now_override", default=None)


def db_now(using: str = "default") -> datetime:
    """Current UTC time as reported by the database, timezone-aware, microsecond precision."""
    override = _override.get()
    if override is not None:
        return override

    connection = connections[using]
    vendor = connection.vendor
    with connection.cursor() as cursor:
        if vendor == "mysql":
            cursor.execute("SELECT UTC_TIMESTAMP(6)")
            raw = cursor.fetchone()[0]
        elif vendor == "sqlite":
            cursor.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")
            raw = cursor.fetchone()[0]
        else:  # pragma: no cover - unsupported backend guard
            raise RuntimeError(f"不支持的数据库后端：{vendor}")

    if isinstance(raw, str):
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f")
    else:
        parsed = raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@contextmanager
def override_db_clock(value: datetime):
    """Test-only clock injection for exact boundary cases.

    Guarded so a production deployment cannot be talked into using a client-supplied
    ``now``: the override only activates outside production.
    """
    if settings.HARNESSTASK["env_name"] == "production":
        raise RuntimeError("生产环境禁止覆盖数据库时钟")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    token = _override.set(value.astimezone(timezone.utc))
    try:
        yield
    finally:
        _override.reset(token)
