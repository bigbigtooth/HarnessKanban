"""Create the singleton global claim lock row.

The lock row must exist before any claim can happen. This migration inserts it.
``flush`` truncates the row and does not re-run data migrations; ``post_migrate``
(see ``apps.tasks.claim_lock``) restores it. API processes must not create the
row on each request (DEVELOP D04/D05).
"""

from __future__ import annotations

from django.db import migrations

CLAIM_LOCK_ID = 1


def create_claim_lock(apps, schema_editor):
    TaskClaimLock = apps.get_model("tasks", "TaskClaimLock")
    TaskClaimLock.objects.update_or_create(id=CLAIM_LOCK_ID, defaults={"revision": 0})
    if TaskClaimLock.objects.filter(id=CLAIM_LOCK_ID).count() != 1:
        raise RuntimeError(
            "全局领取锁行必须恰有一条（id=1），迁移中止，不能以无锁模式继续"
        )


def remove_claim_lock(apps, schema_editor):
    TaskClaimLock = apps.get_model("tasks", "TaskClaimLock")
    TaskClaimLock.objects.filter(id=CLAIM_LOCK_ID).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_claim_lock, remove_claim_lock),
    ]
