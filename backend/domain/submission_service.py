"""Formal, non-overwritable result submission (DEVELOP D14, product 7.1/7.4/8).

The submission row, its frozen artifact references, the execution end, the capacity
release, the IN_REVIEW transition, the activity event, the outbox event and the receipt all
commit in one transaction. A successful submission that lost its response can be replayed
after the original deadline and still return the original receipt.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from django.db import IntegrityError, transaction

from apps.files.models import AttachmentLink, FileAsset
from apps.tasks.models import Task, TaskExecution, TaskSubmission
from domain.clock import db_now
from domain.contracts import (
    OP_TASK_SUBMIT,
    ActorContext,
    ActorType,
    AttachmentPurpose,
    EndReason,
    TaskStatus,
    route_operation,
)
from domain.errors import (
    AttachmentLimitExceededError,
    InvalidArgumentError,
    ResourceNotFoundError,
    RoleForbiddenError,
    SubmissionExistsError,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.execution import finish_execution_locked
from domain.idempotency import canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan
from domain.permissions import accessible_project_ids, load_actor_agent_locked, require_membership
from domain.presenters import execution_view, iso, submission_view, task_summary

logger = logging.getLogger("domain.submission")


def submit_result(
    actor: ActorContext,
    *,
    task_id: uuid.UUID,
    payload: dict,
    idem_key: str,
) -> dict:
    """Submit the one formal result for the current execution."""
    if actor.actor_type is ActorType.AGENT:
        agent = load_actor_agent_locked(actor)
    elif actor.actor_type is ActorType.WEB_OPERATOR:
        # The local administrator may submit on behalf of the executing agent; the audit
        # event keeps actor_type=web_operator so a proxied submission is never recorded as
        # an Agent-authenticated action.
        agent = None
    else:
        raise RoleForbiddenError("system 来源不能提交结果")

    summary = _require_text(payload, "summary", max_length=200)
    content = str(payload.get("content") or "")
    if len(content) > 50000:
        raise InvalidArgumentError("content 超过长度上限 50000")
    self_check = _validate_self_check(payload.get("self_check"))
    links = _validate_links(payload.get("links"))
    artifact_ids = _validate_artifact_ids(payload.get("artifacts"))

    expected_version = payload.get("expected_version")
    if expected_version is None or isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise InvalidArgumentError("expected_version 必填且必须是整数")
    execution_id = _parse_uuid(payload.get("execution_id"), "execution_id")

    operation = route_operation(OP_TASK_SUBMIT, "agent")
    request_hash = canonical_request_hash(
        operation=operation,
        body={
            "summary": summary,
            "content": content,
            "self_check": self_check,
            "links": links,
            "artifacts": [str(item) for item in artifact_ids],
            "expected_version": expected_version,
            "execution_id": str(execution_id),
        },
        resource_id=task_id,
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return {**(receipt.response_data or {}), "idempotent_replay": True}

    project_ids = accessible_project_ids(agent.pk) if agent is not None else None

    plan = LockPlan()
    plan.add_task(task_id)
    plan.add_execution(execution_id)
    if agent is not None:
        plan.add_agent(agent.pk)
    hint = Task.objects.filter(pk=task_id).values("project_id").first()
    if hint:
        plan.add_project(hint["project_id"])
    execution_agent = TaskExecution.objects.filter(pk=execution_id).values_list("agent_id", flat=True).first()
    if execution_agent is not None:
        plan.add_agent(execution_agent)
    locked = acquire_plan(plan)

    task = locked.tasks.get(task_id)
    execution = locked.executions.get(execution_id)
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    if project_ids is not None and task.project_id not in project_ids:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    if agent is not None:
        require_membership(agent.pk, task.project_id)
    if execution is None:
        raise VersionConflictError("执行 ID 不属于该任务")

    if task.status != TaskStatus.IN_PROGRESS.value:
        raise VersionConflictError(
            "任务当前不是进行中，无法提交", details={"task_status": task.status}
        )
    if task.active_execution_id != execution.pk:
        raise VersionConflictError("执行 ID 不是当前有效执行")
    if agent is not None and execution.agent_id != agent.pk:
        raise RoleForbiddenError("只能提交自己持有的当前执行")
    if execution.ended_at is not None:
        raise VersionConflictError("当前执行已经结束")
    if task.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": task.version}
        )

    # The database clock is read after the locks: a request that queued across the deadline
    # is late, exactly like the product contract requires.
    now = db_now()
    if now >= execution.execution_deadline_at:
        from domain.errors import ExecutionTimeoutError

        raise ExecutionTimeoutError(
            details={
                "execution_deadline_at": iso(execution.execution_deadline_at),
                "now": iso(now),
            }
        )

    if artifact_ids:
        _verify_artifact_ownership(artifact_ids, execution, task)

    try:
        with transaction.atomic():
            submission = TaskSubmission.objects.create(
                execution=execution,
                task=task,
                summary=summary,
                content=content,
                self_check=self_check,
                artifact_refs=[str(item) for item in artifact_ids],
                links=links,
                requirements_version=task.requirements_version,
                context_version=execution.context_version,
                # The submission belongs to the executing agent even when the local
                # administrator proxies it; the proxied source is recorded on the activity
                # event (actor_type=web_operator), not by nulling the submitter.
                submitted_by_agent_id=execution.agent_id,
                submitted_at=now,
            )
    except IntegrityError as exc:
        # The savepoint above was rolled back, so the connection is usable again and the
        # failure can be classified. Mapping every IntegrityError to "already submitted"
        # previously reported a NOT NULL violation as a duplicate submission.
        if TaskSubmission.objects.filter(execution_id=execution.pk).exists():
            raise SubmissionExistsError(details={"execution_id": str(execution.pk)}) from exc
        logger.exception("创建正式提交失败，非重复提交原因")
        raise

    # Only the execution is finished here; the wrapper (not the primitive) writes IN_REVIEW.
    if not finish_execution_locked(
        execution, ended_at=now, end_reason=EndReason.SUBMIT.value
    ):  # pragma: no cover - guarded above
        raise VersionConflictError("当前执行已被结束，无法提交")

    updated = Task.objects.filter(
        pk=task.pk,
        status=TaskStatus.IN_PROGRESS.value,
        active_execution_id=execution.pk,
        version=expected_version,
    ).update(
        status=TaskStatus.IN_REVIEW.value,
        active_execution_id=None,
        current_submission_id=submission.pk,
        submitted_at=now,
        version=expected_version + 1,
    )
    if updated != 1:
        raise VersionConflictError("任务状态或版本已变化，提交未生效")

    _freeze_artifacts(artifact_ids, submission, task, execution, now)
    task.refresh_from_db()

    record_activity(
        actor,
        "task.submitted",
        task_id=task.pk,
        project_id=task.project_id,
        execution_id=execution.pk,
        submission_id=submission.pk,
        before={"status": TaskStatus.IN_PROGRESS.value},
        after={"status": task.status, "submission_id": str(submission.pk)},
        reason=summary,
    )
    enqueue_outbox(
        "task.submitted",
        task.pk,
        {
            "task_id": str(task.pk),
            "project_id": str(task.project_id),
            "execution_id": str(execution.pk),
            "submission_id": str(submission.pk),
        },
    )

    result = {
        "data": {
            "task_id": str(task.pk),
            "status": task.status,
            "version": task.version,
            "submission_id": str(submission.pk),
            "submitted_at": iso(submission.submitted_at),
            "assignee_agent_id": str(task.assignee_agent_id),
            "task": task_summary(task),
            "submission": submission_view(submission),
            "execution": execution_view(execution),
        }
    }
    complete_receipt(receipt, response_data=result, resource_type="task", resource_id=task.pk)
    return {**result, "idempotent_replay": False}


def _freeze_artifacts(artifact_ids, submission, task, execution, now) -> None:
    """Mark submitted files as frozen submission artifacts and bind their references."""
    for file_id in artifact_ids:
        AttachmentLink.objects.create(
            file_asset_id=file_id,
            project_id=task.project_id,
            task_id=task.pk,
            execution_id=execution.pk,
            submission_id=submission.pk,
            purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value,
        )
        FileAsset.objects.filter(pk=file_id, bound_at__isnull=True).update(bound_at=now)


def _verify_artifact_ownership(artifact_ids, execution, task) -> None:
    """Artifacts must be files this execution actually uploaded for this task/project."""
    from django.conf import settings

    limit = settings.HARNESSTASK["max_submission_artifacts"]
    if len(artifact_ids) > limit:
        raise AttachmentLimitExceededError(
            f"单次提交最多 {limit} 个产物", details={"limit": limit, "count": len(artifact_ids)}
        )

    found = set(
        AttachmentLink.objects.filter(
            file_asset_id__in=artifact_ids,
            execution_id=execution.pk,
            task_id=task.pk,
            project_id=task.project_id,
            purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        ).values_list("file_asset_id", flat=True)
    )
    missing = [str(file_id) for file_id in artifact_ids if file_id not in found]
    if missing:
        # Never accept an arbitrary disk path or another agent's file id.
        raise InvalidArgumentError(
            "提交引用的产物不属于当前执行",
            details={"invalid_artifact_ids": missing},
        )


def _validate_artifact_ids(raw) -> list[uuid.UUID]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidArgumentError("artifacts 必须是数组")
    result = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("file_id")
        result.append(_parse_uuid(item, "artifacts[].file_id"))
    if len(set(result)) != len(result):
        raise InvalidArgumentError("artifacts 中存在重复文件")
    return result


def _validate_links(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidArgumentError("links 必须是数组")
    links = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidArgumentError("links 的每一项必须是对象")
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title:
            raise InvalidArgumentError("links[].title 不能为空")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            # External links are stored as text references only; they are never fetched.
            raise InvalidArgumentError("links[].url 必须是 http 或 https 外链")
        links.append({"title": title, "url": url})
    return links


def _validate_self_check(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidArgumentError("self_check 必须是数组")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InvalidArgumentError("self_check 的每一项必须是对象")
        criterion = str(entry.get("criterion") or "").strip()
        if not criterion:
            raise InvalidArgumentError("self_check[].criterion 不能为空")
        items.append(
            {
                "criterion": criterion,
                "passed": bool(entry.get("passed", False)),
                "evidence": str(entry.get("evidence") or ""),
            }
        )
    return items


def _require_text(payload: dict, name: str, *, max_length: int) -> str:
    value = payload.get(name)
    if value is None or not str(value).strip():
        raise InvalidArgumentError(f"缺少必需字段 {name}", details={"field": name})
    value = str(value).strip()
    if len(value) > max_length:
        raise InvalidArgumentError(f"字段 {name} 超过长度上限 {max_length}")
    return value


def _parse_uuid(value, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(f"字段 {field} 必须是合法 UUID", details={"field": field}) from exc


def submission_history(task: Task) -> list[dict]:
    return [
        submission_view(item)
        for item in TaskSubmission.objects.filter(task=task).order_by("submitted_at", "id")
    ]


def execution_history(task: Task) -> list[dict]:
    return [
        execution_view(item)
        for item in TaskExecution.objects.filter(task=task).order_by("sequence")
    ]
