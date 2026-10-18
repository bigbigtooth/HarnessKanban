"""WebAPI task routes: list/create, detail/edit and the authoring action endpoints (T13/T14).

The WebAPI has no login: every request is treated as the trusted local administrator, so
these routes never accept an actor identity in the body. Business rules live in
``domain.task_service`` / ``domain.read_task_query``; the views only translate HTTP into a
service call and the result back into the frozen envelope.

``difficulty`` here is the WebAPI integer filter (1..5). The AgentAPI ``l1``..``l5`` URL flags
are a different parameter with different semantics and are deliberately not accepted here.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from domain import read_task_query, task_service
from domain.errors import InvalidArgumentError
from webapi.params import (
    FORBIDDEN_IDENTITY_FIELDS,
    FORBIDDEN_TASK_PATCH_FIELDS,
    parse_json_body,
    parse_pagination,
    parse_query,
    parse_uuid,
    reject_fields,
    require_idempotency_key,
    web_actor,
)
from webapi.views_base import BaseAPIView

TASK_LIST_PARAMS = {
    "project_id",
    "status",
    "difficulty",
    "claim_mode",
    "designated_agent_id",
    "assignee_agent_id",
    "q",
    "include_archived",
    "page",
    "page_size",
}

# Fields that describe "who acts" (or the execution outcome). Neither surface may set them.
NEVER_SETTABLE_FIELDS = FORBIDDEN_IDENTITY_FIELDS | {"created_by_agent_id"}


class TaskView(BaseAPIView):
    """Shared plumbing: convert any failure into the frozen error envelope."""

    def guarded(self, request: HttpRequest, callback):
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001 - handled by the shared envelope mapper
            return self.handle_error(request, exc)

    def write(self, request: HttpRequest, callback):
        """Every write takes the outermost transaction and the retry policy."""
        return self.guarded(request, lambda: self.ok(request, self.run_write(callback)))

    def read(self, request: HttpRequest, callback):
        return self.guarded(request, lambda: self.ok(request, self.run_read(callback)))


def _bulk(request: HttpRequest, callback):
    actor = web_actor(request)
    idem_key = require_idempotency_key(request)
    payload = parse_json_body(request)
    reject_fields(payload, {"project_id", "status", "difficulty", "version"})
    return callback(actor, payload, idem_key)


class TaskCollectionView(TaskView):
    def get(self, request: HttpRequest):
        return self.read(request, lambda: _list_tasks(request))

    def post(self, request: HttpRequest):
        def run():
            idem_key = require_idempotency_key(request)
            payload = parse_json_body(request)
            reject_fields(payload, NEVER_SETTABLE_FIELDS)
            actor = web_actor(request)
            return task_service.create_task(actor, payload, idem_key)

        return self.write(request, run)


class TaskBulkArchiveView(TaskView):
    """``POST /tasks/bulk-archive``：批量归档（= 批量删除），逐项结果。"""

    def post(self, request: HttpRequest):
        return self.write(request, lambda: _bulk(request, task_service.bulk_archive_tasks))


class TaskBulkRestoreView(TaskView):
    """``POST /tasks/bulk-restore``：批量恢复归档标记，逐项结果。"""

    def post(self, request: HttpRequest):
        return self.write(request, lambda: _bulk(request, task_service.bulk_restore_tasks))


class TaskDetailView(TaskView):
    def get(self, request: HttpRequest, task_id: str):
        def run():
            actor = web_actor(request)
            return read_task_query.get_task_detail(actor, parse_uuid(task_id, field="task_id"))

        return self.read(request, run)

    def patch(self, request: HttpRequest, task_id: str):
        def run():
            idem_key = require_idempotency_key(request)
            payload = parse_json_body(request)
            reject_fields(payload, FORBIDDEN_TASK_PATCH_FIELDS | NEVER_SETTABLE_FIELDS)
            actor = web_actor(request)
            return task_service.edit_task(
                actor, parse_uuid(task_id, field="task_id"), payload, idem_key
            )

        return self.write(request, run)


class TaskPublishView(TaskView):
    def post(self, request: HttpRequest, task_id: str):
        def run():
            idem_key = require_idempotency_key(request)
            payload = parse_json_body(request)
            reject_fields(payload, NEVER_SETTABLE_FIELDS)
            actor = web_actor(request)
            return task_service.publish_task(
                actor,
                parse_uuid(task_id, field="task_id"),
                payload.get("expected_version"),
                idem_key,
            )

        return self.write(request, run)


class TaskWithdrawView(TaskView):
    def post(self, request: HttpRequest, task_id: str):
        return self.write(request, lambda: _action(request, task_id, task_service.withdraw_task))


class TaskAssignView(TaskView):
    def post(self, request: HttpRequest, task_id: str):
        return self.write(request, lambda: _action(request, task_id, task_service.assign_task))


class TaskArchiveView(TaskView):
    def post(self, request: HttpRequest, task_id: str):
        return self.write(request, lambda: _action(request, task_id, task_service.archive_task))


class TaskRestoreView(TaskView):
    def post(self, request: HttpRequest, task_id: str):
        return self.write(request, lambda: _action(request, task_id, task_service.restore_task))


class TaskActivityView(TaskView):
    def get(self, request: HttpRequest, task_id: str):
        def run():
            query = parse_query(request, allowed={"page", "page_size"})
            page_number, page_size = parse_pagination(query)
            actor = web_actor(request)
            return read_task_query.list_task_activity(
                actor,
                parse_uuid(task_id, field="task_id"),
                page_number=page_number,
                page_size=page_size,
            )

        return self.read(request, run)


def _action(request: HttpRequest, task_id: str, service):
    """Payload-driven task action shared by withdraw/assign/archive/restore."""
    idem_key = require_idempotency_key(request)
    payload = parse_json_body(request)
    reject_fields(payload, NEVER_SETTABLE_FIELDS)
    actor = web_actor(request)
    return service(actor, parse_uuid(task_id, field="task_id"), payload, idem_key)


def _list_tasks(request: HttpRequest) -> dict:
    query = parse_query(request, allowed=TASK_LIST_PARAMS)
    page_number, page_size = parse_pagination(query)
    q = (query.get("q") or "").strip() or None
    if q is None and "q" in query.values:
        raise InvalidArgumentError("q 不能为空", details={"parameter": "q"})
    filters = read_task_query.TaskListFilters(
        project_id=query.get_uuid("project_id"),
        status=query.get("status") or None,
        difficulty=query.get_int("difficulty"),
        claim_mode=query.get("claim_mode") or None,
        designated_agent_id=query.get_uuid("designated_agent_id"),
        assignee_agent_id=query.get_uuid("assignee_agent_id"),
        q=q,
        include_archived=bool(query.get_bool("include_archived", False)),
    )
    actor = web_actor(request)
    return read_task_query.list_tasks(actor, filters, page_number=page_number, page_size=page_size)


urlpatterns = [
    path("tasks", TaskCollectionView.as_view(), name="web-task-collection"),
    # 批量路由必须先于 ``tasks/<task_id>`` 注册，否则 "bulk-archive" 会被当成 ID。
    path(
        "tasks/bulk-archive",
        TaskBulkArchiveView.as_view(),
        name="web-task-bulk-archive",
    ),
    path(
        "tasks/bulk-restore",
        TaskBulkRestoreView.as_view(),
        name="web-task-bulk-restore",
    ),
    path("tasks/<str:task_id>", TaskDetailView.as_view(), name="web-task-detail"),
    path("tasks/<str:task_id>/publish", TaskPublishView.as_view(), name="web-task-publish"),
    path("tasks/<str:task_id>/withdraw", TaskWithdrawView.as_view(), name="web-task-withdraw"),
    path("tasks/<str:task_id>/assign", TaskAssignView.as_view(), name="web-task-assign"),
    path("tasks/<str:task_id>/archive", TaskArchiveView.as_view(), name="web-task-archive"),
    path("tasks/<str:task_id>/restore", TaskRestoreView.as_view(), name="web-task-restore"),
    path("tasks/<str:task_id>/activity", TaskActivityView.as_view(), name="web-task-activity"),
]
