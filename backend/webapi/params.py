"""Strict request-parameter handling for the WebAPI (CONTRACT.md §3, product 12.2).

Lengths, UUIDs, duplicate query parameters, page bounds and the idempotency key are all
validated explicitly instead of being handed to an ORM.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest

from domain.errors import IdempotencyKeyRequiredError, InvalidArgumentError, UnsupportedParameterError
from domain.contracts import ActorContext, ActorType
from webapi.context import get_request_id

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class StrictQuery:
    """Parsed query parameters with duplicate detection and an allow-list."""

    values: dict[str, str]
    raw_items: list[tuple[str, str]]

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.values.get(name, default)

    def get_bool(self, name: str, default: bool | None = None) -> bool | None:
        raw = self.values.get(name)
        if raw is None:
            return default
        if raw in ("1", "true", "True"):
            return True
        if raw in ("0", "false", "False"):
            return False
        raise InvalidArgumentError(
            f"参数 {name} 必须是布尔值", details={"parameter": name, "value": raw}
        )

    def get_int(self, name: str, default: int | None = None) -> int | None:
        raw = self.values.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise InvalidArgumentError(
                f"参数 {name} 必须是整数", details={"parameter": name, "value": raw}
            ) from exc

    def get_uuid(self, name: str, default: uuid.UUID | None = None) -> uuid.UUID | None:
        raw = self.values.get(name)
        if raw is None or raw == "":
            return default
        return parse_uuid(raw, field=name)

    def require_uuid(self, name: str) -> uuid.UUID:
        value = self.get_uuid(name)
        if value is None:
            raise InvalidArgumentError(f"缺少必需参数 {name}", details={"parameter": name})
        return value

    def require_str(self, name: str, *, max_length: int = 5000, min_length: int = 1) -> str:
        raw = self.values.get(name)
        if raw is None:
            raise InvalidArgumentError(f"缺少必需参数 {name}", details={"parameter": name})
        raw = raw.strip()
        if len(raw) < min_length:
            raise InvalidArgumentError(f"参数 {name} 不能为空", details={"parameter": name})
        if len(raw) > max_length:
            raise InvalidArgumentError(
                f"参数 {name} 超过长度上限 {max_length}",
                details={"parameter": name, "max_length": max_length},
            )
        return raw

    def optional_str(self, name: str, *, max_length: int = 5000) -> str | None:
        raw = self.values.get(name)
        if raw is None:
            return None
        raw = raw.strip()
        if len(raw) > max_length:
            raise InvalidArgumentError(
                f"参数 {name} 超过长度上限 {max_length}",
                details={"parameter": name, "max_length": max_length},
            )
        return raw


def parse_query(request: HttpRequest, *, allowed: set[str], allow_multi: set[str] | None = None) -> StrictQuery:
    """Parse ``request.GET`` while preserving raw multi-value information.

    Duplicated keys are rejected (product 12.2) except for explicitly permitted repeatable
    filters, and unknown keys are rejected so a typo cannot silently change a query.
    """
    allow_multi = allow_multi or set()
    raw_items: list[tuple[str, str]] = []
    values: dict[str, str] = {}
    seen: set[str] = set()

    for key in request.GET:
        items = request.GET.getlist(key)
        for value in items:
            raw_items.append((key, value))
        if key not in allowed:
            raise UnsupportedParameterError(
                f"不支持的查询参数 {key}", details={"parameter": key}
            )
        if len(items) > 1 and key not in allow_multi:
            raise InvalidArgumentError(
                f"查询参数 {key} 重复出现", details={"parameter": key}
            )
        values[key] = items[-1]
        seen.add(key)

    return StrictQuery(values=values, raw_items=raw_items)


def parse_pagination(query: StrictQuery) -> tuple[int, int]:
    page = query.get_int("page", 1) or 1
    page_size = query.get_int("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE
    if page < 1:
        raise InvalidArgumentError("page 必须大于等于 1", details={"page": page})
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise InvalidArgumentError(
            f"page_size 必须在 1 到 {MAX_PAGE_SIZE} 之间", details={"page_size": page_size}
        )
    return page, page_size


def parse_uuid(value: str, *, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(
            f"参数 {field} 必须是合法 UUID", details={"parameter": field}
        ) from exc


def require_idempotency_key(request: HttpRequest) -> str:
    """Every create/business-action write requires an ``Idempotency-Key`` header."""
    raw = request.headers.get("Idempotency-Key")
    if raw is None or not raw.strip():
        raise IdempotencyKeyRequiredError()
    key = raw.strip()
    if len(key) > 255:
        raise InvalidArgumentError(
            "Idempotency-Key 超过长度上限 255", details={"max_length": 255}
        )
    return key


def optional_idempotency_key(request: HttpRequest) -> str | None:
    raw = request.headers.get("Idempotency-Key")
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def web_actor(request: HttpRequest) -> ActorContext:
    """The local WebAPI has no login: the source is recorded as the local administrator."""
    return ActorContext(
        actor_type=ActorType.WEB_OPERATOR,
        request_id=get_request_id(),
        web_operator_note="local-admin-webui",
    )


def parse_json_body(request: HttpRequest) -> dict[str, Any]:
    import json

    if not request.body:
        return {}
    try:
        parsed = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidArgumentError("请求体必须是合法 JSON") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    return parsed


def require_field(payload: dict[str, Any], name: str, *, max_length: int | None = None) -> Any:
    if name not in payload or payload[name] is None:
        raise InvalidArgumentError(f"缺少必需字段 {name}", details={"field": name})
    value = payload[name]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise InvalidArgumentError(f"字段 {name} 不能为空", details={"field": name})
        if max_length is not None and len(value) > max_length:
            raise InvalidArgumentError(
                f"字段 {name} 超过长度上限 {max_length}",
                details={"field": name, "max_length": max_length},
            )
    return value


def reject_fields(payload: dict[str, Any], forbidden: set[str]) -> None:
    """Reject attempts to write fields that only business actions may change."""
    present = sorted(name for name in forbidden if name in payload)
    if present:
        raise InvalidArgumentError(
            "请求包含只能由业务动作修改的字段",
            details={"fields": present},
        )


FORBIDDEN_TASK_PATCH_FIELDS = {
    "status",
    "assignee_agent_id",
    "active_execution_id",
    "current_submission_id",
    "version",
    "requirements_version",
    "number",
    "project_id",
    "is_archived",
}


FORBIDDEN_IDENTITY_FIELDS = {
    "agent_id",
    "assignee_agent_id",
    "reviewer_agent_id",
    "submitted_by_agent_id",
    "actor_agent_id",
}
