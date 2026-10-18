"""WebAPI project routes: member authorisation (T08) and later lifecycle/document/order
endpoints owned by other tasks.

Membership only expresses authorisation; the supervisor/worker role stays global on the
Agent. A revocation may be asked to keep the open execution until its fixed deadline
(``WAIT``) or to reclaim it immediately (``NOW``); the immediate branch ends the executions
in the same transaction as the revocation.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.urls import path

from domain import membership_service, ordering_service, project_service
from domain.context_service import update_project_background
from webapi.params import (
    parse_json_body,
    parse_pagination,
    parse_query,
    parse_uuid,
    reject_fields,
    require_idempotency_key,
    web_actor,
)
from webapi.routes.agents import EnvelopedAPIView, reject_unknown_fields

PROJECT_LIST_PARAMS = {"status", "include_archived", "deleted", "page", "page_size"}
MEMBER_LIST_PARAMS = {"page", "page_size"}
MEMBER_REMOVE_FIELDS = {"disposition", "reason"}


class ProjectMemberCollectionView(EnvelopedAPIView):
    """``GET /projects/{id}/members``."""

    def get(self, request: HttpRequest, project_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(project_id, field="project_id")
        query = parse_query(request, allowed=MEMBER_LIST_PARAMS)
        page, page_size = parse_pagination(query)
        data = self.run_read(
            lambda: membership_service.list_members(actor, parsed_id, page, page_size)
        )
        return self.ok(request, data)


class ProjectMemberDetailView(EnvelopedAPIView):
    """``PUT /projects/{id}/members/{agent_id}`` add and ``DELETE`` remove."""

    def put(self, request: HttpRequest, project_id: str, agent_id: str):
        actor = web_actor(request)
        parsed_project = parse_uuid(project_id, field="project_id")
        parsed_agent = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_unknown_fields(payload, set())
        data = self.run_write(
            lambda: membership_service.update_membership(
                actor, parsed_project, parsed_agent, membership_service.ACTION_ADD, payload, idem_key
            )
        )
        http_status = 200 if data.get("idempotent_replay") else 201
        return self.ok(request, data, http_status=http_status)

    def delete(self, request: HttpRequest, project_id: str, agent_id: str):
        actor = web_actor(request)
        parsed_project = parse_uuid(project_id, field="project_id")
        parsed_agent = parse_uuid(agent_id, field="agent_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"agent_id", "project_id", "membership_id"})
        reject_unknown_fields(payload, MEMBER_REMOVE_FIELDS)
        data = self.run_write(
            lambda: membership_service.update_membership(
                actor,
                parsed_project,
                parsed_agent,
                membership_service.ACTION_REMOVE,
                payload,
                idem_key,
            )
        )
        return self.ok(request, data)


def _bool_param(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value in ("1", "true", "True")


class ProjectCollectionView(EnvelopedAPIView):
    """``GET /projects`` list and ``POST /projects`` create (always a DRAFT)."""

    def get(self, request: HttpRequest):
        actor = web_actor(request)
        query = parse_query(request, allowed=PROJECT_LIST_PARAMS)
        page, page_size = parse_pagination(query)
        data = self.run_read(
            lambda: project_service.list_projects(
                actor,
                status_filter=query.get("status"),
                include_archived=_bool_param(query.get("include_archived"), True),
                deleted=_bool_param(query.get("deleted"), False),
                page=page,
                page_size=page_size,
            )
        )
        return self.ok(request, data)

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"status", "version", "content_version", "archived_from_status", "id"})
        reject_unknown_fields(payload, {"title", "summary", "background"})
        data = self.run_write(
            lambda: project_service.create_project(actor, payload=payload, idem_key=idem_key)
        )
        http_status = 200 if data.get("idempotent_replay") else 201
        return self.ok(request, data, http_status=http_status)


class ProjectBulkDeleteView(EnvelopedAPIView):
    """``POST /projects/bulk-delete``：软删除（冻结为只读 + 删除标记），逐项结果。"""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_unknown_fields(payload, {"project_ids", "reason"})
        data = self.run_write(
            lambda: project_service.bulk_delete_projects(actor, payload=payload, idem_key=idem_key)
        )
        return self.ok(request, data)


class ProjectBulkRestoreView(EnvelopedAPIView):
    """``POST /projects/bulk-restore``：恢复到删除前的生命周期。"""

    def post(self, request: HttpRequest):
        actor = web_actor(request)
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_unknown_fields(payload, {"project_ids"})
        data = self.run_write(
            lambda: project_service.bulk_restore_projects(actor, payload=payload, idem_key=idem_key)
        )
        return self.ok(request, data)


class ProjectDetailView(EnvelopedAPIView):
    """``GET /projects/{id}`` detail and ``PATCH`` metadata/background edit.

    Background text is edited through the context-version service so ``content_version``
    advances and a new immutable snapshot is recorded; it may not be combined with other
    fields in one request, which keeps each edit to a single receipt.
    """

    def get(self, request: HttpRequest, project_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(project_id, field="project_id")
        data = self.run_read(lambda: project_service.get_project(actor, parsed_id))
        return self.ok(request, data)

    def patch(self, request: HttpRequest, project_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(project_id, field="project_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"status", "version", "content_version", "archived_from_status", "id"})

        if "background" in payload:
            extra = sorted(set(payload) - {"background", "expected_version"})
            if extra:
                from domain.errors import InvalidArgumentError

                raise InvalidArgumentError(
                    "背景信息必须单独修改，以便生成新的上下文版本",
                    details={"fields": extra},
                )
            expected_version = payload.get("expected_version")
            if expected_version is None:
                from domain.errors import InvalidArgumentError

                raise InvalidArgumentError("expected_version 必填")
            data = self.run_write(
                lambda: update_project_background(
                    actor,
                    parsed_id,
                    background=str(payload.get("background") or ""),
                    expected_version=expected_version,
                    idem_key=idem_key,
                )
            )
        else:
            reject_unknown_fields(payload, {"title", "summary", "expected_version"})
            data = self.run_write(
                lambda: project_service.update_project_metadata(
                    actor, parsed_id, payload=payload, idem_key=idem_key
                )
            )
        return self.ok(request, data)


class ProjectTaskOrderView(EnvelopedAPIView):
    """``POST /projects/{id}/task-order`` in-column ordering (T24).

    Carries the moved task, its real neighbours and ``expected_version``. Ordering never
    changes the task's column: a cross-column drag must use the business action endpoints.
    """

    def post(self, request: HttpRequest, project_id: str):
        actor = web_actor(request)
        parsed_id = parse_uuid(project_id, field="project_id")
        idem_key = require_idempotency_key(request)
        payload = parse_json_body(request)
        reject_fields(payload, {"status_target", "assignee_agent_id", "active_execution_id"})
        data = self.run_write(
            lambda: ordering_service.reorder_task(
                actor, parsed_id, payload=payload, idem_key=idem_key
            )
        )
        return self.ok(request, data)



urlpatterns: list = [
    path("projects", ProjectCollectionView.as_view(), name="web-projects"),
    # 批量路由必须先于 ``projects/<project_id>`` 注册，否则 "bulk-delete" 会被当成 ID。
    path(
        "projects/bulk-delete",
        ProjectBulkDeleteView.as_view(),
        name="web-project-bulk-delete",
    ),
    path(
        "projects/bulk-restore",
        ProjectBulkRestoreView.as_view(),
        name="web-project-bulk-restore",
    ),
    path("projects/<str:project_id>", ProjectDetailView.as_view(), name="web-project"),
    path(
        "projects/<str:project_id>/task-order",
        ProjectTaskOrderView.as_view(),
        name="web-project-task-order",
    ),
    path(
        "projects/<str:project_id>/members",
        ProjectMemberCollectionView.as_view(),
        name="web-project-members",
    ),
    path(
        "projects/<str:project_id>/members/<str:agent_id>",
        ProjectMemberDetailView.as_view(),
        name="web-project-member",
    ),
]
