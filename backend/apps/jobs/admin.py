"""Django admin（只读）：Outbox 与幂等记录（排障用）。"""

from __future__ import annotations

from django.contrib import admin

from .models import IdempotencyRecord, OutboxEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(OutboxEvent)
class OutboxEventAdmin(ReadOnlyAdmin):
    list_display = ("event_type", "delivery_state", "attempts", "next_retry_at", "created_at")
    list_filter = ("event_type", "delivery_state")
    ordering = ("-created_at",)


@admin.register(IdempotencyRecord)
class IdempotencyRecordAdmin(ReadOnlyAdmin):
    list_display = ("actor_scope", "operation", "state", "resource_type", "created_at", "expires_at")
    list_filter = ("operation", "state")
    search_fields = ("key",)
    ordering = ("-created_at",)
