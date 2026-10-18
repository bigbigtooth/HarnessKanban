"""Projects, membership authorisation, immutable context versions and documents.

Project lifecycle is independent from the five task statuses. Context versions and
document versions are append-only: an existing execution's snapshot evidence must never
be rewritten by a later edit (product 5, 11; DEVELOP D08/D09).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import GreaterThanOrEqual, LessThanOrEqual

from domain.contracts import ActorType, ProjectStatus

PROJECT_STATUS_CHOICES = [(status.value, status.value) for status in ProjectStatus]
UPLOADER_TYPE_CHOICES = [
    (ActorType.WEB_OPERATOR.value, ActorType.WEB_OPERATOR.value),
    (ActorType.AGENT.value, ActorType.AGENT.value),
]


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, default="")
    background = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20, choices=PROJECT_STATUS_CHOICES, default=ProjectStatus.DRAFT.value
    )
    archived_from_status = models.CharField(
        max_length=20, choices=PROJECT_STATUS_CHOICES, null=True, blank=True
    )
    # 软删除：与归档一致地冻结项目（进行中/待审核任务前置校验），同时从列表与统计中消失。
    # 任务、文档与上下文版本一行都不删；恢复时回到删除前的生命周期。
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_reason = models.TextField(blank=True, default="")
    content_version = models.PositiveIntegerField(default=1)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "projects_project"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=[status.value for status in ProjectStatus]),
                name="ck_project_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    GreaterThanOrEqual(Length("title"), 1) & LessThanOrEqual(Length("title"), 200)
                ),
                name="ck_project_title_length",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="ix_project_status"),
            models.Index(fields=["deleted_at"], name="ix_project_deleted_at"),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return f"{self.title}({self.status})"


class ProjectMember(models.Model):
    """Authorisation only. The supervisor/worker role lives on ``Agent``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="members")
    agent = models.ForeignKey("identity.Agent", on_delete=models.PROTECT, related_name="memberships")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_member"
        constraints = [
            models.UniqueConstraint(fields=["project", "agent"], name="uniq_member_project_agent"),
        ]
        indexes = [
            models.Index(fields=["agent", "project"], name="ix_member_agent_project"),
        ]


class ProjectContextVersion(models.Model):
    """Immutable snapshot of a project's context at a point in time."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="context_versions")
    number = models.PositiveIntegerField()
    background_snapshot = models.TextField(blank=True, default="")
    # Frozen list of {"document_id": ..., "document_version_id": ..., "number": ..., "name": ...}
    document_version_refs = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_context_version"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "number"], name="uniq_context_version_project_number"
            ),
        ]
        ordering = ["project", "-number"]


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="documents")
    name = models.CharField(max_length=255)
    current_version = models.ForeignKey(
        "projects.DocumentVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "projects_document"
        indexes = [
            models.Index(fields=["project", "is_archived"], name="ix_document_project_archived"),
        ]


class DocumentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    file_asset = models.ForeignKey("files.FileAsset", on_delete=models.PROTECT, related_name="+")
    uploaded_by_type = models.CharField(max_length=20, choices=UPLOADER_TYPE_CHOICES)
    uploaded_by_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_document_version"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "number"], name="uniq_document_version_document_number"
            ),
        ]
        ordering = ["document", "-number"]
