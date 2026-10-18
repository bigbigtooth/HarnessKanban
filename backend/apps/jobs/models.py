"""Persistent idempotency receipts and the transactional outbox.

Both records are written in the same transaction as the business change they describe, so
a committed business fact always has its receipt and its event (DEVELOP D07/D18).
"""

from __future__ import annotations

import uuid

from django.db import models

from domain.contracts import DeliveryState, IdempotencyState

DELIVERY_STATE_CHOICES = [(state.value, state.value) for state in DeliveryState]
IDEMPOTENCY_STATE_CHOICES = [(state.value, state.value) for state in IdempotencyState]


class IdempotencyRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_scope = models.CharField(max_length=80)
    operation = models.CharField(max_length=160)
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    state = models.CharField(
        max_length=20, choices=IDEMPOTENCY_STATE_CHOICES, default=IdempotencyState.IN_PROGRESS.value
    )
    response_data = models.JSONField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(default=200)
    # Original resource reference so a replay can re-check read authorisation without
    # selecting a new candidate.
    resource_type = models.CharField(max_length=40, blank=True, default="")
    resource_id = models.UUIDField(null=True, blank=True)
    # Set only for sensitive receipts (key material) that must not be replayed verbatim.
    sensitive = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jobs_idempotency_record"
        constraints = [
            models.UniqueConstraint(
                fields=["actor_scope", "operation", "key"], name="uniq_idempotency_scope_op_key"
            ),
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="ix_idempotency_expires"),
            models.Index(fields=["resource_type", "resource_id"], name="ix_idempotency_resource"),
        ]


class OutboxEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=64)
    entity_id = models.UUIDField()
    payload = models.JSONField(default=dict, blank=True)
    delivery_state = models.CharField(
        max_length=20, choices=DELIVERY_STATE_CHOICES, default=DeliveryState.PENDING.value
    )
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    # Dispatch lease: prevents two dispatchers grabbing the same event concurrently.
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.CharField(max_length=120, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jobs_outbox_event"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(delivery_state__in=[state.value for state in DeliveryState]),
                name="ck_outbox_delivery_state_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["delivery_state", "next_retry_at"], name="ix_outbox_state_retry"),
            models.Index(
                fields=["delivery_state", "published_at", "processed_at"],
                name="ix_outbox_dispatch_scan",
            ),
            models.Index(fields=["entity_id", "event_type"], name="ix_outbox_entity"),
        ]
        ordering = ["created_at", "id"]
