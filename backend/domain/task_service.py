"""Task authoring, publish/withdraw, assignment, archive/restore and activity (DEVELOP D10).

Five states are driven by explicit business actions, never by writing ``status`` directly:

* create always yields ``PREPARING`` and never publishes implicitly;
* ``publish`` (PREPARING -> TODO) checks the project is ACTIVE and the requirements are
  complete, and re-validates an ASSIGNED designated agent;
* ``withdraw`` (TODO -> PREPARING) refuses while a current execution exists and requires a
  reason, so a claimed task's requirements can only be changed after the execution ended;
* ``assign`` changes OPEN/ASSIGNED without creating an execution, reserving capacity or
  starting the difficulty timer;
* archive is an independent flag: it never changes the five-state status.

Version and requirements version are separate: substantive edits bump ``requirements_version``
so a claimed/submitted execution snapshot can never silently drift, while every real action
bumps ``version`` exactly once and is guarded by ``expected_version``.

Every action runs through ``domain.service_runner`` (outermost transaction + bounded retry),
requires an idempotency receipt and writes exactly one ActivityEvent plus one OutboxEvent per
real action (never on a replay).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Max

from apps.files.models import AttachmentLink, FileAsset
from apps.identity.models import Agent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import SORT_ORDER_STEP, Task
from domain.bulk import bulk_result, parse_bulk_ids, run_bulk
from domain.clock import db_now
from domain.contracts import (
    OP_AGENT_TASK_CREATE,
    OP_TASK_ARCHIVE,
    OP_TASK_ASSIGN,
    OP_TASK_PUBLISH,
    OP_TASK_RESTORE,
    OP_TASK_WITHDRAW,
    OP_WEB_TASK_CREATE,
    ActorContext,
    ActorType,
    AttachmentPurpose,
    ClaimMode,
    ProjectStatus,
    TaskStatus,
)
from domain.errors import (
    AttachmentLimitExceededError,
    InvalidArgumentError,
    InvalidTransitionError,
    ProjectNotActiveError,
    ResourceNotFoundError,
    RetryTransaction,
    VersionConflictError,
)
from domain.events import enqueue_outbox, record_activity
from domain.idempotency import Receipt, canonical_request_hash, complete_receipt, lookup_or_reserve
from domain.locking import LockPlan, acquire_plan, require
from domain import oss_storage
from domain.permissions import require_project_active, require_project_supervisor, require_task_supervisor
from domain.presenters import iso, uuid_str
from domain.read_task_query import task_detail_payload

logger = logging.getLogger("domain.task")

TITLE_MAX_LENGTH = 200
TEXT_MAX_LENGTH = 50_000
LABEL_MAX_LENGTH = 50
MAX_LABELS = 20

# The full PATCH allow-list (CONTRACT T13). ``claim_mode``, ``sort_order``, ``project_id``,
# status, assignment and execution references are never editable here: claim mode has its own
# action, ordering is a separate endpoint and the rest are business-action outcomes.
TASK_EDITABLE_FIELDS = {
    "title",
    "description",
    "acceptance_criteria",
    "difficulty",
    "due_at",
    "labels",
}

# Substantive fields change the execution standard. In PREPARING they bump
# ``requirements_version``; from TODO onwards they require a withdraw (or are refused).
SUBSTANTIVE_FIELDS = {"title", "description", "acceptance_criteria", "difficulty"}

_EDITABLE_BY_STATUS: dict[str, set[str]] = {
    TaskStatus.PREPARING.value: set(TASK_EDITABLE_FIELDS),
    # TODO may only change non-substantive fields; requirements need a withdraw first.
    TaskStatus.TODO.value: {"title", "due_at", "labels"},
    # Once an execution exists the requirements snapshot is frozen; only these may change.
    TaskStatus.IN_PROGRESS.value: {"due_at", "labels"},
    TaskStatus.IN_REVIEW.value: {"due_at", "labels"},
    TaskStatus.DONE.value: {"due_at", "labels"},
}

CREATE_FIELDS = {
    "project_id",
    "title",
    "description",
    "acceptance_criteria",
    "difficulty",
    "claim_mode",
    "designated_agent_id",
    "due_at",
    "labels",
    "attachment_file_ids",
}


# --------------------------------------------------------------------------------------
# Small payload helpers (shared by both API surfaces so validation has one implementation)
# --------------------------------------------------------------------------------------


def require_expected_version(payload: dict[str, Any]) -> int:
    """Read ``expected_version`` from a request body. Missing or malformed -> 400."""
    if not isinstance(payload, dict) or "expected_version" not in payload:
        raise InvalidArgumentError(
            "缺少必需字段 expected_version", details={"field": "expected_version"}
        )
    return validate_expected_version(payload["expected_version"])


def validate_expected_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError(
            "expected_version 必须是正整数", details={"field": "expected_version", "value": value}
        )
    if value < 1:
        raise InvalidArgumentError(
            "expected_version 必须大于等于 1", details={"field": "expected_version", "value": value}
        )
    return value


def require_reason(payload: dict[str, Any], *, field: str = "reason") -> str:
    raw = payload.get(field) if isinstance(payload, dict) else None
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise InvalidArgumentError(f"缺少必填字段 {field}", details={"field": field})
    return raw.strip()


def resolve_claim_assignment(
    *, claim_mode: Any, designated_agent_id: Any, project_id
) -> Agent | None:
    """Validate an OPEN/ASSIGNED pair against live Agent state (read-only).

    Returns the designated Agent row so the caller can add it to its LockPlan; the caller
    re-validates it after locking. A disabled agent or a non-member is an argument error, not
    a lost version race, so it answers 400 instead of 409.
    """
    if claim_mode not in (ClaimMode.OPEN.value, ClaimMode.ASSIGNED.value):
        raise InvalidArgumentError(
            "claim_mode 只能是 OPEN 或 ASSIGNED",
            details={"field": "claim_mode", "value": claim_mode},
        )
    if claim_mode == ClaimMode.OPEN.value:
        if designated_agent_id is not None:
            raise InvalidArgumentError(
                "OPEN 模式下不能指定 designated_agent_id",
                details={"field": "designated_agent_id"},
            )
        return None

    agent_uuid = parse_agent_id(designated_agent_id)
    if agent_uuid is None:
        raise InvalidArgumentError(
            "ASSIGNED 模式必须提供 designated_agent_id", details={"field": "designated_agent_id"}
        )
    agent = Agent.objects.filter(pk=agent_uuid).first()
    if agent is None:
        raise ResourceNotFoundError(
            "指定的 Agent 不存在", details={"agent_id": str(agent_uuid)}
        )
    _require_assignable_agent(agent, project_id)
    return agent


def parse_agent_id(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(
            "designated_agent_id 必须是合法 UUID",
            details={"field": "designated_agent_id", "value": value},
        ) from exc


def _require_assignable_agent(agent: Agent, project_id) -> None:
    if not agent.enabled:
        raise InvalidArgumentError(
            "指定的 Agent 已被禁用", details={"agent_id": str(agent.pk), "enabled": False}
        )
    if not ProjectMember.objects.filter(project_id=project_id, agent_id=agent.pk).exists():
        raise InvalidArgumentError(
            "指定的 Agent 不是该项目成员",
            details={"agent_id": str(agent.pk), "project_id": str(project_id)},
        )


def _optional_text(payload: dict[str, Any], field: str, *, max_length: int) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidArgumentError(f"字段 {field} 必须是字符串", details={"field": field})
    value = value.strip()
    if len(value) > max_length:
        raise InvalidArgumentError(
            f"字段 {field} 超过长度上限 {max_length}",
            details={"field": field, "max_length": max_length},
        )
    return value


def _require_title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError("缺少必填字段 title", details={"field": "title"})
    title = value.strip()
    if len(title) > TITLE_MAX_LENGTH:
        raise InvalidArgumentError(
            f"title 超过长度上限 {TITLE_MAX_LENGTH}",
            details={"field": "title", "max_length": TITLE_MAX_LENGTH},
        )
    return title


def _require_difficulty(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError("difficulty 必须是 1 到 5 的整数", details={"field": "difficulty"})
    if value < 1 or value > 5:
        raise InvalidArgumentError(
            "difficulty 必须在 1 到 5 之间", details={"field": "difficulty", "value": value}
        )
    return value


def _optional_due_at(payload: dict[str, Any], field: str = "due_at"):
    if field not in payload:
        return None
    value = payload[field]
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InvalidArgumentError(
            "due_at 必须是 ISO 8601 字符串", details={"field": "due_at", "value": value}
        )
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidArgumentError(
            "due_at 必须是 ISO 8601 字符串", details={"field": "due_at", "value": value}
        ) from exc
    if parsed.tzinfo is None:
        # A naive value is read as UTC instead of letting Django guess from the process zone.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_labels(payload: dict[str, Any], field: str = "labels") -> list[str] | None:
    if field not in payload:
        return None
    value = payload[field]
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidArgumentError("labels 必须是字符串数组", details={"field": "labels"})
    if len(value) > MAX_LABELS:
        raise InvalidArgumentError(
            f"labels 数量超过上限 {MAX_LABELS}", details={"field": "labels", "max": MAX_LABELS}
        )
    labels: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidArgumentError(
                "labels 每一项必须是非空字符串", details={"field": "labels", "value": item}
            )
        label = item.strip()
        if len(label) > LABEL_MAX_LENGTH:
            raise InvalidArgumentError(
                f"labels 单项超过长度上限 {LABEL_MAX_LENGTH}", details={"field": "labels"}
            )
        labels.append(label)
    return labels


def _reject_unknown_fields(payload: dict[str, Any], allowed: set[str], *, surface: str) -> None:
    unknown = sorted(name for name in payload if name not in allowed)
    if unknown:
        raise InvalidArgumentError(
            "请求包含不允许的字段",
            details={"surface": surface, "fields": unknown},
        )


# --------------------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------------------


def operation_for(template: str, actor: ActorContext) -> str:
    """Resolve the frozen ``{surface}`` placeholder for the idempotency operation scope."""
    surface = "agent" if actor.actor_type is ActorType.AGENT else "web"
    return template.replace("{surface}", surface)


def _create_operation(actor: ActorContext) -> str:
    return OP_AGENT_TASK_CREATE if actor.actor_type is ActorType.AGENT else OP_WEB_TASK_CREATE


def _replay_payload(receipt: Receipt) -> dict:
    """Return the stored receipt of an identical earlier request."""
    stored = receipt.response_data if isinstance(receipt.response_data, dict) else {}
    return {**stored, "idempotent_replay": True}


def _load_project(project_id) -> Project:
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        raise ResourceNotFoundError("项目不存在或当前身份无权访问")
    return project


def _require_project_authorable(project: Project) -> None:
    """A task may only be authored while the project accepts writes.

    ARCHIVED (no writes at all) and COMPLETED (activate first, DEVELOP D08) answer
    ``409 PROJECT_NOT_ACTIVE``; DRAFT and ACTIVE accept authoring, and publish additionally
    demands ACTIVE through ``permissions.require_project_active``.
    """
    if project.status in (ProjectStatus.ARCHIVED.value, ProjectStatus.COMPLETED.value):
        raise ProjectNotActiveError(details={"project_status": project.status})


def _load_task_or_404(task_id) -> Task:
    task = Task.objects.filter(pk=task_id).first()
    if task is None:
        raise ResourceNotFoundError("任务不存在或当前身份无权访问")
    return task


def _authorize_task_action(actor: ActorContext, task: Task) -> None:
    """Authoring/publishing/withdrawing/assigning/archiving is a supervisor action."""
    require_task_supervisor(actor, task)


def _lock_plan(project_id, *, task_id=None, agent_ids: tuple = ()) -> LockPlan:
    plan = LockPlan()
    plan.add_project(project_id)
    plan.add_task(task_id)
    for agent_id in agent_ids:
        plan.add_agent(agent_id)
    return plan


def _locked_task(plan: LockPlan, locked, task_id) -> Task:
    require(plan.contains_task(task_id) and task_id in locked.tasks, "任务不在锁计划内或已被删除")
    return locked.tasks[task_id]


def _locked_project(plan: LockPlan, locked, project_id) -> Project:
    require(
        plan.contains_project(project_id) and project_id in locked.projects,
        "项目不在锁计划内或已被删除",
    )
    return locked.projects[project_id]


def _insert_task_row(**kwargs) -> Task:
    """Insert the task row, translating a number collision into a whole-service retry.

    The unique ``(project, number)`` constraint is the last line of defence: a concurrent
    create that slipped past the project lock must roll this transaction back and re-run the
    service (which re-reads ``max(number)``), never write a duplicate or silently renumber.
    """
    try:
        with transaction.atomic():  # legal savepoint: the outer transaction stays usable
            return Task.objects.create(**kwargs)
    except IntegrityError as exc:
        logger.info("任务编号竞争，整笔回滚后由服务重试：%s", exc)
        raise RetryTransaction("任务编号分配冲突，请重试") from exc


def _cas_update(task: Task, expected_version: int, *, fields: dict[str, Any]) -> Task:
    """Conditional UPDATE on ``version`` so two writers can never both win."""
    now = db_now()
    rows = (
        Task.objects.filter(pk=task.pk, version=expected_version)
        .update(**fields, version=F("version") + 1, updated_at=now)
    )
    if rows != 1:
        current = Task.objects.filter(pk=task.pk).values_list("version", flat=True).first()
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": current}
        )
    task.refresh_from_db()
    return task


def _require_version(task: Task, expected_version: int) -> None:
    if task.version != expected_version:
        raise VersionConflictError(
            details={"expected_version": expected_version, "current_version": task.version}
        )


def _activity(actor: ActorContext, action: str, task: Task, **kwargs) -> None:
    record_activity(actor, action, project_id=task.project_id, task_id=task.pk, **kwargs)


def _outbox(event_type: str, task: Task, payload: dict[str, Any]) -> None:
    enqueue_outbox(event_type, task.pk, payload)


# --------------------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------------------


def create_task(actor: ActorContext, payload: dict[str, Any], idem_key: str) -> dict:
    """Create a PREPARING task with ``version=1``; never publishes implicitly."""
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, CREATE_FIELDS, surface="task.create")

    project_id = _required_project_id(payload)
    title = _require_title(payload.get("title"))
    difficulty = _require_difficulty(payload.get("difficulty"))
    description = _optional_text(payload, "description", max_length=TEXT_MAX_LENGTH) or ""
    acceptance = _optional_text(payload, "acceptance_criteria", max_length=TEXT_MAX_LENGTH) or ""
    due_at = _optional_due_at(payload)
    labels = _optional_labels(payload) or []
    claim_mode = payload.get("claim_mode", ClaimMode.OPEN.value)

    project = _load_project(project_id)
    require_project_supervisor(actor, project)
    _require_project_authorable(project)
    attachment_assets = _load_staging_attachments(actor, payload.get("attachment_file_ids"), project)
    description = _compose_description_with_attachments(description, attachment_assets)
    designated = resolve_claim_assignment(
        claim_mode=claim_mode,
        designated_agent_id=payload.get("designated_agent_id"),
        project_id=project.pk,
    )

    operation = _create_operation(actor)
    request_hash = canonical_request_hash(
        operation=operation, body=payload, project_id=project.pk
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    plan = _lock_plan(
        project.pk,
        agent_ids=tuple(
            agent_id
            for agent_id in (
                None if designated is None else designated.pk,
                actor.agent_id,
            )
            if agent_id is not None
        ),
    )
    locked = acquire_plan(plan)
    locked_project = _locked_project(plan, locked, project.pk)
    _require_project_authorable(locked_project)
    if designated is not None:
        locked_agent = locked.agents.get(designated.pk)
        require(locked_agent is not None, "指定 Agent 不在锁计划内或已被删除")
        _require_assignable_agent(locked_agent, locked_project.pk)

    number = _next_task_number(locked_project.pk)
    task = _insert_task_row(
        project=locked_project,
        number=number,
        title=title,
        description=description,
        acceptance_criteria=acceptance,
        difficulty=difficulty,
        status=TaskStatus.PREPARING.value,
        claim_mode=claim_mode,
        designated_agent=designated,
        sort_order=Decimal(number) * SORT_ORDER_STEP,
        due_at=due_at,
        labels=labels,
        created_by_agent_id=actor.agent_id if actor.actor_type is ActorType.AGENT else None,
    )

    _activity(
        actor,
        "created",
        task,
        after={
            "number": task.number,
            "status": task.status,
            "difficulty": task.difficulty,
            "claim_mode": task.claim_mode,
            "designated_agent_id": uuid_str(task.designated_agent_id),
            "version": task.version,
        },
    )
    _outbox(
        "task.created",
        task,
        {
            "task_id": str(task.pk),
            "project_id": str(task.project_id),
            "number": task.number,
            "status": task.status,
        },
    )

    if attachment_assets:
        _bind_created_attachments(actor, task, attachment_assets)

    response_data = task_detail_payload(task)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=task.pk
    )
    return {**response_data, "idempotent_replay": False}


def _load_staging_attachments(actor: ActorContext, raw_ids: Any, project: Project) -> list[FileAsset]:
    """Load, own and bound-check the staging files referenced by ``attachment_file_ids``.

    Only files this caller uploaded (agents) or the Web operator staged, still unbound and
    scoped to this project may become task references. Limits reuse the shared
    ``max_task_reference_attachments`` cap because these files become TASK_REFERENCE links.
    """
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list) or not raw_ids:
        raise InvalidArgumentError(
            "attachment_file_ids 必须是非空 UUID 数组", details={"field": "attachment_file_ids"}
        )
    limit = settings.HARNESSTASK["max_task_reference_attachments"]
    if len(raw_ids) > limit:
        raise AttachmentLimitExceededError(
            "任务参考附件数量超过限制",
            details={"limit": limit, "requested": len(raw_ids)},
        )

    parsed: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            parsed.append(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidArgumentError(
                "attachment_file_ids 包含非法 UUID",
                details={"field": "attachment_file_ids", "value": str(raw)[:100]},
            ) from exc
    deduped = list(dict.fromkeys(parsed))
    if len(deduped) != len(parsed):
        raise InvalidArgumentError(
            "attachment_file_ids 不得包含重复项", details={"field": "attachment_file_ids"}
        )

    assets = list(FileAsset.objects.filter(pk__in=deduped))
    by_id = {asset.pk: asset for asset in assets}
    for file_id in deduped:
        asset = by_id.get(file_id)
        if asset is None or asset.bound_at is not None:
            raise ResourceNotFoundError("上传文件不存在或已被绑定", details={"file_id": str(file_id)})
        if asset.project_id is not None and asset.project_id != project.pk:
            raise ResourceNotFoundError(
                "上传文件不属于该项目", details={"file_id": str(file_id)}
            )
        if actor.actor_type is ActorType.AGENT and asset.uploader_agent_id != actor.agent_id:
            raise ResourceNotFoundError(
                "只能引用本人上传的暂存文件", details={"file_id": str(file_id)}
            )
    return [by_id[file_id] for file_id in deduped]


def _compose_description_with_attachments(description: str, assets: list[FileAsset]) -> str:
    """Append the OSS reference section to the prompt text (the task description).

    Agents receive this text verbatim in ``requirements_snapshot.description`` at claim
    time, so the signed OSS links are part of the distributed prompt. Files without an
    OSS copy (OSS disabled) are listed without a link so the section never lies.
    """
    lines = []
    for asset in assets:
        url = oss_storage.asset_url(asset)
        if url:
            lines.append(f"- {asset.original_name}: {url}")
    if not lines:
        return description
    composed = (
        f"{description.rstrip()}\n\n## 参考附件（OSS 下载链接，可直接下载）\n" + "\n".join(lines) + "\n"
    )
    if len(composed) > TEXT_MAX_LENGTH:
        raise InvalidArgumentError(
            f"description 连同附件下载链接超过 {TEXT_MAX_LENGTH} 字符上限",
            details={"field": "description", "length": len(composed)},
        )
    return composed


def _bind_created_attachments(actor: ActorContext, task: Task, assets: list[FileAsset]) -> None:
    """Bind staged uploads to a just-created task as TASK_REFERENCE links.

    Runs inside create_task's transaction: the same rows an explicit
    ``POST /tasks/{id}/attachments`` would produce (link, bound_at, activity, outbox),
    without re-opening the idempotency machinery the task create already reserved.
    """
    now = db_now()
    for asset in assets:
        link = AttachmentLink.objects.create(
            file_asset=asset,
            project_id=task.project_id,
            task=task,
            execution=None,
            submission=None,
            purpose=AttachmentPurpose.TASK_REFERENCE.value,
        )
        FileAsset.objects.filter(pk=asset.pk, bound_at__isnull=True).update(
            bound_at=now, project_id=task.project_id
        )
        asset.bound_at = now
        record_activity(
            actor,
            "attachment.upload",
            project_id=task.project_id,
            task_id=task.pk,
            before={},
            after={
                "purpose": AttachmentPurpose.TASK_REFERENCE.value,
                "file_id": str(asset.pk),
                "original_name": asset.original_name,
                "size": asset.size,
                "media_type": asset.media_type,
                "sha256": asset.sha256,
            },
        )
        enqueue_outbox(
            "attachment.bound",
            link.pk,
            {
                "project_id": str(task.project_id),
                "task_id": str(task.pk),
                "execution_id": None,
                "file_id": str(asset.pk),
                "purpose": AttachmentPurpose.TASK_REFERENCE.value,
            },
        )


def _required_project_id(payload: dict[str, Any]) -> uuid.UUID:
    value = payload.get("project_id")
    if value is None or value == "":
        raise InvalidArgumentError("缺少必填字段 project_id", details={"field": "project_id"})
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(
            "project_id 必须是合法 UUID", details={"field": "project_id", "value": value}
        ) from exc


def _next_task_number(project_id) -> int:
    """Per-project ``max(number)+1`` under the project lock.

    Archived tasks keep their number reserved, so a number is never reused.
    """
    current = Task.objects.filter(project_id=project_id).aggregate(maximum=Max("number"))["maximum"]
    return (current or 0) + 1


# --------------------------------------------------------------------------------------
# edit (PATCH)
# --------------------------------------------------------------------------------------


def edit_task(actor: ActorContext, task_id, payload: dict[str, Any], idem_key: str) -> dict:
    """Field-whitelisted task edit with ``expected_version``; never writes status or refs."""
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, TASK_EDITABLE_FIELDS | {"expected_version"}, surface="task.patch")
    expected_version = require_expected_version(payload)

    raw_task_id = _coerce_uuid(task_id, field="task_id")
    task = _load_task_or_404(raw_task_id)
    _authorize_task_action(actor, task)
    _require_project_authorable(task.project)

    changes = _edit_changes(task, payload)

    operation = operation_for("PATCH /api/{surface}/v1/tasks/{id}", actor)
    request_hash = canonical_request_hash(
        operation=operation, body=payload, resource_id=task.pk, project_id=task.project_id
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    plan = _lock_plan(task.project_id, task_id=task.pk, agent_ids=(actor.agent_id,) if actor.agent_id else ())
    locked = acquire_plan(plan)
    locked_task = _locked_task(plan, locked, task.pk)
    _require_project_authorable(locked.projects.get(task.project_id, task.project))
    _require_version(locked_task, expected_version)
    _edit_allowed_in_status(locked_task, payload)

    fields = dict(changes)
    if _touches_requirements(locked_task, payload):
        fields["requirements_version"] = locked_task.requirements_version + 1
    before = {"version": locked_task.version, "requirements_version": locked_task.requirements_version}
    updated = _cas_update(locked_task, expected_version, fields=fields)

    _activity(
        actor,
        "updated",
        updated,
        before=before,
        after={
            "changed_fields": sorted(fields.keys() - {"requirements_version"}),
            "version": updated.version,
            "requirements_version": updated.requirements_version,
        },
    )
    _outbox(
        "task.updated",
        updated,
        {
            "task_id": str(updated.pk),
            "project_id": str(updated.project_id),
            "changed_fields": sorted(fields.keys() - {"requirements_version"}),
            "version": updated.version,
        },
    )

    response_data = task_detail_payload(updated)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=updated.pk
    )
    return {**response_data, "idempotent_replay": False}


def _coerce_uuid(value, *, field: str):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgumentError(
            f"{field} 必须是合法 UUID", details={"field": field, "value": value}
        ) from exc


def _edit_changes(task: Task, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise the payload into column values (no version/status/ref fields)."""
    changes: dict[str, Any] = {}
    if "title" in payload:
        changes["title"] = _require_title(payload["title"])
    if "description" in payload:
        changes["description"] = _optional_text(payload, "description", max_length=TEXT_MAX_LENGTH) or ""
    if "acceptance_criteria" in payload:
        changes["acceptance_criteria"] = (
            _optional_text(payload, "acceptance_criteria", max_length=TEXT_MAX_LENGTH) or ""
        )
    if "difficulty" in payload:
        changes["difficulty"] = _require_difficulty(payload["difficulty"])
    if "due_at" in payload:
        changes["due_at"] = _optional_due_at(payload)
    if "labels" in payload:
        changes["labels"] = _optional_labels(payload) or []
    if not changes:
        raise InvalidArgumentError(
            "请求没有包含任何可更新字段", details={"allowed": sorted(TASK_EDITABLE_FIELDS)}
        )
    return changes


def _edit_allowed_in_status(task: Task, payload: dict[str, Any]) -> None:
    """A requirements change after publish needs a withdraw; after claim it is forbidden."""
    allowed = _EDITABLE_BY_STATUS.get(task.status, set())
    rejected = sorted(
        field
        for field in payload
        if field in TASK_EDITABLE_FIELDS and field not in allowed
    )
    if not rejected:
        return
    if task.status != TaskStatus.PREPARING.value and any(
        field in SUBSTANTIVE_FIELDS for field in rejected
    ):
        raise InvalidTransitionError(
            "任务已发布，实质要求只能先撤回到准备中再修改"
            if task.status == TaskStatus.TODO.value
            else "任务已领取或已完成，要求快照不可覆盖",
            details={"status": task.status, "fields": rejected},
        )
    raise InvalidTransitionError(
        "该状态下不允许修改这些字段", details={"status": task.status, "fields": rejected}
    )


def _touches_requirements(task: Task, payload: dict[str, Any]) -> bool:
    if task.status != TaskStatus.PREPARING.value:
        # TODO only allows non-substantive fields, and later states freeze requirements.
        return False
    return any(field in SUBSTANTIVE_FIELDS for field in payload)


# --------------------------------------------------------------------------------------
# publish / withdraw
# --------------------------------------------------------------------------------------


def publish_task(
    actor: ActorContext, task_id, expected_version: int, idem_key: str
) -> dict:
    """PREPARING -> TODO. Requires an ACTIVE project and complete requirements."""
    expected_version = validate_expected_version(expected_version)
    task = _load_task_or_404(_coerce_uuid(task_id, field="task_id"))
    _authorize_task_action(actor, task)

    operation = operation_for(OP_TASK_PUBLISH, actor)
    request_hash = canonical_request_hash(
        operation=operation,
        body={"expected_version": expected_version},
        resource_id=task.pk,
        project_id=task.project_id,
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    designated_id = task.designated_agent_id
    plan = _lock_plan(
        task.project_id,
        task_id=task.pk,
        agent_ids=tuple(
            agent_id for agent_id in (designated_id, actor.agent_id) if agent_id is not None
        ),
    )
    locked = acquire_plan(plan)
    locked_task = _locked_task(plan, locked, task.pk)
    locked_project = _locked_project(plan, locked, task.project_id)
    _require_version(locked_task, expected_version)
    if locked_task.status != TaskStatus.PREPARING.value:
        raise InvalidTransitionError(
            "只有准备中的任务可以发布", details={"status": locked_task.status}
        )
    require_project_active(locked_project)
    _require_publishable(locked_task, locked)

    before = {"status": locked_task.status, "version": locked_task.version}
    updated = _cas_update(
        locked_task,
        expected_version,
        fields={"status": TaskStatus.TODO.value, "published_at": db_now()},
    )

    _activity(
        actor,
        "published",
        updated,
        before=before,
        after={"status": updated.status, "version": updated.version, "published_at": iso(updated.published_at)},
    )
    _outbox(
        "task.published",
        updated,
        {
            "task_id": str(updated.pk),
            "project_id": str(updated.project_id),
            "number": updated.number,
            "status": updated.status,
            "claim_mode": updated.claim_mode,
            "designated_agent_id": uuid_str(updated.designated_agent_id),
        },
    )

    response_data = task_detail_payload(updated)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=updated.pk
    )
    return {**response_data, "idempotent_replay": False}


def _require_publishable(task: Task, locked) -> None:
    missing: list[str] = []
    if not (task.title or "").strip():
        missing.append("title")
    if not (task.description or "").strip():
        missing.append("description")
    if not (task.acceptance_criteria or "").strip():
        missing.append("acceptance_criteria")
    if len(task.title or "") > TITLE_MAX_LENGTH:
        missing.append("title_length")
    if task.difficulty not in (1, 2, 3, 4, 5):
        missing.append("difficulty")
    if missing:
        raise InvalidArgumentError(
            "任务要求不完整，无法发布", details={"fields": missing, "task_id": str(task.pk)}
        )
    if task.claim_mode == ClaimMode.ASSIGNED.value:
        agent = locked.agents.get(task.designated_agent_id)
        if agent is None:
            raise InvalidArgumentError(
                "指定领取的任务缺少有效指定人", details={"task_id": str(task.pk)}
            )
        _require_assignable_agent(agent, task.project_id)


def withdraw_task(actor: ActorContext, task_id, payload: dict[str, Any], idem_key: str) -> dict:
    """TODO -> PREPARING. Refused while a current execution exists; reason is mandatory."""
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, {"expected_version", "reason"}, surface="task.withdraw")
    expected_version = require_expected_version(payload)
    reason = require_reason(payload)

    task = _load_task_or_404(_coerce_uuid(task_id, field="task_id"))
    _authorize_task_action(actor, task)
    _require_project_authorable(task.project)

    operation = operation_for(OP_TASK_WITHDRAW, actor)
    request_hash = canonical_request_hash(
        operation=operation, body=payload, resource_id=task.pk, project_id=task.project_id
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    plan = _lock_plan(
        task.project_id, task_id=task.pk, agent_ids=(actor.agent_id,) if actor.agent_id else ()
    )
    locked = acquire_plan(plan)
    locked_task = _locked_task(plan, locked, task.pk)
    _require_version(locked_task, expected_version)
    if locked_task.status != TaskStatus.TODO.value:
        raise InvalidTransitionError(
            "只有待办任务可以撤回", details={"status": locked_task.status}
        )
    if locked_task.active_execution_id is not None:
        raise InvalidTransitionError(
            "任务存在当前执行，必须先回收后才能撤回",
            details={"execution_id": str(locked_task.active_execution_id)},
        )

    before = {"status": locked_task.status, "version": locked_task.version}
    updated = _cas_update(locked_task, expected_version, fields={"status": TaskStatus.PREPARING.value})

    _activity(
        actor,
        "withdrawn",
        updated,
        before=before,
        after={"status": updated.status, "version": updated.version},
        reason=reason,
    )
    _outbox(
        "task.withdrawn",
        updated,
        {
            "task_id": str(updated.pk),
            "project_id": str(updated.project_id),
            "status": updated.status,
            "reason": reason,
        },
    )

    response_data = task_detail_payload(updated)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=updated.pk
    )
    return {**response_data, "idempotent_replay": False}


# --------------------------------------------------------------------------------------
# assign
# --------------------------------------------------------------------------------------


def assign_task(actor: ActorContext, task_id, payload: dict[str, Any], idem_key: str) -> dict:
    """Set OPEN/ASSIGNED. Never creates an execution, reserves capacity or starts the timer."""
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    _reject_unknown_fields(
        payload, {"expected_version", "claim_mode", "designated_agent_id"}, surface="task.assign"
    )
    expected_version = require_expected_version(payload)
    if "claim_mode" not in payload:
        raise InvalidArgumentError("缺少必填字段 claim_mode", details={"field": "claim_mode"})

    task = _load_task_or_404(_coerce_uuid(task_id, field="task_id"))
    _authorize_task_action(actor, task)
    _require_project_authorable(task.project)
    designated = resolve_claim_assignment(
        claim_mode=payload.get("claim_mode"),
        designated_agent_id=payload.get("designated_agent_id"),
        project_id=task.project_id,
    )

    operation = operation_for(OP_TASK_ASSIGN, actor)
    request_hash = canonical_request_hash(
        operation=operation, body=payload, resource_id=task.pk, project_id=task.project_id
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    plan = _lock_plan(
        task.project_id,
        task_id=task.pk,
        agent_ids=tuple(
            agent_id
            for agent_id in (None if designated is None else designated.pk, actor.agent_id)
            if agent_id is not None
        ),
    )
    locked = acquire_plan(plan)
    locked_task = _locked_task(plan, locked, task.pk)
    _require_version(locked_task, expected_version)
    if locked_task.status not in (TaskStatus.PREPARING.value, TaskStatus.TODO.value):
        raise InvalidTransitionError(
            "只有准备中或待办任务可以改派；进行中任务必须先回收",
            details={"status": locked_task.status},
        )
    if designated is not None:
        locked_agent = locked.agents.get(designated.pk)
        require(locked_agent is not None, "指定 Agent 不在锁计划内或已被删除")
        _require_assignable_agent(locked_agent, locked_task.project_id)

    before = {
        "claim_mode": locked_task.claim_mode,
        "designated_agent_id": uuid_str(locked_task.designated_agent_id),
        "version": locked_task.version,
    }
    updated = _cas_update(
        locked_task,
        expected_version,
        fields={
            "claim_mode": payload["claim_mode"],
            "designated_agent_id": None if designated is None else designated.pk,
        },
    )

    _activity(
        actor,
        "assigned",
        updated,
        before=before,
        after={
            "claim_mode": updated.claim_mode,
            "designated_agent_id": uuid_str(updated.designated_agent_id),
            "version": updated.version,
        },
    )
    _outbox(
        "task.assigned",
        updated,
        {
            "task_id": str(updated.pk),
            "project_id": str(updated.project_id),
            "claim_mode": updated.claim_mode,
            "designated_agent_id": uuid_str(updated.designated_agent_id),
        },
    )

    response_data = task_detail_payload(updated)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=updated.pk
    )
    return {**response_data, "idempotent_replay": False}


# --------------------------------------------------------------------------------------
# archive / restore
# --------------------------------------------------------------------------------------


def archive_task(actor: ActorContext, task_id, payload: dict[str, Any], idem_key: str) -> dict:
    """Set the archive flag. Never changes the five-state status."""
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, {"expected_version", "reason"}, surface="task.archive")
    expected_version = require_expected_version(payload)
    reason = require_reason(payload)

    task = _load_task_or_404(_coerce_uuid(task_id, field="task_id"))
    _authorize_task_action(actor, task)
    _require_project_authorable(task.project)

    operation = operation_for(OP_TASK_ARCHIVE, actor)
    request_hash = canonical_request_hash(
        operation=operation, body=payload, resource_id=task.pk, project_id=task.project_id
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    plan = _lock_plan(
        task.project_id, task_id=task.pk, agent_ids=(actor.agent_id,) if actor.agent_id else ()
    )
    locked = acquire_plan(plan)
    locked_task = _locked_task(plan, locked, task.pk)
    _require_version(locked_task, expected_version)
    if locked_task.is_archived:
        raise InvalidTransitionError("任务已经归档", details={"task_id": str(locked_task.pk)})
    if locked_task.active_execution_id is not None:
        raise InvalidTransitionError(
            "任务存在有效执行，不能归档",
            details={"execution_id": str(locked_task.active_execution_id)},
        )
    if locked_task.status == TaskStatus.IN_REVIEW.value:
        raise InvalidTransitionError("待审核任务不能归档", details={"status": locked_task.status})

    before = {"is_archived": locked_task.is_archived, "version": locked_task.version}
    updated = _cas_update(locked_task, expected_version, fields={"is_archived": True})

    _activity(
        actor,
        "archived",
        updated,
        before=before,
        after={"is_archived": updated.is_archived, "version": updated.version},
        reason=reason,
    )
    _outbox(
        "task.archived",
        updated,
        {
            "task_id": str(updated.pk),
            "project_id": str(updated.project_id),
            "status": updated.status,
            "reason": reason,
        },
    )

    response_data = task_detail_payload(updated)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=updated.pk
    )
    return {**response_data, "idempotent_replay": False}


def restore_task(actor: ActorContext, task_id, payload: dict[str, Any], idem_key: str) -> dict:
    """Clear the archive flag; the project must accept writes again."""
    if not isinstance(payload, dict):
        raise InvalidArgumentError("请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, {"expected_version"}, surface="task.restore")
    expected_version = require_expected_version(payload)

    task = _load_task_or_404(_coerce_uuid(task_id, field="task_id"))
    _authorize_task_action(actor, task)
    _require_project_authorable(task.project)

    operation = operation_for(OP_TASK_RESTORE, actor)
    request_hash = canonical_request_hash(
        operation=operation, body=payload, resource_id=task.pk, project_id=task.project_id
    )
    receipt = lookup_or_reserve(
        actor_scope=actor.actor_scope(),
        operation=operation,
        key=idem_key,
        request_hash=request_hash,
        resource_type="task",
    )
    if receipt.replayed:
        return _replay_payload(receipt)

    plan = _lock_plan(
        task.project_id, task_id=task.pk, agent_ids=(actor.agent_id,) if actor.agent_id else ()
    )
    locked = acquire_plan(plan)
    locked_task = _locked_task(plan, locked, task.pk)
    _require_version(locked_task, expected_version)
    if not locked_task.is_archived:
        raise InvalidTransitionError("任务当前未归档", details={"task_id": str(locked_task.pk)})

    before = {"is_archived": locked_task.is_archived, "version": locked_task.version}
    updated = _cas_update(locked_task, expected_version, fields={"is_archived": False})

    _activity(
        actor,
        "restored",
        updated,
        before=before,
        after={"is_archived": updated.is_archived, "version": updated.version},
    )
    _outbox(
        "task.restored",
        updated,
        {
            "task_id": str(updated.pk),
            "project_id": str(updated.project_id),
            "status": updated.status,
        },
    )

    response_data = task_detail_payload(updated)
    complete_receipt(
        receipt, response_data=response_data, resource_type="task", resource_id=updated.pk
    )
    return {**response_data, "idempotent_replay": False}


# --------------------------------------------------------------------------------------
# 批量管理动作（WebAPI 管理面）
# --------------------------------------------------------------------------------------


def bulk_archive_tasks(
    actor: ActorContext, payload: dict[str, Any], idem_key: str | None
) -> dict[str, Any]:
    """批量归档（=管理面的批量删除）：逐项复用单条归档的全部校验，返回逐项结果。

    归档只改 ``is_archived`` 标记，五态状态与执行、提交、审核历史全部保留。
    """
    ids = parse_bulk_ids(payload, "task_ids")
    reason = require_reason(payload)

    def callback(task_id, _index, item_key):
        task = _load_task_or_404(task_id)
        return archive_task(
            actor, task_id, {"expected_version": task.version, "reason": reason}, item_key
        )

    outcome = run_bulk(ids, callback, key_prefix=idem_key)
    return bulk_result(actor, "task.bulk_archive", outcome)


def bulk_restore_tasks(
    actor: ActorContext, payload: dict[str, Any], idem_key: str | None
) -> dict[str, Any]:
    """批量恢复归档标记：逐项复用单条恢复的全部校验，返回逐项结果。"""
    ids = parse_bulk_ids(payload, "task_ids")

    def callback(task_id, _index, item_key):
        task = _load_task_or_404(task_id)
        return restore_task(actor, task_id, {"expected_version": task.version}, item_key)

    outcome = run_bulk(ids, callback, key_prefix=idem_key)
    return bulk_result(actor, "task.bulk_restore", outcome)


# --------------------------------------------------------------------------------------
# Namespace aliases frozen by CONTRACT.md §5
# --------------------------------------------------------------------------------------

author_task = create_task

__all__ = [
    "author_task",
    "archive_task",
    "bulk_archive_tasks",
    "bulk_restore_tasks",
    "assign_task",
    "create_task",
    "edit_task",
    "operation_for",
    "publish_task",
    "require_expected_version",
    "require_reason",
    "resolve_claim_assignment",
    "restore_task",
    "validate_expected_version",
    "withdraw_task",
]
