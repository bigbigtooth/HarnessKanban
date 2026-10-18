"""Restore the singleton claim-lock row when a flush or a wiped table removes it.

The row is inserted by the data migration. ``manage.py flush`` truncates every
application table and does not re-run data migrations, so ``post_migrate`` (also
emitted by flush) puts id=1 back. ``run_write_service`` calls
``ensure_claim_lock_row`` only after a lock attempt has already rolled back,
then starts a new transaction whose first statement is still the lock ``UPDATE``.

An existing revision is never reset. If the follow-up update still does not
lock exactly one row, the write is refused.
"""

from __future__ import annotations

from django.db import router

from apps.tasks.models import TaskClaimLock

CLAIM_LOCK_ID = 1


def ensure_claim_lock_row(*, using: str = "default") -> None:
    """Insert ``TaskClaimLock.id=1`` when it is absent. Leave an existing revision alone."""
    if not router.allow_migrate_model(using, TaskClaimLock):
        return
    TaskClaimLock.objects.using(using).get_or_create(
        id=CLAIM_LOCK_ID, defaults={"revision": 0}
    )


def restore_claim_lock_after_migrate(sender, **kwargs) -> None:
    ensure_claim_lock_row(using=kwargs.get("using") or "default")
