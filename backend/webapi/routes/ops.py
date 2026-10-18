"""WebAPI operations routes: queryable failures and manual recovery (T34).

Management-only. These paths live under ``/api/web/v1`` on the loopback WebAPI, so the
remote Agent proxy (which forwards only ``/api/agent/v1``) never exposes them.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from domain import ops_service
from webapi.params import (
    parse_pagination,
    parse_query,
    parse_uuid,
    require_idempotency_key,
    web_actor,
)
from webapi.routes.agents import EnvelopedAPIView

FAILURE_LIST_PARAMS = {"page", "page_size", "include_resolved"}


def _bool_param(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value in ("1", "true", "True")


class OpsFailureListView(EnvelopedAPIView):
    """``GET /ops/failures`` — stuck outbox events plus recorded system failures."""

    def get(self, request: HttpRequest):
        actor = web_actor(request)
        query = parse_query(request, allowed=FAILURE_LIST_PARAMS)
        page, page_size = parse_pagination(query)
        data = self.run_read(
            lambda: ops_service.list_failures(
                actor,
                page=page,
                page_size=page_size,
                include_resolved=_bool_param(query.get("include_resolved"), False),
            )
        )
        return self.ok(request, data)


class OpsOutboxRetryView(EnvelopedAPIView):
    """``POST /ops/outbox/{event_id}/retry`` — keep the event, make it dispatchable again."""

    def post(self, request: HttpRequest, event_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(event_id, field="event_id")
        idem_key = require_idempotency_key(request)
        data = self.run_write(
            lambda: ops_service.retry_outbox_event(actor, parsed_id, idem_key=idem_key)
        )
        return self.ok(request, data)


class OpsCapacityAuditView(EnvelopedAPIView):
    """``POST /ops/capacity-audit`` — run the reserved-slot audit now."""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        data = self.run_write(
            lambda: ops_service.run_capacity_audit(actor, idem_key=idem_key)
        )
        return self.ok(request, data)


urlpatterns: list = [
    path("ops/failures", OpsFailureListView.as_view(), name="web-ops-failures"),
    path(
        "ops/outbox/<str:event_id>/retry",
        OpsOutboxRetryView.as_view(),
        name="web-ops-outbox-retry",
    ),
    path("ops/capacity-audit", OpsCapacityAuditView.as_view(), name="web-ops-capacity-audit"),
]
