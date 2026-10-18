"""WebUI 访问密钥的登录/状态/登出端点。

- ``GET  /auth/status``：是否启用校验、当前请求是否已通过（前端启动时调用）。
- ``POST /auth/login`` ：用密钥换取签名 Cookie；密钥错误返回 403。
- ``POST /auth/logout``：清除 Cookie（幂等）。
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from webapi import auth
from webapi.params import parse_json_body, require_idempotency_key, web_actor
from webapi.routes.agents import EnvelopedAPIView


class AuthStatusView(EnvelopedAPIView):
    """``GET /auth/status``：永远 200，用布尔值告诉前端要不要显示登录页。"""

    def get(self, request: HttpRequest):
        actor = web_actor(request)
        data = self.run_read(
            lambda: {
                "required": auth.is_enabled(),
                "authenticated": auth.request_is_authenticated(request),
            }
        )
        return self.ok(request, data)


class AuthLoginView(EnvelopedAPIView):
    """``POST /auth/login``：校验密钥并下发签名 Cookie。"""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        require_idempotency_key(request)
        payload = parse_json_body(request)
        key = str(payload.get("key") or "")

        def login():
            if not auth.is_enabled():
                return {"required": False, "authenticated": True}
            from domain.errors import WebUiKeyInvalidError, WebUiKeyRequiredError

            if not key:
                raise WebUiKeyRequiredError()
            if not auth.key_matches(key):
                raise WebUiKeyInvalidError()
            return {"required": True, "authenticated": True}

        data = self.run_read(login)
        response = self.ok(request, data)
        if auth.is_enabled() and data.get("authenticated"):
            from django.conf import settings

            response.set_cookie(
                auth.COOKIE_NAME,
                auth.make_session_cookie_value(auth.configured_key()),
                max_age=auth.COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                samesite="Lax",
                secure=not settings.DEBUG,
                path="/",
            )
        return response


class AuthLogoutView(EnvelopedAPIView):
    """``POST /auth/logout``：清掉登录 Cookie。"""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        require_idempotency_key(request)
        self.run_read(lambda: None)
        response = self.ok(request, {"authenticated": False, "required": auth.is_enabled()})
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response


urlpatterns: list = [
    path("auth/status", AuthStatusView.as_view(), name="web-auth-status"),
    path("auth/login", AuthLoginView.as_view(), name="web-auth-login"),
    path("auth/logout", AuthLogoutView.as_view(), name="web-auth-logout"),
]
