"""Django admin（只读）：Agent 目录。

后台只用于查看与排障：领域写操作必须走 WebAPI 的动作端点，
直接改库会绕过版本号、审计与容量约束。
"""

from __future__ import annotations

from django.contrib import admin

from .models import Agent


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "role",
        "enabled",
        "max_concurrency",
        "reserved_execution_count",
        "last_heartbeat_at",
        "deleted_at",
        "version",
    )
    list_filter = ("role", "enabled", "deleted_at")
    search_fields = ("name",)
    ordering = ("-created_at",)
    readonly_fields = tuple(field.name for field in Agent._meta.fields)

    def has_add_permission(self, request) -> bool:  # noqa: ANN001 - Django 签名
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False
