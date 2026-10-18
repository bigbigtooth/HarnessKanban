"""Root URL configuration.

Only the WebAPI prefix is served by Django. The AgentAPI is a separate FastAPI
application (``agentapi.main:app``) and is never routed through Django.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def health_live(_request):
    """Minimal liveness probe: liveness only, no environment details."""
    return JsonResponse({"data": {"status": "live"}})


def handler404(request, exception=None):
    """Unimplemented or unknown WebAPI paths answer with the frozen error envelope."""
    from domain.errors import ResourceNotFoundError
    from webapi.envelope import error_response

    return error_response(
        ResourceNotFoundError(), request_id=getattr(request, "request_id", None)
    )


# Django admin 的品牌文案（浏览器标题 / 页头 / 首页小标题）
admin.site.site_header = "Harness看板（HarnessKanban）"
admin.site.site_title = "Harness看板"
admin.site.index_title = "运营管理"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live", health_live, name="health-live"),
    path("api/web/v1/", include("webapi.urls")),
]
