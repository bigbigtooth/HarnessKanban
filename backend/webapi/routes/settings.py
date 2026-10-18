"""WebAPI runtime settings routes (T43): environment variables and the LLM profile.

Local-admin only by construction: these paths live under ``/api/web/v1`` on the loopback
WebAPI, which the remote agent proxy never forwards. Secrets are write-only — the GET view
reports whether a credential is set, never its value.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from domain import settings_service
from webapi.params import parse_json_body, require_idempotency_key, web_actor
from webapi.routes.agents import EnvelopedAPIView


class SettingsView(EnvelopedAPIView):
    """``GET /settings`` (masked view) and ``PUT /settings`` (partial merge)."""

    def get(self, request: HttpRequest):
        actor = web_actor(request)
        data = self.run_read(lambda: settings_service.get_settings(actor))
        return self.ok(request, data)

    def put(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        data = self.run_write(
            lambda: settings_service.update_settings(actor, payload, idem_key=idem_key)
        )
        return self.ok(request, data)


class SettingsLlmTestView(EnvelopedAPIView):
    """``POST /settings/llm/test`` — probe the saved LLM endpoint (never raises for an
    unreachable provider; the answer reports why it failed)."""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        data = self.run_read(lambda: settings_service.test_llm_connection(actor))
        return self.ok(request, data)


urlpatterns: list = [
    path("settings", SettingsView.as_view(), name="web-settings"),
    path("settings/llm/test", SettingsLlmTestView.as_view(), name="web-settings-llm-test"),
]
