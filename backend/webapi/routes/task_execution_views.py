"""WebAPI execution views (claim / progress / release / reclaim)."""

from __future__ import annotations

from django.http import HttpRequest

from domain.claim_service import claim_task, web_claim_request
from domain.contracts import EndReason
from domain.errors import InvalidArgumentError, RoleForbiddenError
from domain.execution_service import end_execution, update_progress
from webapi.params import (
    parse_json_body,
    parse_uuid,
    require_field,
    require_idempotency_key,
    reject_fields,
    web_actor,
)
from webapi.views_base import BaseAPIView


class WebTaskClaimView(BaseAPIView):
    """Local administrator claims one explicit task on behalf of a chosen Agent.

    Uses the same singleton lock and the same domain service as the AgentAPI claim, and
    the audit trail records that this was a proxied web operation.
    """

    global_claim_lock = True

    def post(self, request: HttpRequest, task_id):
        payload = parse_json_body(request)
        idem_key = require_idempotency_key(request)
        agent_id = parse_uuid(require_field(payload, "agent_id"), field="agent_id")
        expected_version = payload.get("expected_version")
        if expected_version is None:
            raise InvalidArgumentError("Web 代领必须携带 expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise InvalidArgumentError("expected_version 必须是整数")
        reject_fields(payload, {"status", "priority", "sort_order"})

        actor = web_actor(request)
        claim_request = web_claim_request(
            agent_id=agent_id,
            task_id=task_id,
            expected_version=expected_version,
            fields=payload,
        )

        try:
            result = self.run_write(
                lambda: claim_task(actor, claim_request, idem_key), global_claim_lock=True
            )
        except BaseException as exc:  # noqa: BLE001 - mapped by the base handler
            return self.handle_error(request, exc)
        return self.ok(request, result["data"])


class WebTaskProgressView(BaseAPIView):
    def post(self, request: HttpRequest, task_id):
        payload = parse_json_body(request)
        idem_key = require_idempotency_key(request)
        execution_id = parse_uuid(require_field(payload, "execution_id"), field="execution_id")
        percent = payload.get("percent")
        if percent is not None and (isinstance(percent, bool) or not isinstance(percent, int)):
            raise InvalidArgumentError("percent 必须是整数")
        actor = web_actor(request)
        try:
            result = self.run_write(
                lambda: update_progress(
                    actor,
                    task_id=task_id,
                    execution_id=execution_id,
                    message=payload.get("message", ""),
                    percent=percent,
                    idem_key=idem_key,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            return self.handle_error(request, exc)
        return self.ok(request, result["data"])


class _WebEndExecutionView(BaseAPIView):
    reason: EndReason = EndReason.RECLAIM
    require_reason = True

    def post(self, request: HttpRequest, task_id):
        payload = parse_json_body(request)
        idem_key = require_idempotency_key(request)
        execution_id = parse_uuid(require_field(payload, "execution_id"), field="execution_id")
        expected_version = payload.get("expected_version")
        if expected_version is not None and (
            isinstance(expected_version, bool) or not isinstance(expected_version, int)
        ):
            raise InvalidArgumentError("expected_version 必须是整数")
        reason_text = str(payload.get("reason", ""))
        if self.require_reason and not reason_text.strip():
            raise InvalidArgumentError("必须填写原因")
        actor = web_actor(request)
        try:
            result = self.run_write(
                lambda: end_execution(
                    actor,
                    task_id=task_id,
                    execution_id=execution_id,
                    reason=self.reason,
                    expected_version=expected_version,
                    reason_text=reason_text,
                    idem_key=idem_key,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            return self.handle_error(request, exc)
        return self.ok(request, result["data"])


class WebTaskReclaimView(_WebEndExecutionView):
    reason = EndReason.RECLAIM


class WebTaskReleaseView(_WebEndExecutionView):
    """Release is a self-service action: a web operator cannot release someone's execution."""

    reason = EndReason.RELEASE

    def post(self, request: HttpRequest, task_id):
        return self.handle_error(
            request, RoleForbiddenError("主动释放必须由当前执行的 Agent 发起")
        )
