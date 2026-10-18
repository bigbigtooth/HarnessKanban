"""Django admin（只读）：项目、成员、文档与上下文版本。"""

from __future__ import annotations

from django.contrib import admin

from .models import Document, DocumentVersion, Project, ProjectContextVersion, ProjectMember


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(Project)
class ProjectAdmin(ReadOnlyAdmin):
    list_display = ("title", "status", "content_version", "version", "deleted_at", "created_at")
    list_filter = ("status", "deleted_at")
    search_fields = ("title", "summary")
    ordering = ("-created_at",)
    readonly_fields = tuple(field.name for field in Project._meta.fields)


@admin.register(ProjectMember)
class ProjectMemberAdmin(ReadOnlyAdmin):
    list_display = ("project", "agent", "created_at")
    search_fields = ("project__title", "agent__name")
    ordering = ("-created_at",)


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdmin):
    list_display = ("name", "project", "is_archived", "current_version", "created_at")
    list_filter = ("is_archived",)
    search_fields = ("name", "project__title")
    ordering = ("-created_at",)


@admin.register(DocumentVersion)
class DocumentVersionAdmin(ReadOnlyAdmin):
    list_display = ("document", "number", "file_asset", "uploaded_by_type", "created_at")
    ordering = ("-created_at",)


@admin.register(ProjectContextVersion)
class ProjectContextVersionAdmin(ReadOnlyAdmin):
    list_display = ("project", "number", "created_at")
    ordering = ("-created_at",)
