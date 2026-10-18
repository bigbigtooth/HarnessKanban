"""Django admin（只读）：文件资产与附件关联。"""

from __future__ import annotations

from django.contrib import admin

from .models import AttachmentLink, FileAsset


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(FileAsset)
class FileAssetAdmin(ReadOnlyAdmin):
    list_display = ("original_name", "media_type", "size", "uploader_type", "project", "created_at")
    search_fields = ("original_name", "sha256")
    ordering = ("-created_at",)


@admin.register(AttachmentLink)
class AttachmentLinkAdmin(ReadOnlyAdmin):
    list_display = ("purpose", "file_asset", "task", "project", "created_at")
    list_filter = ("purpose",)
    ordering = ("-created_at",)
