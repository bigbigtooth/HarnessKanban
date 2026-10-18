"""Django admin（只读）：任务、执行、提交与审核。

任务状态只能通过动作端点推进（发布/领取/提交/审核…），后台不提供编辑，
避免绕过状态机与版本冲突检查。
"""

from __future__ import annotations

from django.contrib import admin

from .models import Task, TaskClaimLock, TaskExecution, TaskReview, TaskSubmission


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False


@admin.register(Task)
class TaskAdmin(ReadOnlyAdmin):
    list_display = (
        "number",
        "title",
        "status",
        "difficulty",
        "claim_mode",
        "project",
        "designated_agent",
        "assignee_agent",
        "is_archived",
        "version",
        "updated_at",
    )
    list_filter = ("status", "claim_mode", "difficulty", "is_archived")
    search_fields = ("title", "description", "acceptance_criteria")
    ordering = ("-updated_at",)
    readonly_fields = tuple(field.name for field in Task._meta.fields)


@admin.register(TaskExecution)
class TaskExecutionAdmin(ReadOnlyAdmin):
    list_display = ("task", "agent", "sequence", "claimed_at", "execution_deadline_at", "ended_at", "end_reason")
    list_filter = ("end_reason",)
    search_fields = ("task__title", "agent__name")
    ordering = ("-claimed_at",)


@admin.register(TaskSubmission)
class TaskSubmissionAdmin(ReadOnlyAdmin):
    list_display = ("task", "execution", "summary", "submitted_at")
    search_fields = ("task__title", "summary")
    ordering = ("-submitted_at",)


@admin.register(TaskReview)
class TaskReviewAdmin(ReadOnlyAdmin):
    list_display = ("task", "decision", "actor_type", "reviewer_agent", "created_at")
    list_filter = ("decision", "actor_type")
    search_fields = ("task__title",)
    ordering = ("-created_at",)


@admin.register(TaskClaimLock)
class TaskClaimLockAdmin(ReadOnlyAdmin):
    list_display = ("id",)
