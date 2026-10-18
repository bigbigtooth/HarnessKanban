"""Unified response envelope shared by both APIs (CONTRACT.md §3).

Success:  ``{"data": ..., "request_id": "..."}``
Failure:  ``{"error": {"code", "message", "details"}, "request_id": "..."}``
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from django.http import JsonResponse
from rest_framework import status as drf_status
from rest_framework.exceptions import APIException, ErrorDetail
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from domain.errors import DomainError, InvalidArgumentError, MethodNotAllowedError, ResourceNotFoundError

logger = logging.getLogger("webapi.envelope")


def success_response(data: Any, *, request_id: str | None = None, http_status: int = 200) -> JsonResponse:
    """Plain ``JsonResponse`` so ordinary Django views render without DRF negotiation."""
    return JsonResponse(
        {"data": data, "request_id": request_id or _fallback_request_id()},
        status=http_status,
    )


def list_response(
    items: list[Any],
    *,
    total: int,
    page: int,
    page_size: int,
    extra: dict[str, Any] | None = None,
    request_id: str | None = None,
    http_status: int = 200,
) -> JsonResponse:
    data: dict[str, Any] = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if extra:
        data.update(extra)
    return success_response(data, request_id=request_id, http_status=http_status)


def error_payload(error: DomainError, *, request_id: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
        "request_id": request_id or _fallback_request_id(),
    }


def error_response(error: DomainError, *, request_id: str | None = None) -> JsonResponse:
    return JsonResponse(error_payload(error, request_id=request_id), status=error.http_status)


def domain_error_to_http_status(error: DomainError) -> int:
    try:
        return HTTPStatus(error.http_status).value
    except ValueError:  # pragma: no cover - defensive
        return 500


def _flatten_details(detail: Any) -> Any:
    if isinstance(detail, dict):
        return {key: _flatten_details(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return [_flatten_details(item) for item in detail]
    if isinstance(detail, ErrorDetail):
        return str(detail)
    return detail


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """DRF exception handler producing the frozen envelope."""
    request_id = None
    request = context.get("request")
    if request is not None:
        request_id = getattr(request, "request_id", None)

    if isinstance(exc, DomainError):
        return Response(
            error_payload(exc, request_id=request_id),
            status=domain_error_to_http_status(exc),
        )

    # Framework-level validation / 404 / 405 are mapped onto the same taxonomy instead of
    # leaking DRF's default body shape.
    if isinstance(exc, APIException):
        status_code = exc.status_code
        if status_code == drf_status.HTTP_404_NOT_FOUND:
            mapped: DomainError = ResourceNotFoundError()
        elif status_code == drf_status.HTTP_405_METHOD_NOT_ALLOWED:
            mapped = MethodNotAllowedError()
        elif status_code in (drf_status.HTTP_400_BAD_REQUEST,):
            mapped = InvalidArgumentError(details={"fields": _flatten_details(getattr(exc, "detail", {}))})
        else:
            mapped = DomainError(str(getattr(exc, "detail", exc)))
            mapped.code = _code_for_status(status_code)
            mapped.http_status = status_code
        return Response(
            error_payload(mapped, request_id=request_id),
            status=domain_error_to_http_status(mapped),
        )

    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    logger.exception("未处理的服务端异常", extra={"request_id": request_id})
    unexpected = DomainError("服务处理失败，请携带 request_id 联系管理员")
    return Response(
        error_payload(unexpected, request_id=request_id),
        status=drf_status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _code_for_status(status_code: int) -> str:
    mapping = {
        401: "AGENT_TOKEN_INVALID",
        403: "ROLE_FORBIDDEN",
        409: "VERSION_CONFLICT",
        429: "RATE_LIMITED",
        503: "DEPENDENCY_UNAVAILABLE",
    }
    return mapping.get(status_code, "INVALID_ARGUMENT")


def _fallback_request_id() -> str:
    from webapi.context import get_request_id

    return get_request_id()
