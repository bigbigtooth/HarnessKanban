"""Thin Django view plumbing shared by every WebAPI endpoint.

Views translate HTTP to a service call and a service result back to the frozen envelope.
They never touch ``Task.status`` or open their own transaction.
"""

from __future__ import annotations

import logging

from django.http import Http404, HttpRequest
from django.views import View

from domain.errors import (
    DomainError,
    IdempotencyRaceLost,
    MethodNotAllowedError,
    ResourceNotFoundError,
)
from domain.service_runner import run_request_service
from webapi.envelope import error_response, success_response

logger = logging.getLogger("webapi.views")


class BaseAPIView(View):
    """Base class providing envelope handling and outermost-transaction execution.

    Exception conversion happens here rather than in middleware: Django wraps every
    middleware layer in its own exception-to-500 converter, so a middleware can no longer
    observe a view exception. Without this, parameter validation that runs before a view's
    own ``try`` block (a missing idempotency key, a malformed UUID) would surface as a
    generic 500 page instead of the documented error envelope.
    """

    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]
    global_claim_lock = False

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except Http404 as exc:
            return self.handle_error(request, ResourceNotFoundError(str(exc) or None))
        except BaseException as exc:  # noqa: BLE001 - mapped below, never swallowed silently
            return self.handle_error(request, exc)

    def http_method_not_allowed(self, request: HttpRequest, *args, **kwargs):
        """Method rejection is a business answer too, so it uses the shared envelope."""
        return self.handle_error(request, MethodNotAllowedError())

    def run_write(self, callback, *, global_claim_lock: bool | None = None):
        lock = self.global_claim_lock if global_claim_lock is None else global_claim_lock
        return run_request_service(callback, global_claim_lock=lock)

    def run_read(self, callback):
        """Reads do not open a write transaction and never mutate business state."""
        return callback()

    def handle_error(self, request: HttpRequest, exc: BaseException):
        if isinstance(exc, DomainError):
            return error_response(exc, request_id=getattr(request, "request_id", None))
        if isinstance(exc, IdempotencyRaceLost):
            from domain.errors import IdempotencyInProgressError

            return error_response(
                IdempotencyInProgressError(details={"key": exc.key}),
                request_id=getattr(request, "request_id", None),
            )
        logger.exception("WebAPI 未处理异常", extra={"request_id": getattr(request, "request_id", None)})
        return error_response(
            DomainError("服务处理失败，请携带 request_id 联系管理员"),
            request_id=getattr(request, "request_id", None),
        )

    def ok(self, request: HttpRequest, data, *, http_status: int = 200):
        return success_response(data, request_id=getattr(request, "request_id", None), http_status=http_status)
