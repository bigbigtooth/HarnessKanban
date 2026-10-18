"""Agent identity, key material and capacity slots (product 4, 11).

Only the SHA-256 digest of an Agent key is persisted. Plaintext keys are produced by the
domain layer and returned exactly once in the create/rotate response.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import GreaterThanOrEqual, LessThanOrEqual

from domain.contracts import AgentRole

ROLE_CHOICES = [(role.value, role.value) for role in AgentRole]


class Agent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    description = models.TextField(blank=True, default="")
    capabilities = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)

    # 软删除：从列表、候选与统计中消失，Key 立即失效（enabled 同时置 False），
    # 但执行、提交与审核历史一行都不删（product 11.1「保护历史」）；可由管理员恢复。
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_reason = models.TextField(blank=True, default="")

    key_hash = models.CharField(max_length=64, unique=True)
    key_version = models.PositiveIntegerField(default=1)

    max_concurrency = models.PositiveIntegerField(default=1)
    reserved_execution_count = models.PositiveIntegerField(default=0)

    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    runtime_info = models.JSONField(default=dict, blank=True)

    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_agent"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=[role.value for role in AgentRole]),
                name="ck_agent_role_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(max_concurrency__gte=1),
                name="ck_agent_max_concurrency_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_execution_count__gte=0),
                name="ck_agent_reserved_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_execution_count__lte=models.F("max_concurrency")),
                name="ck_agent_reserved_within_limit",
            ),
            models.CheckConstraint(
                condition=(
                    LessThanOrEqual(Length("name"), 100) & GreaterThanOrEqual(Length("name"), 1)
                ),
                name="ck_agent_name_length",
            ),
        ]
        indexes = [
            models.Index(fields=["enabled", "role"], name="ix_agent_enabled_role"),
            models.Index(fields=["deleted_at"], name="ix_agent_deleted_at"),
            models.Index(fields=["last_heartbeat_at"], name="ix_agent_heartbeat"),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return f"{self.name}({self.role})"

    @property
    def available_capacity(self) -> int:
        return max(self.max_concurrency - self.reserved_execution_count, 0)
