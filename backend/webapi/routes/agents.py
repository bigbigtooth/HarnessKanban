"""WebAPI agent routes: creation, list/detail, base attributes and the key lifecycle (T07),
plus the global role / enable / disable actions (T08).

The adapters translate HTTP into one domain service call and serialise the frozen envelope
(CONTRACT.md §3). They never touch ``Agent.role``/``enabled`` directly, never open their own
transaction, and never return key material except in the create / rotate / enable-with-rotate
response, where it is delivered exactly once.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response as DRFResponse

from domain import agent_service
from domain.errors import InvalidArgumentError, MethodNotAllowedError
from webapi.params import (
    parse_json_body,
    parse_pagination,
    parse_query,
    parse_uuid,
    reject_fields,
    require_idempotency_key,
    web_actor,
)
from webapi.views_base import BaseAPIView

AGENT_PATCH_ALLOWED_FIELDS = {"name", "description", "capabilities", "max_concurrency", "expected_version"}
AGENT_CREATE_FORBIDDEN_FIELDS = {
    "enabled",
    "key_hash",
    "key_version",
    "reserved_execution_count",
    "version",
    "id",
}
AGENT_ENABLE_ALLOWED_FIELDS = {"rotate_key"}
AGENT_DISABLE_ALLOWED_FIELDS = {"reason", "reclaim"}
AGENT_ROLE_ALLOWED_FIELDS = {"role", "expected_version"}

AGENT_LIST_PARAMS = {"name", "role", "enabled", "online", "deleted", "page", "page_size"}


class EnvelopedAPIView(BaseAPIView):
    """``BaseAPIView`` with the shared error envelope for plain Django views.

    ``BaseAPIView`` returns ``rest_framework`` ``Response`` objects, which must be finalised
    (renderer + context) when they are returned from a plain ``View``; and every framework
    or domain failure is serialised into the frozen error envelope instead of Django's
    default HTML body. No stack trace, SQL or filesystem path can escape this way.
    """

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the envelope handler decides the body
            response = self.handle_error(request, exc)
        return self.finalize(request, response)

    def finalize(self, request: HttpRequest, response):
        if isinstance(response, DRFResponse) and getattr(response, "accepted_renderer", None) is None:
            response.accepted_renderer = JSONRenderer()
            response.accepted_media_type = "application/json"
            response.renderer_context = {"request": request, "view": self}
        return response

    def http_method_not_allowed(self, request: HttpRequest, *args, **kwargs):
        return self.handle_error(request, MethodNotAllowedError())


def reject_unknown_fields(payload: dict, allowed: set[str]) -> None:
    unknown = sorted(name for name in payload if name not in allowed)
    if unknown:
        raise InvalidArgumentError(
            "请求包含不支持的字段",
            details={"fields": unknown, "allowed": sorted(allowed)},
        )


class AgentCollectionView(EnvelopedAPIView):
    """``GET /agents`` list+filters and ``POST /agents`` create."""

    def get(self, request: HttpRequest):
        actor = web_actor(request)
        query = parse_query(request, allowed=AGENT_LIST_PARAMS)
        page, page_size = parse_pagination(query)
        filters = {
            "name": query.optional_str("name", max_length=agent_service.NAME_MAX_LENGTH),
            "role": query.get("role"),
            "enabled": query.get_bool("enabled"),
            "online": query.get_bool("online"),
            "deleted": query.get_bool("deleted"),
        }
        data = self.run_read(lambda: agent_service.list_agents(actor, filters, page, page_size))
        return self.ok(request, data)

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, AGENT_CREATE_FORBIDDEN_FIELDS)
        reject_unknown_fields(
            payload, {"name", "role", "max_concurrency", "description", "capabilities"}
        )
        data = self.run_write(lambda: agent_service.create_agent(actor, payload, idem_key))
        # A replay of a lost create response delivers the original metadata (with
        # ``key_delivery_unavailable``) instead of a created resource.
        http_status = 200 if data.get("idempotent_replay") else 201
        return self.ok(request, data, http_status=http_status)


class AgentDetailView(EnvelopedAPIView):
    """``GET /agents/{id}`` detail and ``PATCH /agents/{id}`` base attributes."""

    def get(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        data = self.run_read(lambda: agent_service.get_agent(actor, parsed_id))
        return self.ok(request, data)

    def patch(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        # Role and enable/disable are business actions (T08), never generic PATCH fields.
        reject_fields(payload, agent_service.PROTECTED_AGENT_FIELDS)
        reject_unknown_fields(payload, AGENT_PATCH_ALLOWED_FIELDS)
        data = self.run_write(
            lambda: agent_service.update_agent(actor, parsed_id, payload, idem_key)
        )
        return self.ok(request, data)


class AgentRotateKeyView(EnvelopedAPIView):
    """``POST /agents/{id}/rotate-key``: new key returned once, same Agent id."""

    def post(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_unknown_fields(payload, set())
        data = self.run_write(lambda: agent_service.rotate_agent_key(actor, parsed_id, idem_key))
        return self.ok(request, data)


class AgentEnableView(EnvelopedAPIView):
    """``POST /agents/{id}/enable`` with optional ``{"rotate_key": true}``."""

    def post(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"enabled"})
        reject_unknown_fields(payload, AGENT_ENABLE_ALLOWED_FIELDS)
        data = self.run_write(
            lambda: agent_service.enable_agent(actor, parsed_id, payload, idem_key)
        )
        return self.ok(request, data)


class AgentDisableView(EnvelopedAPIView):
    """``POST /agents/{id}/disable`` with ``reason`` and ``reclaim`` disposition."""

    def post(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"enabled"})
        reject_unknown_fields(payload, AGENT_DISABLE_ALLOWED_FIELDS)
        data = self.run_write(
            lambda: agent_service.disable_agent(actor, parsed_id, payload, idem_key)
        )
        return self.ok(request, data)


class AgentDeleteView(EnvelopedAPIView):
    """``POST /agents/{id}/delete``：软删除（隐藏 + Key 失效 + 保留全部历史）。"""

    def post(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"enabled"})
        reject_unknown_fields(payload, AGENT_DISABLE_ALLOWED_FIELDS)
        data = self.run_write(
            lambda: agent_service.delete_agent(actor, parsed_id, payload, idem_key)
        )
        return self.ok(request, data)


class AgentBulkDeleteView(EnvelopedAPIView):
    """``POST /agents/bulk-delete``：逐项结果，一项失败不影响其他项。"""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_unknown_fields(payload, {"agent_ids", "reason", "reclaim"})
        data = self.run_write(lambda: agent_service.bulk_delete_agents(actor, payload, idem_key))
        return self.ok(request, data)


class AgentBulkRestoreView(EnvelopedAPIView):
    """``POST /agents/bulk-restore``：恢复（启用 + 清空删除标记）。"""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_unknown_fields(payload, {"agent_ids"})
        data = self.run_write(lambda: agent_service.bulk_restore_agents(actor, payload, idem_key))
        return self.ok(request, data)


class AgentRoleView(EnvelopedAPIView):
    """``POST /agents/{id}/role``: global role change guarded by the last supervisor rule."""

    def post(self, request: HttpRequest, agent_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"enabled", "key_hash", "key_version", "reserved_execution_count", "version"})
        reject_unknown_fields(payload, AGENT_ROLE_ALLOWED_FIELDS)
        data = self.run_write(
            lambda: agent_service.update_agent_role(actor, parsed_id, payload, idem_key)
        )
        return self.ok(request, data)


urlpatterns: list = [
    path("agents", AgentCollectionView.as_view(), name="web-agent-list"),
    # 批量路由必须先于 ``agents/<agent_id>`` 注册，否则 "bulk-delete" 会被当成 ID。
    path(
        "agents/bulk-delete",
        AgentBulkDeleteView.as_view(),
        name="web-agent-bulk-delete",
    ),
    path(
        "agents/bulk-restore",
        AgentBulkRestoreView.as_view(),
        name="web-agent-bulk-restore",
    ),
    path("agents/<str:agent_id>", AgentDetailView.as_view(), name="web-agent-detail"),
    path(
        "agents/<str:agent_id>/rotate-key",
        AgentRotateKeyView.as_view(),
        name="web-agent-rotate-key",
    ),
    path("agents/<str:agent_id>/enable", AgentEnableView.as_view(), name="web-agent-enable"),
    path("agents/<str:agent_id>/disable", AgentDisableView.as_view(), name="web-agent-disable"),
    path("agents/<str:agent_id>/role", AgentRoleView.as_view(), name="web-agent-role"),
    path(
        "agents/<str:agent_id>/delete",
        AgentDeleteView.as_view(),
        name="web-agent-delete",
    ),
]

# Re-exported so the sibling project route module shares the same envelope plumbing and
# never grows a second implementation of the protocol adaptation.
__all__ = ["EnvelopedAPIView", "reject_unknown_fields", "urlpatterns"]
