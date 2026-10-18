"""Outermost transaction boundary, retry policy and the SQLite development write lock.

Rules implemented here (DEVELOP D05):

* A synchronous domain write service is the outermost transaction entry point. The HTTP
  layer calls ``run_write_service`` once; services never commit on their own and never
  open a second transaction.
* MySQL runs at READ COMMITTED with row locks. SQLite has no usable ``SELECT FOR UPDATE``,
  so any read-then-write service must really ``UPDATE`` the singleton lock row before its
  first business ``SELECT``.
* Lock-plan invalidation, database busy and deadlocks roll back the *whole* transaction and
  retry the service a bounded number of times; after that a 503 with evidence is returned
  rather than a lock-free fallback.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from django.conf import settings
from django.db import OperationalError, connections, transaction
from django.db.utils import DatabaseError

from domain.contracts import ACTION_LOCK_TIMEOUT, ACTION_RETRY_EXHAUSTED
from domain.errors import (
    ClaimLockMissingError,
    DatabaseBusyError,
    RetryTransaction,
)

logger = logging.getLogger("domain.transactions")

T = TypeVar("T")

CLAIM_LOCK_ID = 1

_SQLITE_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)
_MYSQL_DEADLOCK_CODE = 1213
_MYSQL_LOCK_WAIT_TIMEOUT_CODE = 1205
_RETRY_BACKOFF_BASE_SECONDS = 0.01
_RETRY_BACKOFF_MAX_SECONDS = 0.25


def is_sqlite(using: str = "default") -> bool:
    return connections[using].vendor == "sqlite"


def is_retryable_db_error(exc: BaseException) -> bool:
    """True when rolling back and retrying the whole transaction may succeed.

    Deadlocks are retried in-process (DEVELOP D05: "死锁 -> 回滚后有限次数重试整个服务").
    A lock-wait timeout is NOT retried here: D05 requires returning 503 so the client
    retries with its original idempotency key. Retrying internally would silently multiply
    the configured ``CLAIM_LOCK_WAIT_SECONDS`` by the retry count and turn a bounded wait
    into a much longer one.
    """
    if isinstance(exc, RetryTransaction):
        return True
    if not isinstance(exc, (OperationalError, DatabaseError)):
        return False
    message = str(exc).lower()
    if any(marker in message for marker in _SQLITE_BUSY_MARKERS):
        # SQLite has no row locks: a busy/busy-snapshot error means the write transaction
        # lost the race, and the documented recovery is to roll back and retry in a new
        # transaction.
        return True
    return _mysql_error_code(exc) == _MYSQL_DEADLOCK_CODE


def is_lock_wait_timeout(exc: BaseException) -> bool:
    return _mysql_error_code(exc) == _MYSQL_LOCK_WAIT_TIMEOUT_CODE


def _mysql_error_code(exc: BaseException) -> int | None:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    cause = exc.__cause__
    if cause is not None:
        args = getattr(cause, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
    return None


def acquire_claim_lock_row(using: str = "default") -> None:
    """Take the global claim lock by really updating the singleton row.

    Must be the first statement of the outermost transaction, before any business read.
    A missing or non-singleton row is a configuration/data fault and must never degrade
    into lock-free execution.
    """
    connection = connections[using]
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE tasks_claim_lock SET revision = revision + 1 WHERE id = %s",
            [CLAIM_LOCK_ID],
        )
        if cursor.rowcount != 1:
            logger.error(
                "全局领取锁行不可用：影响行数=%s，拒绝无锁执行", cursor.rowcount
            )
            raise ClaimLockMissingError(
                "全局领取锁行缺失或不可用，拒绝无锁执行",
                details={"affected_rows": cursor.rowcount},
            )


def assert_in_transaction(using: str = "default") -> None:
    if not connections[using].in_atomic_block:
        raise RuntimeError(
            "领域写服务必须在最外层事务内调用（run_write_service），不得自行提交事务"
        )


def run_write_service(
    callback: Callable[[], T],
    *,
    global_claim_lock: bool = False,
    require_write_lock: bool = True,
    max_attempts: int | None = None,
    using: str = "default",
) -> T:
    """Run ``callback`` inside the outermost durable transaction with bounded retries.

    ``global_claim_lock`` takes the singleton claim lock (always on MySQL claims, and
    implicitly on SQLite for every read-then-write service because that is the only
    available write lock). A missing singleton is inserted once *before* that
    transaction is retried. The business transaction itself still starts with
    ``UPDATE`` and still refuses to run when that update does not lock exactly one row.
    """
    attempts = max_attempts
    if attempts is None:
        attempts = settings.HARNESSTASK["database_retry_attempts"]
    attempts = max(attempts, 0)

    sqlite = is_sqlite(using)
    take_lock = global_claim_lock or (sqlite and require_write_lock)

    def run_attempts() -> T:
        last_error: BaseException | None = None
        for attempt in range(attempts + 1):
            try:
                with transaction.atomic(using=using):
                    if take_lock:
                        acquire_claim_lock_row(using=using)
                    return callback()
            except BaseException as exc:  # noqa: BLE001 - re-raised below when not retryable
                if is_lock_wait_timeout(exc):
                    # Bounded wait is already spent; surface a retryable 503 for the client
                    # instead of spending the bound again in-process.
                    _record_failure_best_effort(
                        ACTION_LOCK_TIMEOUT, exc, claim_lock=take_lock, exhausted=False
                    )
                    raise _exhausted_error(exc, claim_lock=take_lock) from exc
                if not is_retryable_db_error(exc):
                    raise
                last_error = exc
                if attempt >= attempts:
                    break
                logger.warning(
                    "写事务第 %s 次重试（可重试数据库错误）：%s", attempt + 1, exc
                )
                _sleep_backoff(attempt)

        logger.error("写事务在 %s 次尝试后仍失败：%s", attempts + 1, last_error)
        _record_failure_best_effort(
            ACTION_RETRY_EXHAUSTED, last_error, claim_lock=take_lock, exhausted=True
        )
        raise _exhausted_error(last_error, claim_lock=take_lock)

    try:
        return run_attempts()
    except ClaimLockMissingError:
        if not take_lock:
            raise
        # The failed attempt already rolled back. Insert the singleton in its own
        # transaction, then take it inside a fresh business transaction. This does
        # not reset an existing revision and does not continue without the lock.
        logger.warning("全局领取锁行缺失，补回 id=1 后重试本次写事务")
        from apps.tasks.claim_lock import ensure_claim_lock_row

        ensure_claim_lock_row(using=using)
        return run_attempts()


def _exhausted_error(last_error: BaseException | None, *, claim_lock: bool) -> DatabaseBusyError:
    if last_error is None:  # pragma: no cover - defensive
        return DatabaseBusyError()
    message = str(last_error)
    code = _mysql_error_code(last_error)
    details = {"reason": message}
    if code is not None:
        details["driver_code"] = code
    if code == _MYSQL_LOCK_WAIT_TIMEOUT_CODE:
        from domain.errors import ClaimLockTimeoutError

        if claim_lock:
            return ClaimLockTimeoutError(
                "等待全局领取锁超时，请按原幂等键重试", details=details
            )
        return ClaimLockTimeoutError(
            "等待数据库行锁超时，请按原幂等键重试", details=details
        )
    return DatabaseBusyError("数据库繁忙，请按原幂等键稍后重试", details=details)


def _sleep_backoff(attempt: int) -> None:
    delay = min(_RETRY_BACKOFF_BASE_SECONDS * (2**attempt), _RETRY_BACKOFF_MAX_SECONDS)
    delay += random.uniform(0, delay / 2)
    time.sleep(delay)


def _record_failure_best_effort(
    action: str, exc: BaseException | None, *, claim_lock: bool, exhausted: bool
) -> None:
    """Persist a queryable failure record in a new transaction.

    Called only after the failing transaction has rolled back. Best effort by design: the
    record must never replace the original error, and it must never be the reason a
    committed business fact looks like a failure.
    """
    if exc is None:  # pragma: no cover - defensive
        return
    # Imported lazily: ops_service imports this module's siblings.
    from domain.ops_service import record_system_failure

    details = {"driver_code": _mysql_error_code(exc)}
    if not exhausted:
        details["stage"] = "lock_wait_timeout"
    else:
        details["stage"] = "retry_exhausted"
    details["claim_lock"] = claim_lock
    record_system_failure(
        action,
        reason=str(exc),
        details=details,
    )
