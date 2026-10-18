"""Agent identity services: create, read, base attributes, key lifecycle, enable/disable,
global role change and the AgentAPI self/heartbeat surface (T07 + T08).

Frozen rules implemented here:

* A key is at least 256 bits from ``domain.security.generate_agent_key``; only the SHA-256
  digest and ``key_version`` are persisted. The plaintext appears exactly once, in the
  create / rotate / enable-with-rotate response, and never in a receipt, activity event,
  outbox payload, log line or detail response.
* Create and rotate receipts are sensitive: the stored receipt keeps the agent id, the
  operation result and the key version plus ``key_delivery_unavailable=true``, so a replay
  of a lost response never mints a second key.
* ``max_concurrency`` is a positive int and may not drop below the agent's currently
  reserved slots; rotating a key never changes an execution deadline or a task state.
* Role and enable/disable are global (on ``Agent``) and never editable through the generic
  PATCH; disabling revokes the key immediately (401 AGENT_DISABLED) and shares the
  last-supervisor guard with the membership service.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Count, F, Q

from apps.identity.models import Agent
from apps.projects.models import ProjectMember
from apps.tasks.models import TaskExecution
from domain import membership_service
from domain.bulk import bulk_result, parse_bulk_ids, run_bulk
from domain.clock import db_now
from domain.contracts import (
    OP_AGENT_CREATE,
    OP_AGENT_ROTATE_KEY,
    ActorContext,
    ActorType,
    AgentRole,
    EndReason,
    ProjectStatus,
    display_number,
)
from domain.errors import (
    AgentNameConflictError,
    CapacityBelowReservedError,
    InvalidArgumentError,
    ResourceNotFoundError,
    RoleForbiddenError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan
from domain.permissions import load_actor_agent_locked
from domain.presenters import agent_summary, iso
from domain.security import generate_agent_key, hash_agent_key
from domain.transactions import assert_in_transaction

logger = logging.getLogger("domain.agent")

# Idempotency operation scopes (CONTRACT.md §8: "<METHOD> <route template>").
OP_AGENT_UPDATE = "PATCH /api/web/v1/agents/{id}"
OP_AGENT_ENABLE = "POST /api/web/v1/agents/{id}/enable"
OP_AGENT_DISABLE = "POST /api/web/v1/agents/{id}/disable"
OP_AGENT_ROLE_UPDATE = "POST /api/web/v1/agents/{id}/role"
OP_AGENT_DELETE = "POST /api/web/v1/agents/{id}/delete"
OP_AGENT_BULK_DELETE = "POST /api/web/v1/agents/bulk-delete"
OP_AGENT_BULK_RESTORE = "POST /api/web/v1/agents/bulk-restore"

NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 2000
CAPABILITY_MAX_ITEMS = 20
CAPABILITY_MAX_LENGTH = 50
REASON_MAX_LENGTH = 500
RUNTIME_MAX_KEYS = 30
RUNTIME_VALUE_MAX_LENGTH = 200

# Display-only connectivity state (product 4.3). Disabled wins over every other state.
ONLINE_NEVER_CONNECTED = "NEVER_CONNECTED"
ONLINE_IDLE = "ONLINE_IDLE"
ONLINE_WORKING = "ONLINE_WORKING"
ONLINE_OFFLINE = "OFFLINE"
ONLINE_DISABLED = "DISABLED"
ONLINE_STATES = (
    ONLINE_NEVER_CONNECTED,
    ONLINE_IDLE,
    ONLINE_WORKING,
    ONLINE_OFFLINE,
    ONLINE_DISABLED,
)
ONLINE_ACTIVE_STATES = (ONLINE_IDLE, ONLINE_WORKING)

ACTIVITY_AGENT_CREATE = "agent.create"
ACTIVITY_AGENT_UPDATE = "agent.update"
ACTIVITY_AGENT_KEY_ROTATE = "agent.key.rotate"
ACTIVITY_AGENT_ENABLE = "agent.enable"
ACTIVITY_AGENT_DISABLE = "agent.disable"
ACTIVITY_AGENT_ROLE_UPDATE = "agent.role.update"
ACTIVITY_AGENT_DELETE = "agent.delete"
ACTIVITY_AGENT_RESTORE = "agent.restore"

EVENT_AGENT_CREATED = "agent.created"
EVENT_AGENT_UPDATED = "agent.updated"
EVENT_AGENT_ENABLED = "agent.enabled"
EVENT_AGENT_DISABLED = "agent.disabled"
EVENT_AGENT_ROLE_UPDATED = "agent.role.updated"
EVENT_AGENT_DELETED = "agent.deleted"
EVENT_AGENT_RESTORED = "agent.restored"

PATCHABLE_FIELDS = ("name", "description", "capabilities", "max_concurrency")
# Fields only business actions may write (product 12.3, T07/T08 boundary).
PROTECTED_AGENT_FIELDS = {
    "role",
    "enabled",
    "key_hash",
    "key_version",
    "reserved_execution_count",
    "version",
}


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _require_web_operator(actor: ActorContext) -> None:
    if actor.actor_type is not ActorType.WEB_OPERATOR:
        raise RoleForbiddenError("Agent 管理只能由本地管理员完成")


def _parse_name(value: Any, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise InvalidArgumentError("缺少必需字段 name", details={"field": "name"})
        return None
    if not isinstance(value, str):
        raise InvalidArgumentError("name 必须是字符串", details={"field": "name"})
    name = value.strip()
    if len(name) < NAME_MIN_LENGTH:
        raise InvalidArgumentError("name 不能为空", details={"field": "name"})
    if len(name) > NAME_MAX_LENGTH:
        raise InvalidArgumentError(
            f"name 超过长度上限 {NAME_MAX_LENGTH}",
            details={"field": "name", "max_length": NAME_MAX_LENGTH},
        )
    return name


def _parse_role(value: Any) -> str:
    if value is None:
        raise InvalidArgumentError("缺少必需字段 role", details={"field": "role"})
    if not isinstance(value, str):
        raise InvalidArgumentError("role 必须是字符串", details={"field": "role"})
    role = value.strip().upper()
    if role not in {item.value for item in AgentRole}:
        raise InvalidArgumentError(
            "role 必须是 SUPERVISOR 或 WORKER",
            details={"field": "role", "allowed": [item.value for item in AgentRole]},
        )
    return role


def _parse_max_concurrency(value: Any, *, reserved: int, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise InvalidArgumentError(
                "缺少必需字段 max_concurrency", details={"field": "max_concurrency"}
            )
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError(
            "max_concurrency 必须是正整数", details={"field": "max_concurrency", "value": value}
        )
    if value < 1:
        raise InvalidArgumentError(
            "max_concurrency 必须大于等于 1",
            details={"field": "max_concurrency", "value": value},
        )
    if value < reserved:
        raise CapacityBelowReservedError(
            details={
                "max_concurrency": value,
                "reserved_execution_count": reserved,
            }
        )
    return value


def _parse_description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidArgumentError("description 必须是字符串", details={"field": "description"})
    description = value.strip()
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise InvalidArgumentError(
            f"description 超过长度上限 {DESCRIPTION_MAX_LENGTH}",
            details={"field": "description", "max_length": DESCRIPTION_MAX_LENGTH},
        )
    return description


def _parse_capabilities(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidArgumentError("capabilities 必须是字符串数组", details={"field": "capabilities"})
    if len(value) > CAPABILITY_MAX_ITEMS:
        raise InvalidArgumentError(
            f"capabilities 最多 {CAPABILITY_MAX_ITEMS} 项",
            details={"field": "capabilities", "max_items": CAPABILITY_MAX_ITEMS},
        )
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidArgumentError(
                "capabilities 必须是字符串数组", details={"field": "capabilities"}
            )
        text = item.strip()
        if not text:
            continue
        if len(text) > CAPABILITY_MAX_LENGTH:
            raise InvalidArgumentError(
                f"capabilities 单项超过长度上限 {CAPABILITY_MAX_LENGTH}",
                details={"field": "capabilities", "max_length": CAPABILITY_MAX_LENGTH},
            )
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_reason(payload: dict[str, Any]) -> str:
    value = payload.get("reason", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidArgumentError("reason 必须是字符串", details={"field": "reason"})
    reason = value.strip()
    if len(reason) > REASON_MAX_LENGTH:
        raise InvalidArgumentError(
            f"reason 超过长度上限 {REASON_MAX_LENGTH}",
            details={"field": "reason", "max_length": REASON_MAX_LENGTH},
        )
    return reason


def _parse_expected_version(payload: dict[str, Any]) -> int:
    value = payload.get("expected_version")
    if value is None:
        raise InvalidArgumentError(
            "缺少必需字段 expected_version", details={"field": "expected_version"}
        )
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidArgumentError(
            "expected_version 必须是正整数",
            details={"field": "expected_version", "value": value},
        )
    return value


def _parse_bool_field(payload: dict[str, Any], name: str) -> bool:
    value = payload.get(name, False)
    if isinstance(value, bool):
        return value
    raise InvalidArgumentError(f"{name} 必须是布尔值", details={"field": name, "value": value})


def _parse_runtime(value: Any) -> dict[str, Any]:
    """Bound the self-reported runtime info; it may never change role/authorisation."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidArgumentError("runtime 必须是对象", details={"field": "runtime"})
    if len(value) > RUNTIME_MAX_KEYS:
        raise InvalidArgumentError(
            f"runtime 最多 {RUNTIME_MAX_KEYS} 个字段",
            details={"field": "runtime", "max_keys": RUNTIME_MAX_KEYS},
        )
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise InvalidArgumentError("runtime 的键必须是非空字符串", details={"field": "runtime"})
        if isinstance(item, bool) or isinstance(item, (int, float)) or item is None:
            cleaned[key.strip()] = item
        elif isinstance(item, str):
            cleaned[key.strip()] = item[:RUNTIME_VALUE_MAX_LENGTH]
        else:
            raise InvalidArgumentError(
                "runtime 的值只能是字符串、数字或布尔值",
                details={"field": "runtime", "key": key},
            )
    return cleaned


# --------------------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------------------


def _offline_threshold_seconds() -> int:
    return int(settings.HARNESSTASK["offline_threshold_seconds"])


def online_state_for(agent: Agent, *, now=None, effective_execution_count: int | None = None) -> str:
    """Display state per product 4.3 (disabled wins; heartbeat decides connectivity)."""
    if not agent.enabled:
        return ONLINE_DISABLED
    if agent.last_heartbeat_at is None:
        return ONLINE_NEVER_CONNECTED
    reference = now or db_now()
    age = reference - agent.last_heartbeat_at
    if age > timedelta(seconds=_offline_threshold_seconds()):
        return ONLINE_OFFLINE
    if effective_execution_count is None:
        effective_execution_count = _effective_execution_count(agent.pk, now=reference)
    return ONLINE_WORKING if effective_execution_count > 0 else ONLINE_IDLE


def _effective_execution_count(agent_id, *, now) -> int:
    return (
        TaskExecution.objects.filter(
            agent_id=agent_id, ended_at__isnull=True, execution_deadline_at__gt=now
        ).count()
    )


def execution_summary(agent: Agent, *, now=None) -> dict[str, int]:
    """Execution counters for one agent, in a single aggregate query."""
    reference = now or db_now()
    aggregate = TaskExecution.objects.filter(agent_id=agent.pk).aggregate(
        open_count=Count("id", filter=Q(ended_at__isnull=True)),
        effective_count=Count(
            "id",
            filter=Q(ended_at__isnull=True, execution_deadline_at__gt=reference),
        ),
        completed_count=Count("id", filter=Q(end_reason=EndReason.SUBMIT.value)),
        timeout_count=Count("id", filter=Q(end_reason=EndReason.TIMEOUT.value)),
        rework_count=Count("id", filter=Q(sequence__gt=1)),
        total_count=Count("id"),
    )
    aggregate["pending_reclaim_count"] = aggregate["open_count"] - aggregate["effective_count"]
    return aggregate


def _current_executions(agent_id, *, now, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        TaskExecution.objects.filter(agent_id=agent_id, ended_at__isnull=True)
        .select_related("task")
        .order_by("execution_deadline_at", "id")[:limit]
    )
    return [_execution_brief(execution, now=now) for execution in rows]


def _execution_brief(execution: TaskExecution, *, now) -> dict[str, Any]:
    task = execution.task
    return {
        "execution_id": str(execution.pk),
        "task_id": str(execution.task_id),
        "project_id": str(task.project_id) if task is not None else None,
        "display_number": display_number(task.number) if task is not None else None,
        "difficulty": execution.difficulty_snapshot,
        "claimed_at": iso(execution.claimed_at),
        "execution_deadline_at": iso(execution.execution_deadline_at),
        "expired": execution.execution_deadline_at <= now,
    }


def _membership_rows(agent_id) -> list[ProjectMember]:
    return list(
        ProjectMember.objects.filter(agent_id=agent_id)
        .select_related("project")
        .order_by("created_at", "id")
    )


def agent_detail_payload(agent: Agent, *, now=None) -> dict[str, Any]:
    """Detail representation: summary + authorised projects + capacity + executions.

    Never contains the key, its digest, or any execution deadline change.
    """
    reference = now or db_now()
    summary = execution_summary(agent, now=reference)
    memberships = _membership_rows(agent.pk)
    data = agent_summary(
        agent,
        online_state=online_state_for(
            agent, now=reference, effective_execution_count=summary["effective_count"]
        ),
    )
    data.update(
        {
            "key_version": agent.key_version,
            "project_ids": [str(member.project_id) for member in memberships],
            "projects": [
                {
                    "id": str(member.project_id),
                    "title": member.project.title,
                    "status": member.project.status,
                }
                for member in memberships
            ],
            "capacity": {
                "max_concurrency": agent.max_concurrency,
                "reserved_execution_count": agent.reserved_execution_count,
                "available_capacity": agent.available_capacity,
            },
            "execution_summary": summary,
            "current_executions": _current_executions(agent.pk, now=reference),
        }
    )
    return data


def list_agents(actor: ActorContext, filters: dict[str, Any], page: int, page_size: int) -> dict:
    """Paged agent list with name / role / enabled / online / deleted filters."""
    queryset = Agent.objects.all()

    # 软删除的 Agent 默认从列表消失；`deleted=true` 只看已删除的（回收站视图）。
    deleted = filters.get("deleted")
    if deleted is None:
        queryset = queryset.filter(deleted_at__isnull=True)
    elif deleted:
        queryset = queryset.filter(deleted_at__isnull=False)
    else:
        queryset = queryset.filter(deleted_at__isnull=True)

    name = filters.get("name")
    if name:
        queryset = queryset.filter(name__icontains=name)
    role = filters.get("role")
    if role:
        if role not in {item.value for item in AgentRole}:
            raise InvalidArgumentError(
                "role 必须是 SUPERVISOR 或 WORKER",
                details={"parameter": "role", "allowed": [item.value for item in AgentRole]},
            )
        queryset = queryset.filter(role=role)
    enabled = filters.get("enabled")
    if enabled is not None:
        queryset = queryset.filter(enabled=enabled)
    online = filters.get("online")
    now = db_now()
    if online is not None:
        cutoff = now - timedelta(seconds=_offline_threshold_seconds())
        fresh_and_enabled = Q(enabled=True, last_heartbeat_at__gte=cutoff)
        if online:
            queryset = queryset.filter(fresh_and_enabled)
        else:
            queryset = queryset.exclude(fresh_and_enabled)

    total = queryset.count()
    offset = (page - 1) * page_size
    rows = list(queryset.order_by("created_at", "id")[offset : offset + page_size])
    agent_ids = [agent.pk for agent in rows]

    effective_counts = {
        row["agent_id"]: row["count"]
        for row in TaskExecution.objects.filter(
            agent_id__in=agent_ids, ended_at__isnull=True, execution_deadline_at__gt=now
        )
        .values("agent_id")
        .annotate(count=Count("id"))
    }
    project_map: dict[uuid.UUID, list[str]] = {agent_id: [] for agent_id in agent_ids}
    for agent_id, project_id in (
        ProjectMember.objects.filter(agent_id__in=agent_ids)
        .order_by("created_at")
        .values_list("agent_id", "project_id")
    ):
        project_map.setdefault(agent_id, []).append(str(project_id))

    items = []
    for agent in rows:
        item = agent_summary(
            agent,
            online_state=online_state_for(
                agent, now=now, effective_execution_count=effective_counts.get(agent.pk, 0)
            ),
        )
        item["project_ids"] = project_map.get(agent.pk, [])
        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_agent(actor: ActorContext, agent_id) -> dict:
    agent = Agent.objects.filter(pk=agent_id).first()
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")
    return agent_detail_payload(agent)


def agent_self(actor: ActorContext, agent: Agent) -> dict:
    """AgentAPI ``GET /me``: own role, capacity, authorisation and effective executions."""
    now = db_now()
    summary = execution_summary(agent, now=now)
    memberships = _membership_rows(agent.pk)
    current = _current_executions(agent.pk, now=now)
    return {
        "id": str(agent.pk),
        "name": agent.name,
        "role": agent.role,
        "enabled": agent.enabled,
        "capabilities": list(agent.capabilities or []),
        "key_version": agent.key_version,
        "version": agent.version,
        "capacity": {
            "max_concurrency": agent.max_concurrency,
            "reserved_execution_count": agent.reserved_execution_count,
            "available_capacity": agent.available_capacity,
        },
        "project_ids": [str(member.project_id) for member in memberships],
        "projects": [
            {
                "id": str(member.project_id),
                "title": member.project.title,
                "status": member.project.status,
            }
            for member in memberships
        ],
        "effective_executions": [item for item in current if not item["expired"]],
        "pending_reclaim_executions": [item for item in current if item["expired"]],
        "execution_summary": summary,
        "last_heartbeat_at": iso(agent.last_heartbeat_at),
        "online_state": online_state_for(
            agent, now=now, effective_execution_count=summary["effective_count"]
        ),
        "runtime_info": dict(agent.runtime_info or {}),
    }


# --------------------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------------------


def _replay_payload(receipt) -> dict:
    stored = receipt.response_data
    payload = dict(stored) if isinstance(stored, dict) else {"data": stored}
    payload["idempotent_replay"] = True
    return payload


def _agent_result(agent: Agent, *, raw_key: str | None = None, now=None) -> dict:
    data = agent_detail_payload(agent, now=now)
    if raw_key is not None:
        data["agent_key"] = raw_key
    return data


def bulk_delete_agents(
    actor: ActorContext, payload: dict[str, Any], idem_key: str | None
) -> dict:
    """批量软删除：逐项复用单条删除的全部校验，返回逐项结果。"""
    assert_in_transaction()
    _require_web_operator(actor)
    ids = parse_bulk_ids(payload, "agent_ids")
    reason = _parse_reason(payload)
    if not reason:
        raise InvalidArgumentError("批量删除必须填写原因", details={"field": "reason"})
    reclaim = payload.get("reclaim")
    if reclaim is not None and (
        not isinstance(reclaim, str) or reclaim.upper() not in membership_service.DISPOSITIONS
    ):
        raise InvalidArgumentError(
            "reclaim 必须是 WAIT 或 NOW",
            details={"field": "reclaim", "allowed": list(membership_service.DISPOSITIONS)},
        )
    body = {"reason": reason, "reclaim": reclaim}

    outcome = run_bulk(
        ids,
        lambda agent_id, _index, item_key: _change_agent_availability(
            actor,
            agent_id,
            body,
            item_key,
            operation=OP_AGENT_DELETE,
            activity_action=ACTIVITY_AGENT_DELETE,
            event_type=EVENT_AGENT_DELETED,
            deletes=True,
        ),
        key_prefix=idem_key,
    )
    return bulk_result(actor, "agent.bulk_delete", outcome)


def bulk_restore_agents(actor: ActorContext, payload: dict[str, Any], idem_key: str | None) -> dict:
    """批量恢复（启用 + 清空删除标记）：逐项复用启用服务的全部校验。"""
    assert_in_transaction()
    _require_web_operator(actor)
    ids = parse_bulk_ids(payload, "agent_ids")

    outcome = run_bulk(
        ids,
        lambda agent_id, _index, item_key: enable_agent(
            actor, agent_id, {"rotate_key": False}, item_key
        ),
        key_prefix=idem_key,
    )
    return bulk_result(actor, "agent.bulk_restore", outcome)



def _default_max_concurrency() -> int:
    return int(settings.HARNESSTASK["default_max_concurrency"])


def create_agent(actor: ActorContext, payload: dict[str, Any], idem_key: str | None) -> dict:
    """Create one agent and deliver its plaintext key exactly once."""
    assert_in_transaction()
    _require_web_operator(actor)

    name = _parse_name(payload.get("name"))
    role = _parse_role(payload.get("role"))
    reserved = 0
    max_concurrency = _parse_max_concurrency(
        payload.get("max_concurrency"), reserved=reserved, default=_default_max_concurrency()
    )
    description = _parse_description(payload.get("description"))
    capabilities = _parse_capabilities(payload.get("capabilities"))

    request_hash = canonical_request_hash(
        operation=OP_AGENT_CREATE,
        body={
            "name": name,
            "role": role,
            "max_concurrency": max_concurrency,
            "description": description,
            "capabilities": capabilities,
        },
    )

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_AGENT_CREATE,
        key=idem_key,
        request_hash=request_hash,
        sensitive=True,
        resource_type="agent",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    if Agent.objects.filter(name=name).exists():
        raise AgentNameConflictError(details={"name": name})

    raw_key = generate_agent_key()
    now = db_now()
    try:
        agent = Agent.objects.create(
            name=name,
            role=role,
            description=description,
            capabilities=capabilities,
            enabled=True,
            key_hash=hash_agent_key(raw_key),
            key_version=1,
            max_concurrency=max_concurrency,
            reserved_execution_count=0,
            updated_at=now,
        )
    except IntegrityError as exc:  # concurrent create with the same name
        raise AgentNameConflictError(details={"name": name}) from exc

    data = _agent_result(agent, raw_key=raw_key, now=now)
    # ``sensitive=True`` made the stored receipt drop ``agent_key`` and add
    # ``key_delivery_unavailable=true``; the plaintext never reaches the database.
    complete_receipt(
        receipt,
        response_data=data,
        http_status=201,
        resource_type="agent",
        resource_id=agent.pk,
    )
    record_activity(
        actor,
        ACTIVITY_AGENT_CREATE,
        after={
            "agent_id": str(agent.pk),
            "name": agent.name,
            "role": agent.role,
            "max_concurrency": agent.max_concurrency,
            "key_version": agent.key_version,
        },
    )
    enqueue_outbox(
        EVENT_AGENT_CREATED,
        agent.pk,
        {
            "agent_id": str(agent.pk),
            "name": agent.name,
            "role": agent.role,
            "max_concurrency": agent.max_concurrency,
            "key_version": agent.key_version,
            "actor_type": actor.actor_type.value,
        },
    )
    return data


def update_agent(
    actor: ActorContext, agent_id, payload: dict[str, Any], idem_key: str | None
) -> dict:
    """PATCH the base attributes only (name / description / capabilities / max_concurrency)."""
    assert_in_transaction()
    _require_web_operator(actor)

    expected_version = _parse_expected_version(payload)
    fields: dict[str, Any] = {}
    if "name" in payload:
        fields["name"] = _parse_name(payload.get("name"))
    if "description" in payload:
        fields["description"] = _parse_description(payload.get("description"))
    if "capabilities" in payload:
        fields["capabilities"] = _parse_capabilities(payload.get("capabilities"))
    if "max_concurrency" in payload:
        # Reserved-slot comparison happens under the agent lock below.
        fields["max_concurrency"] = _parse_max_concurrency(payload.get("max_concurrency"), reserved=0)

    if not fields:
        raise InvalidArgumentError(
            "没有可修改的字段",
            details={"allowed_fields": list(PATCHABLE_FIELDS)},
        )

    request_hash = canonical_request_hash(
        operation=OP_AGENT_UPDATE, body=payload, resource_id=agent_id
    )

    plan = LockPlan()
    plan.add_agent(agent_id)
    locked = acquire_plan(plan)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_AGENT_UPDATE,
        key=idem_key,
        request_hash=request_hash,
        resource_type="agent",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    if agent.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": agent.version}
        )
    if "max_concurrency" in fields and fields["max_concurrency"] < agent.reserved_execution_count:
        raise CapacityBelowReservedError(
            details={
                "max_concurrency": fields["max_concurrency"],
                "reserved_execution_count": agent.reserved_execution_count,
            }
        )
    if "name" in fields and fields["name"] != agent.name:
        if Agent.objects.filter(name=fields["name"]).exclude(pk=agent.pk).exists():
            raise AgentNameConflictError(details={"name": fields["name"]})

    before = {
        "name": agent.name,
        "description": agent.description,
        "capabilities": list(agent.capabilities or []),
        "max_concurrency": agent.max_concurrency,
        "version": agent.version,
    }
    now = db_now()
    updates = dict(fields)
    updates["version"] = F("version") + 1
    updates["updated_at"] = now
    try:
        updated = Agent.objects.filter(pk=agent.pk, version=expected_version).update(**updates)
    except IntegrityError as exc:
        raise AgentNameConflictError(details={"name": fields.get("name")}) from exc
    if updated != 1:  # pragma: no cover - the row is locked, so version cannot move
        raise VersionConflictError(details={"expected_version": expected_version})
    agent.refresh_from_db()

    data = _agent_result(agent, now=now)
    complete_receipt(
        receipt,
        response_data=data,
        http_status=200,
        resource_type="agent",
        resource_id=agent.pk,
    )
    record_activity(
        actor,
        ACTIVITY_AGENT_UPDATE,
        before=before,
        after={
            "name": agent.name,
            "description": agent.description,
            "capabilities": list(agent.capabilities or []),
            "max_concurrency": agent.max_concurrency,
            "version": agent.version,
        },
    )
    enqueue_outbox(
        EVENT_AGENT_UPDATED,
        agent.pk,
        {
            "agent_id": str(agent.pk),
            "changed_fields": sorted(fields),
            "version": agent.version,
            "actor_type": actor.actor_type.value,
        },
    )
    return data


def _rotate_key_locked(agent: Agent, *, now) -> str:
    """Replace the key material: new digest, key_version + 1, same Agent id.

    Touches no execution row, so no execution deadline can move.
    """
    raw_key = generate_agent_key()
    updated = (
        Agent.objects.filter(pk=agent.pk, key_version=agent.key_version, version=agent.version)
        .update(
            key_hash=hash_agent_key(raw_key),
            key_version=F("key_version") + 1,
            version=F("version") + 1,
            updated_at=now,
        )
    )
    if updated != 1:  # pragma: no cover - the row is locked
        raise VersionConflictError(details={"agent_id": str(agent.pk)})
    agent.refresh_from_db()
    return raw_key


def rotate_agent_key(actor: ActorContext, agent_id, idem_key: str | None) -> dict:
    """Rotate the agent key; the plaintext is delivered once and stored nowhere."""
    assert_in_transaction()
    _require_web_operator(actor)

    request_hash = canonical_request_hash(
        operation=OP_AGENT_ROTATE_KEY, resource_id=agent_id
    )

    plan = LockPlan()
    plan.add_agent(agent_id)
    locked = acquire_plan(plan)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_AGENT_ROTATE_KEY,
        key=idem_key,
        request_hash=request_hash,
        sensitive=True,
        resource_type="agent",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    now = db_now()
    previous_key_version = agent.key_version
    raw_key = _rotate_key_locked(agent, now=now)

    data = _agent_result(agent, raw_key=raw_key, now=now)
    complete_receipt(
        receipt,
        response_data=data,
        http_status=200,
        resource_type="agent",
        resource_id=agent.pk,
    )
    record_activity(
        actor,
        ACTIVITY_AGENT_KEY_ROTATE,
        before={"key_version": previous_key_version},
        after={"key_version": agent.key_version, "agent_id": str(agent.pk)},
    )
    enqueue_outbox(
        "agent.key.rotated",
        agent.pk,
        {
            "agent_id": str(agent.pk),
            "key_version": agent.key_version,
            "previous_key_version": previous_key_version,
            "actor_type": actor.actor_type.value,
        },
    )
    return data


def enable_agent(
    actor: ActorContext, agent_id, payload: dict[str, Any], idem_key: str | None
) -> dict:
    """Enable an agent (optionally rotating its key). Same identity, same authorisation."""
    assert_in_transaction()
    _require_web_operator(actor)

    rotate = _parse_bool_field(payload, "rotate_key")
    request_hash = canonical_request_hash(
        operation=OP_AGENT_ENABLE, body={"rotate_key": rotate}, resource_id=agent_id
    )

    plan = LockPlan()
    plan.add_agent(agent_id)
    locked = acquire_plan(plan)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_AGENT_ENABLE,
        key=idem_key,
        request_hash=request_hash,
        sensitive=True,
        resource_type="agent",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    now = db_now()
    raw_key: str | None = None
    previous_key_version = agent.key_version
    changed = False
    restored = agent.deleted_at is not None

    if not agent.enabled or restored:
        updates: dict[str, Any] = {
            "enabled": True,
            "version": F("version") + 1,
            "updated_at": now,
        }
        if restored:
            # 启用一个已删除的 Agent 等同于「恢复」：清空删除标记，历史一行不动。
            updates["deleted_at"] = None
            updates["deleted_reason"] = ""
        updated = Agent.objects.filter(pk=agent.pk, version=agent.version).update(**updates)
        if updated != 1:  # pragma: no cover - the row is locked
            raise VersionConflictError(details={"agent_id": str(agent.pk)})
        agent.refresh_from_db()
        changed = True
    if rotate:
        raw_key = _rotate_key_locked(agent, now=now)
        changed = True

    data = _agent_result(agent, raw_key=raw_key, now=now)
    complete_receipt(
        receipt,
        response_data=data,
        http_status=200,
        resource_type="agent",
        resource_id=agent.pk,
    )
    if changed:
        record_activity(
            actor,
            ACTIVITY_AGENT_RESTORE if restored else ACTIVITY_AGENT_ENABLE,
            reason="",
            before={"enabled": False, "deleted": restored, "key_version": previous_key_version},
            after={
                "agent_id": str(agent.pk),
                "enabled": True,
                "deleted": False,
                "key_version": agent.key_version,
                "rotated_key": rotate,
            },
        )
        enqueue_outbox(
            EVENT_AGENT_RESTORED if restored else EVENT_AGENT_ENABLED,
            agent.pk,
            {
                "agent_id": str(agent.pk),
                "key_version": agent.key_version,
                "rotated_key": rotate,
                "actor_type": actor.actor_type.value,
            },
        )
    return data


def _change_agent_availability(
    actor: ActorContext,
    agent_id,
    payload: dict[str, Any],
    idem_key: str | None,
    *,
    operation: str,
    activity_action: str,
    event_type: str,
    deletes: bool,
) -> dict:
    """禁用与软删除共用的实现。

    两者的前置校验完全相同（有效执行处置、最后一名主管保护、幂等回执），
    差别只在写入的字段与活动/事件名：删除额外落 ``deleted_at``/``deleted_reason``。
    """
    assert_in_transaction()
    _require_web_operator(actor)

    reclaim = payload.get("reclaim")
    if reclaim is not None:
        if not isinstance(reclaim, str) or reclaim.upper() not in membership_service.DISPOSITIONS:
            raise InvalidArgumentError(
                "reclaim 必须是 WAIT 或 NOW",
                details={"field": "reclaim", "allowed": list(membership_service.DISPOSITIONS)},
            )
        reclaim = reclaim.upper()
    reason = _parse_reason(payload)

    request_hash = canonical_request_hash(
        operation=operation,
        body={"reclaim": reclaim, "reason": reason},
        resource_id=agent_id,
    )

    plan, membership_ids, _scope = membership_service.build_revocation_plan(
        agent_id, execution_project_ids=None
    )
    locked = acquire_plan(plan)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")
    scope = membership_service.revalidate_revocation_scope(agent_id, plan, execution_project_ids=None)

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="agent",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    if scope and reclaim is None:
        raise InvalidArgumentError(
            "Agent 持有有效执行，必须明确选择保留等待(WAIT)或立即回收(NOW)",
            details={"agent_id": str(agent_id), "open_executions": len(scope)},
        )
    if reclaim == membership_service.DISPOSITION_NOW and not reason:
        raise InvalidArgumentError("立即回收必须填写原因", details={"field": "reason"})
    if deletes and not reason:
        raise InvalidArgumentError("删除必须填写原因", details={"field": "reason"})

    if agent.enabled and agent.role == AgentRole.SUPERVISOR.value:
        active_ids = {
            str(project_id)
            for project_id, project in locked.projects.items()
            if project.status == ProjectStatus.ACTIVE.value and project_id in membership_ids
        }
        membership_service.require_enabled_supervisors_preserved(
            active_ids, removed_agent_ids={agent.pk}
        )

    now = db_now()
    needs_disable = agent.enabled
    needs_delete = deletes and agent.deleted_at is None
    changed = needs_disable or needs_delete
    if changed:
        updates: dict[str, Any] = {
            "enabled": False,
            "version": F("version") + 1,
            "updated_at": now,
        }
        if deletes:
            updates["deleted_at"] = now
            updates["deleted_reason"] = reason
        updated = Agent.objects.filter(pk=agent.pk, version=agent.version).update(**updates)
        if updated != 1:  # pragma: no cover - the row is locked
            raise VersionConflictError(details={"agent_id": str(agent.pk)})
        agent.refresh_from_db()

    ended: list[dict[str, Any]] = []
    if reclaim == membership_service.DISPOSITION_NOW and scope:
        ended = membership_service.reclaim_executions(
            actor,
            executions=membership_service.locked_executions(locked, scope),
            tasks=locked.tasks,
            reason=reason,
        )

    disposition = reclaim or membership_service.DISPOSITION_WAIT
    data = _agent_result(agent, now=now)
    data["disposition"] = disposition
    data["reclaimed_execution_ids"] = [item["execution_id"] for item in ended]
    data["reason"] = reason
    complete_receipt(
        receipt,
        response_data=data,
        http_status=200,
        resource_type="agent",
        resource_id=agent.pk,
    )
    if changed:
        record_activity(
            actor,
            activity_action,
            reason=reason,
            before={"enabled": True, "deleted": False},
            after={
                "agent_id": str(agent.pk),
                "enabled": False,
                "deleted": agent.deleted_at is not None,
                "disposition": disposition,
                "reclaimed_execution_count": len(ended),
            },
        )
        enqueue_outbox(
            event_type,
            agent.pk,
            {
                "agent_id": str(agent.pk),
                "disposition": disposition,
                "reclaimed_execution_ids": [item["execution_id"] for item in ended],
                "actor_type": actor.actor_type.value,
                "reason": reason,
            },
        )
    return data


def disable_agent(
    actor: ActorContext, agent_id, payload: dict[str, Any], idem_key: str | None
) -> dict:
    """Disable an agent globally, applying the requested execution disposition."""
    return _change_agent_availability(
        actor,
        agent_id,
        payload,
        idem_key,
        operation=OP_AGENT_DISABLE,
        activity_action=ACTIVITY_AGENT_DISABLE,
        event_type=EVENT_AGENT_DISABLED,
        deletes=False,
    )


def delete_agent(
    actor: ActorContext, agent_id, payload: dict[str, Any], idem_key: str | None
) -> dict:
    """Soft-delete an agent: hide it everywhere and invalidate its key, keep all history."""
    return _change_agent_availability(
        actor,
        agent_id,
        payload,
        idem_key,
        operation=OP_AGENT_DELETE,
        activity_action=ACTIVITY_AGENT_DELETE,
        event_type=EVENT_AGENT_DELETED,
        deletes=True,
    )


def update_agent_role(
    actor: ActorContext, agent_id, payload: dict[str, Any], idem_key: str | None
) -> dict:
    """Global role change on the Agent (membership never carries the role)."""
    assert_in_transaction()
    _require_web_operator(actor)

    role = _parse_role(payload.get("role"))
    expected_version = _parse_expected_version(payload)
    request_hash = canonical_request_hash(
        operation=OP_AGENT_ROLE_UPDATE,
        body={"role": role, "expected_version": expected_version},
        resource_id=agent_id,
    )

    plan, membership_ids, _scope = membership_service.build_revocation_plan(
        agent_id, execution_project_ids=None
    )
    locked = acquire_plan(plan)
    agent = locked.agents.get(agent_id)
    if agent is None:
        raise ResourceNotFoundError("Agent 不存在")
    membership_service.revalidate_revocation_scope(agent_id, plan, execution_project_ids=None)

    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=OP_AGENT_ROLE_UPDATE,
        key=idem_key,
        request_hash=request_hash,
        resource_type="agent",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    if agent.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": agent.version}
        )

    previous_role = agent.role
    changed = previous_role != role
    if changed and role == AgentRole.WORKER.value and agent.enabled:
        active_ids = {
            str(project_id)
            for project_id, project in locked.projects.items()
            if project.status == ProjectStatus.ACTIVE.value and project_id in membership_ids
        }
        membership_service.require_enabled_supervisors_preserved(
            active_ids, removed_agent_ids={agent.pk}
        )

    now = db_now()
    if changed:
        updated = Agent.objects.filter(pk=agent.pk, version=expected_version).update(
            role=role, version=F("version") + 1, updated_at=now
        )
        if updated != 1:  # pragma: no cover - the row is locked
            raise VersionConflictError(details={"expected_version": expected_version})
        agent.refresh_from_db()

    data = _agent_result(agent, now=now)
    complete_receipt(
        receipt,
        response_data=data,
        http_status=200,
        resource_type="agent",
        resource_id=agent.pk,
    )
    if changed:
        record_activity(
            actor,
            ACTIVITY_AGENT_ROLE_UPDATE,
            before={"role": previous_role},
            after={"agent_id": str(agent.pk), "role": agent.role},
        )
        enqueue_outbox(
            EVENT_AGENT_ROLE_UPDATED,
            agent.pk,
            {
                "agent_id": str(agent.pk),
                "previous_role": previous_role,
                "role": agent.role,
                "actor_type": actor.actor_type.value,
            },
        )
    return data


def heartbeat(actor: ActorContext, runtime: dict[str, Any] | None) -> dict:
    """Record the server receive time; never extends a deadline or changes task state."""
    assert_in_transaction()
    runtime_info = _parse_runtime(runtime)
    agent = load_actor_agent_locked(actor)
    now = db_now()
    updates = {"last_heartbeat_at": now}
    if runtime_info:
        updates["runtime_info"] = runtime_info
    # No version bump: a 30-second heartbeat must not invalidate an operator's
    # expected_version, and it is not a business action (no activity/outbox row).
    Agent.objects.filter(pk=agent.pk).update(**updates)
    agent.last_heartbeat_at = now
    if runtime_info:
        agent.runtime_info = runtime_info
    return {
        "agent_id": str(agent.pk),
        "last_heartbeat_at": iso(now),
        "received_at": iso(now),
        "online_state": online_state_for(agent, now=now),
    }


__all__ = [
    "ACTIVITY_AGENT_CREATE",
    "ACTIVITY_AGENT_DISABLE",
    "ACTIVITY_AGENT_ENABLE",
    "ACTIVITY_AGENT_KEY_ROTATE",
    "ACTIVITY_AGENT_ROLE_UPDATE",
    "ACTIVITY_AGENT_UPDATE",
    "EVENT_AGENT_CREATED",
    "EVENT_AGENT_DISABLED",
    "EVENT_AGENT_ENABLED",
    "EVENT_AGENT_ROLE_UPDATED",
    "EVENT_AGENT_UPDATED",
    "ONLINE_ACTIVE_STATES",
    "ONLINE_DISABLED",
    "ONLINE_IDLE",
    "ONLINE_NEVER_CONNECTED",
    "ONLINE_OFFLINE",
    "ONLINE_STATES",
    "ONLINE_WORKING",
    "OP_AGENT_DISABLE",
    "OP_AGENT_ENABLE",
    "OP_AGENT_ROLE_UPDATE",
    "OP_AGENT_UPDATE",
    "PATCHABLE_FIELDS",
    "PROTECTED_AGENT_FIELDS",
    "agent_detail_payload",
    "agent_self",
    "create_agent",
    "disable_agent",
    "enable_agent",
    "execution_summary",
    "get_agent",
    "heartbeat",
    "list_agents",
    "online_state_for",
    "rotate_agent_key",
    "update_agent",
    "update_agent_role",
]
