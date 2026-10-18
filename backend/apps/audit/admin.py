"""Django admin（只读）：追加写入的审计事件。"""

from __future__ import annotations

from django.contrib import admin

from .models import ActivityEvent


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_type", "actor_agent", "project", "task", "request_id")
    list_filter = ("actor_type", "action")
    search_fields = ("action", "request_id", "reason")
    ordering = ("-created_at",)

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False
