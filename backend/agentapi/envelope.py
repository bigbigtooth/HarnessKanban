"""AgentAPI exception handling: the same envelope as the WebAPI (CONTRACT.md §3)."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from domain.errors import DomainError, InvalidArgumentError, MethodNotAllowedError, ResourceNotFoundError

logger = logging.getLogger("agentapi.envelope")

STATUS_CODE_MAP = {
    401: "AGENT_TOKEN_INVALID",
    403: "ROLE_FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "VERSION_CONFLICT",
    429: "RATE_LIMITED",
    503: "DEPENDENCY_UNAVAILABLE",
}


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-Id") or str(uuid.uuid4())


def envelope(payload_key: str, body: dict, *, request_id: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse({payload_key: body, "request_id": request_id}, status_code=status_code)


def success(data, *, request_id: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"data": data, "request_id": request_id}, status_code=status_code)


def failure(error: DomainError, *, request_id: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": {"code": error.code, "message": error.message, "details": error.details},
            "request_id": request_id,
        },
        status_code=error.http_status,
    )


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError):
        return failure(exc, request_id=request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        mapped = InvalidArgumentError(details={"errors": _clean_errors(exc.errors())})
        return failure(mapped, request_id=request_id_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            mapped: DomainError = ResourceNotFoundError()
        elif exc.status_code == 405:
            mapped = MethodNotAllowedError()
        else:
            mapped = DomainError(str(exc.detail))
            mapped.code = STATUS_CODE_MAP.get(exc.status_code, "INVALID_ARGUMENT")
            mapped.http_status = exc.status_code
        return failure(mapped, request_id=request_id_of(request))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        request_id = request_id_of(request)
        logger.exception("AgentAPI 未处理异常", extra={"request_id": request_id})
        return failure(
            DomainError("服务处理失败，请携带 request_id 联系管理员"),
            request_id=request_id,
        )


def _clean_errors(errors: list[dict]) -> list[dict]:
    """Strip request bodies from validation errors so secrets are not echoed back."""
    cleaned = []
    for error in errors:
        cleaned.append(
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
        )
    return cleaned
