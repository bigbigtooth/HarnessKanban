"""Canonical API representations.

Every module renders business objects through these functions so both APIs and every
endpoint agree on field names, enum values and timestamp formatting. Plaintext key
material never appears here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from apps.identity.models import Agent
from apps.projects.models import Document, DocumentVersion, Project, ProjectContextVersion
from apps.tasks.models import Task, TaskExecution, TaskReview, TaskSubmission
from domain import oss_storage
from domain.contracts import (
    DIFFICULTY_NAMES,
    DIFFICULTY_TIMEOUT_SECONDS,
    display_number,
)


def iso(value: datetime | None) -> str | None:
    """ISO 8601 UTC with a trailing Z, e.g. 2026-10-15T10:00:00Z."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    if value.microsecond:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def uuid_str(value) -> str | None:
    return None if value is None else str(value)


def agent_summary(agent: Agent, *, online_state: str | None = None) -> dict[str, Any]:
    return {
        "id": str(agent.pk),
        "name": agent.name,
        "role": agent.role,
        "description": agent.description,
        "capabilities": list(agent.capabilities or []),
        "enabled": agent.enabled,
        "max_concurrency": agent.max_concurrency,
        "reserved_execution_count": agent.reserved_execution_count,
        "available_capacity": agent.available_capacity,
        "deleted_at": iso(agent.deleted_at),
        "deleted_reason": agent.deleted_reason or "",
        "last_heartbeat_at": iso(agent.last_heartbeat_at),
        "online_state": online_state,
        "version": agent.version,
        "created_at": iso(agent.created_at),
        "updated_at": iso(agent.updated_at),
    }


def project_summary(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.pk),
        "title": project.title,
        "summary": project.summary,
        "status": project.status,
        "archived_from_status": project.archived_from_status,
        "deleted_at": iso(project.deleted_at),
        "deleted_reason": project.deleted_reason or "",
        "content_version": project.content_version,
        "version": project.version,
        "created_at": iso(project.created_at),
        "updated_at": iso(project.updated_at),
    }


def project_detail(project: Project) -> dict[str, Any]:
    data = project_summary(project)
    data["background"] = project.background
    return data


def context_version_view(context: ProjectContextVersion) -> dict[str, Any]:
    return {
        "id": str(context.pk),
        "project_id": str(context.project_id),
        "number": context.number,
        "background_snapshot": context.background_snapshot,
        "document_version_refs": list(context.document_version_refs or []),
        "created_at": iso(context.created_at),
    }


def document_view(document: Document, *, current: DocumentVersion | None = None) -> dict[str, Any]:
    version = current if current is not None else document.current_version
    return {
        "id": str(document.pk),
        "project_id": str(document.project_id),
        "name": document.name,
        "is_archived": document.is_archived,
        "current_version_id": uuid_str(document.current_version_id),
        "current_version_number": None if version is None else version.number,
        "file_id": uuid_str(version.file_asset_id) if version else None,
        "created_at": iso(document.created_at),
        "updated_at": iso(document.updated_at),
    }


def document_version_view(version: DocumentVersion) -> dict[str, Any]:
    asset = version.file_asset
    return {
        "id": str(version.pk),
        "document_id": str(version.document_id),
        "number": version.number,
        "file_id": str(version.file_asset_id),
        "original_name": asset.original_name,
        "media_type": asset.media_type,
        "size": asset.size,
        "sha256": asset.sha256,
        "oss_url": oss_storage.asset_url(asset),
        "uploaded_by_type": version.uploaded_by_type,
        "uploaded_by_agent_id": uuid_str(version.uploaded_by_agent_id),
        "created_at": iso(version.created_at),
    }


def task_summary(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.pk),
        "project_id": str(task.project_id),
        "number": task.number,
        "display_number": display_number(task.number),
        "title": task.title,
        "difficulty": task.difficulty,
        "difficulty_name": DIFFICULTY_NAMES.get(task.difficulty),
        "status": task.status,
        "claim_mode": task.claim_mode,
        "designated_agent_id": uuid_str(task.designated_agent_id),
        "assignee_agent_id": uuid_str(task.assignee_agent_id),
        "active_execution_id": uuid_str(task.active_execution_id),
        "current_submission_id": uuid_str(task.current_submission_id),
        "sort_order": str(task.sort_order),
        "due_at": iso(task.due_at),
        "version": task.version,
        "requirements_version": task.requirements_version,
        "is_archived": task.is_archived,
        "labels": list(task.labels or []),
        "created_at": iso(task.created_at),
        "published_at": iso(task.published_at),
        "claimed_at": iso(task.claimed_at),
        "submitted_at": iso(task.submitted_at),
        "completed_at": iso(task.completed_at),
    }


def task_detail(task: Task) -> dict[str, Any]:
    data = task_summary(task)
    data.update(
        {
            "description": task.description,
            "acceptance_criteria": task.acceptance_criteria,
            "execution_timeout_seconds": DIFFICULTY_TIMEOUT_SECONDS.get(task.difficulty),
            "execution_deadline_at": iso(_active_deadline(task)),
            # Count distinct FILES, not link rows: a submitted artifact carries both an
            # execution link and a submission link, and counting rows would report one
            # upload as two attachments on the card.
            "attachment_count": (
                task.attachment_links.values("file_asset_id").distinct().count()
                if hasattr(task, "attachment_links")
                else 0
            ),
        }
    )
    return data


def _active_deadline(task: Task):
    execution = getattr(task, "active_execution", None)
    return None if execution is None else execution.execution_deadline_at


def execution_view(execution: TaskExecution) -> dict[str, Any]:
    return {
        "execution_id": str(execution.pk),
        "task_id": str(execution.task_id),
        "agent_id": str(execution.agent_id),
        "sequence": execution.sequence,
        "difficulty": execution.difficulty_snapshot,
        "execution_timeout_seconds": execution.execution_timeout_seconds,
        "claimed_at": iso(execution.claimed_at),
        "execution_deadline_at": iso(execution.execution_deadline_at),
        "ended_at": iso(execution.ended_at),
        "end_reason": execution.end_reason,
        "progress_message": execution.progress_message,
        "progress_percent": execution.progress_percent,
        "progress_updated_at": iso(execution.progress_updated_at),
        "context_version_id": uuid_str(execution.context_version_id),
        "requirements_snapshot": execution.requirements_snapshot or {},
    }


def submission_view(submission: TaskSubmission) -> dict[str, Any]:
    return {
        "submission_id": str(submission.pk),
        "task_id": str(submission.task_id),
        "execution_id": str(submission.execution_id),
        "summary": submission.summary,
        "content": submission.content,
        "self_check": list(submission.self_check or []),
        "artifact_refs": list(submission.artifact_refs or []),
        "links": list(submission.links or []),
        "requirements_version": submission.requirements_version,
        "context_version_id": uuid_str(submission.context_version_id),
        "submitted_by_agent_id": str(submission.submitted_by_agent_id),
        "submitted_at": iso(submission.submitted_at),
    }


def review_view(review: TaskReview) -> dict[str, Any]:
    return {
        "review_id": str(review.pk),
        "submission_id": str(review.submission_id),
        "task_id": str(review.task_id),
        "decision": review.decision,
        "comment": review.comment,
        "actor_type": review.actor_type,
        "reviewer_agent_id": uuid_str(review.reviewer_agent_id),
        "reviewer_identity_verified": review.reviewer_identity_verified,
        "created_at": iso(review.created_at),
    }


def activity_view(event) -> dict[str, Any]:
    return {
        "id": str(event.pk),
        "project_id": uuid_str(event.project_id),
        "task_id": uuid_str(event.task_id),
        "execution_id": uuid_str(event.execution_id),
        "submission_id": uuid_str(event.submission_id),
        "actor_type": event.actor_type,
        "actor_agent_id": uuid_str(event.actor_agent_id),
        "action": event.action,
        "before": event.before or {},
        "after": event.after or {},
        "reason": event.reason,
        "request_id": event.request_id,
        "created_at": iso(event.created_at),
    }


def file_asset_view(asset) -> dict[str, Any]:
    return {
        "id": str(asset.pk),
        "file_id": str(asset.pk),
        "original_name": asset.original_name,
        "media_type": asset.media_type,
        "size": asset.size,
        "sha256": asset.sha256,
        "uploader_type": asset.uploader_type,
        "uploader_agent_id": uuid_str(asset.uploader_agent_id),
        "project_id": uuid_str(asset.project_id),
        "oss_url": oss_storage.asset_url(asset),
        "created_at": iso(asset.created_at),
    }


def attachment_view(link) -> dict[str, Any]:
    return {
        "id": str(link.pk),
        "file_id": str(link.file_asset_id),
        "original_name": link.file_asset.original_name,
        "media_type": link.file_asset.media_type,
        "size": link.file_asset.size,
        "purpose": link.purpose,
        "task_id": uuid_str(link.task_id),
        "execution_id": uuid_str(link.execution_id),
        "submission_id": uuid_str(link.submission_id),
        "oss_url": oss_storage.asset_url(link.file_asset),
        "created_at": iso(link.created_at),
    }


def page(items: list[dict[str, Any]], total: int, page_number: int, page_size: int) -> dict[str, Any]:
    return {"items": items, "total": total, "page": page_number, "page_size": page_size}
