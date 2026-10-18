"""WebAPI submission views: proxy submit, supervisor review, reopen."""

from __future__ import annotations

from django.http import HttpRequest

from domain.errors import InvalidArgumentError
from domain.review_service import reopen_task, review_submission
from domain.submission_service import submit_result
from webapi.params import parse_json_body, require_idempotency_key, web_actor
from webapi.views_base import BaseAPIView


class WebTaskSubmitView(BaseAPIView):
    """Formal submission recorded with actor_type=web_operator (proxied local operation)."""

    def post(self, request: HttpRequest, task_id):
        payload = parse_json_body(request)
        idem_key = require_idempotency_key(request)
        actor = web_actor(request)
        try:
            result = self.run_write(
                lambda: submit_result(actor, task_id=task_id, payload=payload, idem_key=idem_key)
            )
        except BaseException as exc:  # noqa: BLE001 - mapped by the base handler
            return self.handle_error(request, exc)
        return self.ok(request, result["data"])


class WebTaskReviewView(BaseAPIView):
    def post(self, request: HttpRequest, task_id):
        payload = parse_json_body(request)
        idem_key = require_idempotency_key(request)
        if not payload.get("reviewer_agent_id"):
            raise InvalidArgumentError("Web 审核必须明确选择一名有效主管作为审核人")
        actor = web_actor(request)
        try:
            result = self.run_write(
                lambda: review_submission(actor, task_id=task_id, payload=payload, idem_key=idem_key)
            )
        except BaseException as exc:  # noqa: BLE001
            return self.handle_error(request, exc)
        return self.ok(request, result["data"])


class WebTaskReopenView(BaseAPIView):
    def post(self, request: HttpRequest, task_id):
        payload = parse_json_body(request)
        idem_key = require_idempotency_key(request)
        actor = web_actor(request)
        try:
            result = self.run_write(
                lambda: reopen_task(
                    actor,
                    task_id=task_id,
                    reason=str(payload.get("reason") or ""),
                    expected_version=payload.get("expected_version"),
                    idem_key=idem_key,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            return self.handle_error(request, exc)
        return self.ok(request, result["data"])
