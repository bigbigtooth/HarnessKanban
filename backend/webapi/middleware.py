"""WebAPI request boundaries (DEVELOP D06/D19, product 17.2).

These checks are browser request boundaries, not a new authentication scheme: the local
WebAPI is unauthenticated on a trusted host. They keep cross-site browser requests and
non-JSON bodies out, and make the request id visible in every response.
"""

from __future__ import annotations

import json
import logging
import time

from django.http import HttpRequest, HttpResponse

from domain.errors import RequestOriginForbiddenError, WebUiKeyInvalidError, WebUiKeyRequiredError
from webapi.context import reset_request_id, sanitize_request_id, set_request_id
from webapi.envelope import error_response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 登录/状态端点本身不能要求已登录（否则无法登录）。
WEBUI_AUTH_EXEMPT_PATHS = frozenset(
    {"/api/web/v1/auth/status", "/api/web/v1/auth/login", "/api/web/v1/auth/logout"}
)

# Paths that legitimately accept multipart/form-data bodies.
MULTIPART_PATH_MARKERS = ("/attachments", "/documents", "/versions", "/files")

# Origin / content-type boundaries belong to the WebAPI contract. Other Django surfaces
# (notably the session-authenticated admin, which posts urlencoded forms) manage their own
# CSRF and must not be forced through the JSON-only boundary.
WEB_API_PATH_PREFIX = "/api/"


class RequestContextMiddleware:
    """Assigns the request id, logs one structured request-chain line, and always clears it.

    The line carries the identifiers an operator needs to follow one request across the
    APIs and the asynchronous jobs: ``request_id``, the resolved path template, the
    response status and error code, the duration, and whichever of task/execution/project/
    agent ids the route carried. Secrets, result bodies and filesystem paths are never
    logged.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = logging.getLogger("harvesstask.access")

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = sanitize_request_id(request.headers.get("X-Request-Id"))
        request.request_id = request_id
        token = set_request_id(request_id)
        started = time.monotonic()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            try:
                self._log(request, response, duration_ms)
            except Exception:  # noqa: BLE001 - logging must never break a response
                self.logger.exception("结构化访问日志写入失败")
            reset_request_id(token)
            if response is not None:
                response["X-Request-Id"] = request_id

    def _log(self, request: HttpRequest, response: HttpResponse | None, duration_ms: float) -> None:
        match = getattr(request, "resolver_match", None)
        kwargs = getattr(match, "kwargs", {}) or {}
        fields = [
            f"method={request.method}",
            f"path={getattr(request, 'path', '-')}",
            f"route={getattr(match, 'route', '-')}",
            f"status={getattr(response, 'status_code', 0)}",
            f"duration_ms={duration_ms}",
            f"error_code={_error_code(response) or '-'}",
        ]
        for key in ("task_id", "execution_id", "project_id", "agent_id"):
            value = kwargs.get(key)
            if value:
                fields.append(f"{key}={value}")
        self.logger.info("http_request %s", " ".join(fields))


def _error_code(response: HttpResponse | None) -> str | None:
    """Read the business error code from a failure envelope, without echoing the body."""
    if response is None or getattr(response, "status_code", 200) < 400:
        return None
    content_type = response.get("Content-Type", "") if hasattr(response, "get") else ""
    if "json" not in content_type:
        return None
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return error.get("code")
    return None


class RequestBoundaryMiddleware:
    """Host, Origin and content-type boundaries for the local WebAPI."""

    def __init__(self, get_response):
        self.get_response = get_response
        from django.conf import settings

        self.allowed_hosts = set(settings.ALLOWED_HOSTS)
        self.allowed_origins = set(settings.HARNESSTASK["allowed_origins"])

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = getattr(request, "request_id", None)
        try:
            self._check_host(request)
            if request.path.startswith(WEB_API_PATH_PREFIX):
                self._check_origin(request)
                self._check_content_type(request)
        except RequestOriginForbiddenError as exc:
            return error_response(exc, request_id=request_id)
        return self.get_response(request)

    def _check_host(self, request: HttpRequest) -> None:
        # ``request.get_host()`` itself raises DisallowedHost outside ALLOWED_HOSTS, which
        # Django turns into a non-enveloped 400. Reading the header and validating it here
        # keeps every rejected Host on the documented 403 REQUEST_ORIGIN_FORBIDDEN path.
        raw = request.META.get("HTTP_HOST") or request.META.get("SERVER_NAME") or ""
        host = raw.split(":")[0].strip().lower()
        if not host or host not in {allowed.lower() for allowed in self.allowed_hosts}:
            raise RequestOriginForbiddenError(
                "请求 Host 不在允许范围内", details={"host": host or "missing"}
            )

    def _check_origin(self, request: HttpRequest) -> None:
        origin = request.headers.get("Origin")
        if not origin:
            return
        if origin not in self.allowed_origins:
            raise RequestOriginForbiddenError(
                "请求 Origin 不在允许范围内", details={"origin": origin}
            )

    def _check_content_type(self, request: HttpRequest) -> None:
        if request.method in SAFE_METHODS:
            return
        content_type = request.content_type or ""
        if any(marker in request.path for marker in MULTIPART_PATH_MARKERS):
            allowed = ("multipart/form-data", "application/json")
        else:
            allowed = ("application/json",)

        if not content_type:
            # A bodyless request legitimately sends no Content-Type. Requiring one would
            # reject valid bodyless DELETEs (for example removing a project member) with a
            # misleading origin error, so only a request that actually carries a body must
            # declare an acceptable type.
            length = request.META.get("CONTENT_LENGTH") or ""
            if request.method == "DELETE" or length in ("", "0"):
                return
            raise RequestOriginForbiddenError(
                "带请求体的写操作必须声明内容类型",
                details={"content_type": content_type, "allowed": list(allowed)},
            )

        if not content_type.startswith(allowed):
            raise RequestOriginForbiddenError(
                "请求内容类型不被支持",
                details={"content_type": content_type, "allowed": list(allowed)},
            )


class WebUiAuthMiddleware:
    """WebUI 访问密钥校验：未携带或密钥错误时返回 403（错误信封）。

    只在配置了 ``WEBUI_ACCESS_KEY`` 时生效；只作用于 WebAPI（``/api/``），
    不影响 Django admin（它有自己的登录会话）与健康检查。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from webapi import auth

        request_id = getattr(request, "request_id", None)
        if (
            not auth.is_enabled()
            or not request.path.startswith(WEB_API_PATH_PREFIX)
            or request.path in WEBUI_AUTH_EXEMPT_PATHS
            or request.method in ("OPTIONS", "HEAD")
        ):
            return self.get_response(request)

        if auth.request_is_authenticated(request):
            return self.get_response(request)

        provided = request.headers.get("X-WebUI-Key", "")
        error = WebUiKeyInvalidError() if provided else WebUiKeyRequiredError()
        return error_response(error, request_id=request_id)
