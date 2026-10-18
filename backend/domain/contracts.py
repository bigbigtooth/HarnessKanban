"""Frozen implementation contract for HarnessKanban (T01).

This module is the code copy of ``docs/CONTRACT.md`` §2/§4. It must stay in sync with
that document; changing an enum value or error code is a contract change and is decided
by the main thread only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

CONTRACT_VERSION = "1.0"


class AgentRole(StrEnum):
    SUPERVISOR = "SUPERVISOR"
    WORKER = "WORKER"


class TaskStatus(StrEnum):
    PREPARING = "PREPARING"
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    DONE = "DONE"


class ClaimMode(StrEnum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class EndReason(StrEnum):
    RELEASE = "RELEASE"
    RECLAIM = "RECLAIM"
    TIMEOUT = "TIMEOUT"
    SUBMIT = "SUBMIT"


class ActorType(StrEnum):
    WEB_OPERATOR = "web_operator"
    AGENT = "agent"
    SYSTEM = "system"


class AttachmentPurpose(StrEnum):
    TASK_REFERENCE = "TASK_REFERENCE"
    EXECUTION_ARTIFACT = "EXECUTION_ARTIFACT"
    SUBMISSION_ARTIFACT = "SUBMISSION_ARTIFACT"


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    PROCESSED = "PROCESSED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class IdempotencyState(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class TaskAction(StrEnum):
    PUBLISH = "publish"
    WITHDRAW = "withdraw"
    CLAIM = "claim"
    SUBMIT = "submit"
    RELEASE = "release"
    RECLAIM = "reclaim"
    TIMEOUT = "timeout"
    APPROVE = "approve"
    REJECT = "reject"
    REOPEN = "reopen"
    ARCHIVE = "archive"
    RESTORE = "restore"
    ASSIGN = "assign"


DIFFICULTY_TIMEOUT_SECONDS: dict[int, int] = {1: 900, 2: 1800, 3: 7200, 4: 28800, 5: 86400}
DIFFICULTY_NAMES: dict[int, str] = {1: "L1", 2: "L2", 3: "L3", 4: "L4", 5: "L5"}
DIFFICULTY_LABELS: dict[int, str] = {
    1: "入门",
    2: "简单",
    3: "中等",
    4: "困难",
    5: "极难",
}
DIFFICULTY_LEVELS: tuple[int, ...] = (1, 2, 3, 4, 5)

# Route templates used for the idempotency operation scope (CONTRACT.md §8).
OP_AGENT_CREATE = "POST /api/web/v1/agents"
OP_AGENT_ROTATE_KEY = "POST /api/web/v1/agents/{id}/rotate-key"
OP_WEB_TASK_CREATE = "POST /api/web/v1/tasks"
OP_AGENT_TASK_CREATE = "POST /api/agent/v1/tasks"
OP_TASK_PUBLISH = "POST /api/{surface}/v1/tasks/{id}/publish"
OP_TASK_WITHDRAW = "POST /api/{surface}/v1/tasks/{id}/withdraw"
OP_TASK_ASSIGN = "POST /api/{surface}/v1/tasks/{id}/assign"
OP_CLAIM_AGENT = "POST /api/agent/v1/get_task/"
OP_CLAIM_WEB = "POST /api/web/v1/tasks/{id}/claim"
OP_TASK_PROGRESS = "POST /api/{surface}/v1/tasks/{id}/progress"
OP_TASK_RELEASE = "POST /api/{surface}/v1/tasks/{id}/release"
OP_TASK_RECLAIM = "POST /api/{surface}/v1/tasks/{id}/reclaim"
OP_TASK_SUBMIT = "POST /api/{surface}/v1/tasks/{id}/submit"
OP_TASK_REVIEW = "POST /api/{surface}/v1/tasks/{id}/review"
OP_TASK_REOPEN = "POST /api/{surface}/v1/tasks/{id}/reopen"
OP_TASK_ARCHIVE = "POST /api/{surface}/v1/tasks/{id}/archive"
OP_TASK_RESTORE = "POST /api/{surface}/v1/tasks/{id}/restore"

# T34 management surface (WebAPI only, local loopback).
OP_OPS_OUTBOX_RETRY = "POST /api/web/v1/ops/outbox/{event_id}/retry"
OP_OPS_CAPACITY_AUDIT = "POST /api/web/v1/ops/capacity-audit"

# Queryable system-failure record actions.
ACTION_RETRY_EXHAUSTED = "system.retry_exhausted"
ACTION_LOCK_TIMEOUT = "system.lock_timeout"
ACTION_CAPACITY_REPAIRED = "agent.capacity_repaired"
ACTION_OUTBOX_MANUAL_RETRY = "system.outbox_manual_retry"


@dataclass(frozen=True)
class ActorContext:
    """Request-scoped operation source.

    ``agent_id`` is only populated for authenticated AgentAPI requests. A web operator's
    target agent is a business object passed as a payload field, never an authenticated
    identity, and must not be written into ``agent_id``.
    """

    actor_type: ActorType
    request_id: str
    agent_id: UUID | None = None
    key_version: int | None = None
    web_operator_note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_agent(self) -> bool:
        return self.actor_type is ActorType.AGENT

    @property
    def is_web_operator(self) -> bool:
        return self.actor_type is ActorType.WEB_OPERATOR

    @property
    def is_system(self) -> bool:
        return self.actor_type is ActorType.SYSTEM

    def actor_scope(self) -> str:
        """Idempotency actor scope (CONTRACT.md §8)."""
        if self.actor_type is ActorType.AGENT and self.agent_id is not None:
            return f"agent:{self.agent_id}"
        if self.actor_type is ActorType.SYSTEM:
            return "system"
        return "web_operator"


def parse_level_filters(raw_items: list[tuple[str, str]]) -> list[int]:
    """Parse raw ``l1``..``l5`` query pairs into a sorted level list (CONTRACT.md §9).

    ``raw_items`` must be the untouched multi-value query pairs so duplicated keys and
    invalid level keys can be rejected instead of silently collapsed into a dict.

    Returns ``[]`` for an all-zero selection. Raises ``InvalidArgumentError`` for
    duplicated level keys, non ``0``/``1`` values and level keys outside ``l1``..``l5``.
    """
    # Imported lazily: contracts.py is imported by models/settings-adjacent code paths.
    from domain.errors import InvalidArgumentError

    level_values: dict[int, list[str]] = {}
    for key, value in raw_items:
        if len(key) == 2 and key[0] == "l" and key[1].isdigit():
            level = int(key[1])
            if level < 1 or level > 5:
                raise InvalidArgumentError(
                    f"不支持的等级参数 {key}，仅接受 l1 至 l5",
                    details={"parameter": key},
                )
            level_values.setdefault(level, []).append(value)

    if not level_values:
        return list(DIFFICULTY_LEVELS)

    for level, values in level_values.items():
        if len(values) > 1:
            raise InvalidArgumentError(
                f"等级参数 l{level} 重复出现",
                details={"parameter": f"l{level}"},
            )
        if values[0] not in ("0", "1"):
            raise InvalidArgumentError(
                f"等级参数 l{level} 的值必须为 0 或 1",
                details={"parameter": f"l{level}", "value": values[0]},
            )

    return sorted(level for level, values in level_values.items() if values[0] == "1")


def selected_level_labels(levels: list[int]) -> list[str]:
    return [DIFFICULTY_NAMES[level] for level in levels]


def display_number(number: int) -> str:
    return f"TASK-{number:04d}"


def route_operation(template: str, surface: str) -> str:
    """Render only the ``{surface}`` placeholder of a route template.

    ``{id}`` must stay a literal placeholder: the idempotency operation scope is
    ``"<METHOD> <route template>"``, and concrete resource ids belong in the request hash.
    """
    return template.replace("{surface}", surface)
