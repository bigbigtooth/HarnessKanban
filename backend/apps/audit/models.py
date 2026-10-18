"""Append-only activity history. Never stores raw keys or full sensitive payloads."""

from __future__ import annotations

import uuid

from django.db import models


class ActivityEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.PROTECT, null=True, blank=True, related_name="activity_events"
    )
    task = models.ForeignKey(
        "tasks.Task", on_delete=models.PROTECT, null=True, blank=True, related_name="activity_events"
    )
    execution = models.ForeignKey(
        "tasks.TaskExecution", on_delete=models.PROTECT, null=True, blank=True, related_name="activity_events"
    )
    submission = models.ForeignKey(
        "tasks.TaskSubmission", on_delete=models.PROTECT, null=True, blank=True, related_name="activity_events"
    )
    actor_type = models.CharField(max_length=20)
    actor_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="activity_events"
    )
    action = models.CharField(max_length=64)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_activity_event"
        indexes = [
            models.Index(fields=["project", "created_at"], name="ix_activity_project_time"),
            models.Index(fields=["task", "created_at"], name="ix_activity_task_time"),
            models.Index(fields=["action", "created_at"], name="ix_activity_action_time"),
        ]
        ordering = ["created_at", "id"]
