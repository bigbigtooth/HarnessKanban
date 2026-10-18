"""Celery application: dispatch/consumption is an accelerator, the database is the fact source.

One Beat instance owns the schedule; any number of Workers may consume the same queue
because every job is idempotent (DEVELOP D18).
"""

from __future__ import annotations

import os
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

app = Celery("harvesstask", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

_task_settings = settings.HARNESSTASK

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    task_default_queue="harvesstask",
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    task_time_limit=600,
    task_soft_time_limit=540,
    beat_schedule={
        "timeout-scan": {
            "task": "jobs.tasks.timeout_scan",
            "schedule": timedelta(seconds=_task_settings["timeout_scan_interval_seconds"]),
        },
        "outbox-dispatch": {
            "task": "jobs.tasks.outbox_dispatch",
            "schedule": timedelta(seconds=_task_settings["outbox_scan_interval_seconds"]),
        },
        "temp-file-cleanup": {
            "task": "jobs.tasks.cleanup_temp_files",
            "schedule": crontab(hour=3, minute=0),
        },
        "idempotency-cleanup": {
            "task": "jobs.tasks.cleanup_idempotency_records",
            "schedule": crontab(hour=3, minute=30),
        },
        "capacity-audit": {
            "task": "jobs.tasks.capacity_audit",
            "schedule": crontab(hour=4, minute=0),
        },
    },
)

app.autodiscover_tasks(["jobs"])
