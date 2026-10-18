"""FastAPI AgentAPI application.

Processes run synchronously behind sync routes so a request's database authentication,
transaction, evaluation and cleanup share one worker thread (DEVELOP D05). The public
surface is ``/api/agent/v1`` plus a minimal liveness probe.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from fastapi import APIRouter, FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from agentapi.envelope import install_handlers, success  # noqa: E402
from agentapi.routers import ROUTE_MODULES  # noqa: E402

API_PREFIX = "/api/agent/v1"


def create_app() -> FastAPI:
    app = FastAPI(
        title="HarnessKanban AgentAPI",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        raw = request.headers.get("X-Request-Id", "")
        request_id = raw.strip()[:64] if raw.strip() else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    install_handlers(app)

    api = APIRouter(prefix=API_PREFIX)
    for module in ROUTE_MODULES:
        api.include_router(module.router)
    app.include_router(api)

    @app.get("/health/live")
    def health_live(request: Request):
        """Liveness only: no database, queue or filesystem details."""
        return success({"status": "live"}, request_id=request.state.request_id)

    return app


app = create_app()
