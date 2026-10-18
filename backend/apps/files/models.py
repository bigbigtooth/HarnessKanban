"""Private file metadata and explicit attachment ownership.

The storage key is always generated server-side; the original name is display metadata
only and never participates in a disk path. A ``FileAsset`` with no document version and
no attachment link is staging material and may be cleaned up after the retention window.
"""

from __future__ import annotations

import uuid

from django.db import models

from domain.contracts import AttachmentPurpose

PURPOSE_CHOICES = [(purpose.value, purpose.value) for purpose in AttachmentPurpose]


class FileAsset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_key = models.CharField(max_length=255, unique=True)
    original_name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    uploader_type = models.CharField(max_length=20)
    uploader_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="uploads"
    )
    # Bounded to the project the upload happened in; authorisation still comes from the
    # attachment link purpose, never from this column alone.
    project = models.ForeignKey(
        "projects.Project", on_delete=models.PROTECT, null=True, blank=True, related_name="file_assets"
    )
    # Set when the file first gains a document version or attachment link. NULL means the
    # file is still staging material.
    bound_at = models.DateTimeField(null=True, blank=True)
    # Non-NULL marks that the OSS copy of this object actually landed (upload mirrored to
    # the private bucket). Only then may a signed ``oss_url`` be handed out.
    oss_uploaded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "files_asset"
        constraints = [
            models.CheckConstraint(condition=models.Q(size__gte=0), name="ck_file_asset_size_non_negative"),
        ]
        indexes = [
            models.Index(fields=["project", "created_at"], name="ix_file_asset_project_time"),
            models.Index(fields=["bound_at", "created_at"], name="ix_file_asset_staging"),
        ]


class AttachmentLink(models.Model):
    """Explicit, purpose-scoped ownership of a file asset by exactly one business object."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file_asset = models.ForeignKey(FileAsset, on_delete=models.PROTECT, related_name="links")
    project = models.ForeignKey(
        "projects.Project", on_delete=models.PROTECT, related_name="attachment_links"
    )
    task = models.ForeignKey(
        "tasks.Task", on_delete=models.PROTECT, null=True, blank=True, related_name="attachment_links"
    )
    execution = models.ForeignKey(
        "tasks.TaskExecution", on_delete=models.PROTECT, null=True, blank=True, related_name="attachment_links"
    )
    submission = models.ForeignKey(
        "tasks.TaskSubmission", on_delete=models.PROTECT, null=True, blank=True, related_name="attachment_links"
    )
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "files_attachment_link"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(purpose=AttachmentPurpose.TASK_REFERENCE.value, task__isnull=False)
                    | ~models.Q(purpose=AttachmentPurpose.TASK_REFERENCE.value)
                ),
                name="ck_link_task_reference_has_task",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value, execution__isnull=False)
                    | ~models.Q(purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value)
                ),
                name="ck_link_execution_artifact_has_execution",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value, submission__isnull=False)
                    | ~models.Q(purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value)
                ),
                name="ck_link_submission_artifact_has_submission",
            ),
        ]
        indexes = [
            models.Index(fields=["file_asset", "purpose"], name="ix_link_asset_purpose"),
            models.Index(fields=["task", "purpose"], name="ix_link_task_purpose"),
            models.Index(fields=["execution", "purpose"], name="ix_link_execution_purpose"),
            models.Index(fields=["submission", "purpose"], name="ix_link_submission_purpose"),
        ]
