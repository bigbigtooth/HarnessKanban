"""End-to-end acceptance over the real HTTP surfaces, without mocks.

Both APIs run in-process but through their real HTTP layers: the FastAPI AgentAPI through
a TestClient worker thread, the Django WebAPI through the Django test client. The flow
covers claim -> progress -> submit -> review and asserts database rows at each step.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client as DjangoClient
from fastapi.testclient import TestClient

from apps.identity.models import Agent
from apps.tasks.models import Task, TaskExecution, TaskReview, TaskSubmission
from domain.contracts import AgentRole, TaskStatus
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.workflow, pytest.mark.django_db(transaction=True)]

HOST = {"HTTP_HOST": "127.0.0.1:8000"}


@pytest.fixture
def agentapi_client():
    from agentapi.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def web_client():
    return DjangoClient()


@pytest.fixture
def committed_world(db):
    """The public fixture, committed so the AgentAPI worker thread can see it."""
    factories.ensure_claim_lock()
    return factories.World()


def _json(response):
    return json.loads(response.content)


def test_agent_api_rejects_missing_and_misspelled_token(agentapi_client):
    response = agentapi_client.post(
        "/api/agent/v1/get_task/",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    body = _json(response)
    assert response.status_code == 401
    assert body["error"]["code"] == "AGENT_TOKEN_MISSING"
    assert body["request_id"]

    misspelled = agentapi_client.post(
        "/api/agent/v1/get_task/",
        headers={"AGENT-TOKEN": "whatever", "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    assert misspelled.status_code == 401
    assert _json(misspelled)["error"]["code"] == "AGENT_TOKEN_MISSING"


def test_agent_api_rejects_unknown_token(agentapi_client, committed_world):
    response = agentapi_client.post(
        "/api/agent/v1/get_task/",
        headers={"AGENT_TOKEN": "agt_not-a-real-key", "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "AGENT_TOKEN_INVALID"


def test_get_task_requires_post(agentapi_client, committed_world):
    """A prefetch/cache GET must never claim a task."""
    response = agentapi_client.get(
        "/api/agent/v1/get_task/",
        headers={"AGENT_TOKEN": committed_world.w1_key},
    )
    assert response.status_code == 405


def test_missing_idempotency_key_is_rejected(agentapi_client, committed_world):
    response = agentapi_client.post(
        "/api/agent/v1/get_task/?l1=1",
        headers={"AGENT_TOKEN": committed_world.w1_key},
        json={},
    )
    assert response.status_code == 400
    assert _json(response)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_invalid_level_parameters_are_rejected(agentapi_client, committed_world):
    for query in ("?l0=1", "?l6=1", "?l1=2", "?l1=1&l1=0"):
        response = agentapi_client.post(
            f"/api/agent/v1/get_task/{query}",
            headers={
                "AGENT_TOKEN": committed_world.w1_key,
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={},
        )
        assert response.status_code == 400, (query, response.status_code, response.text)
        assert _json(response)["error"]["code"] == "INVALID_ARGUMENT"


def test_full_claim_to_approval_flow_over_http(agentapi_client, web_client, committed_world):
    world = committed_world
    task = factories.create_task(world.project_a, title="端到端任务", difficulty=2)

    # 1. Agent claims exactly one task.
    claim = agentapi_client.post(
        "/api/agent/v1/get_task/?l1=1&l2=1",
        headers={"AGENT_TOKEN": world.w1_key, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    assert claim.status_code == 200, claim.text
    claim_body = _json(claim)
    data = claim_body["data"]
    assert data["task_id"] == str(task.pk)
    assert data["status"] == TaskStatus.IN_PROGRESS.value
    assert data["execution_timeout_seconds"] == 1800
    execution_id = data["execution_id"]

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1

    # 2. Progress is recorded without extending the deadline.
    deadline_before = TaskExecution.objects.get(pk=execution_id).execution_deadline_at
    progress = agentapi_client.post(
        f"/api/agent/v1/tasks/{task.pk}/progress",
        headers={"AGENT_TOKEN": world.w1_key, "Idempotency-Key": str(uuid.uuid4())},
        json={"execution_id": execution_id, "message": "完成一半", "percent": 50},
    )
    assert progress.status_code == 200, progress.text
    assert TaskExecution.objects.get(pk=execution_id).execution_deadline_at == deadline_before

    # 3. The executing agent submits the formal result.
    task.refresh_from_db()
    submit = agentapi_client.post(
        f"/api/agent/v1/tasks/{task.pk}/submit",
        headers={"AGENT_TOKEN": world.w1_key, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "execution_id": execution_id,
            "expected_version": task.version,
            "summary": "已实现端到端流程",
            "content": "变更与验证说明",
            "self_check": [{"criterion": "五级筛选可用", "passed": True, "evidence": "手工验证"}],
            "artifacts": [],
            "links": [{"title": "交付", "url": "https://example.com/delivery/1"}],
        },
    )
    assert submit.status_code == 200, submit.text
    submission_id = _json(submit)["data"]["submission_id"]

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_REVIEW.value
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0

    # 4. A supervisor approves through the AgentAPI.
    review = agentapi_client.post(
        f"/api/agent/v1/tasks/{task.pk}/review",
        headers={"AGENT_TOKEN": world.s1_key, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "submission_id": submission_id,
            "expected_version": task.version,
            "decision": "APPROVE",
            "comment": "已核对产物与自检",
        },
    )
    assert review.status_code == 200, review.text

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE.value
    assert task.completed_at is not None
    assert TaskSubmission.objects.filter(task=task).count() == 1
    assert TaskReview.objects.filter(task=task, decision="APPROVE").count() == 1
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0


def test_web_api_lifecycle_actions_over_http(web_client, committed_world):
    world = committed_world
    task = factories.create_task(
        world.project_a, title="Web 生命周期任务", difficulty=1, status="TODO"
    )

    # Web proxy claim uses the same global lock and records the web source.
    claim = web_client.post(
        f"/api/web/v1/tasks/{task.pk}/claim",
        data=json.dumps({"agent_id": str(world.w2.pk), "expected_version": task.version}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **HOST,
    )
    assert claim.status_code == 200, claim.text
    claim_body = _json(claim)
    assert claim_body["data"]["status"] == TaskStatus.IN_PROGRESS.value
    execution_id = claim_body["data"]["execution_id"]

    task.refresh_from_db()
    assert task.assignee_agent_id == world.w2.pk

    # Wrong Idempotency-Key is required, not optional, on web writes.
    missing_key = web_client.post(
        f"/api/web/v1/tasks/{task.pk}/reclaim",
        data=json.dumps({"execution_id": execution_id, "reason": "x"}),
        content_type="application/json",
        **HOST,
    )
    assert missing_key.status_code == 400
    assert _json(missing_key)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    # A supervisor reclaim returns the task to TODO and releases the slot exactly once.
    reclaim = web_client.post(
        f"/api/web/v1/tasks/{task.pk}/reclaim",
        data=json.dumps({"execution_id": execution_id, "reason": "需要重新安排"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **HOST,
    )
    assert reclaim.status_code == 200, reclaim.text

    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.active_execution_id is None
    assert Agent.objects.get(pk=world.w2.pk).reserved_execution_count == 0
    assert TaskExecution.objects.filter(task=task).count() == 1


def test_web_api_rejects_foreign_origin_and_bad_content_type(web_client, committed_world):
    task = factories.create_task(committed_world.project_a, difficulty=1)

    foreign = web_client.post(
        f"/api/web/v1/tasks/{task.pk}/claim",
        data=json.dumps({"agent_id": str(committed_world.w2.pk), "expected_version": task.version}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_ORIGIN="http://evil.example.com",
        **HOST,
    )
    assert foreign.status_code == 403
    assert _json(foreign)["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"

    wrong_type = web_client.post(
        f"/api/web/v1/tasks/{task.pk}/claim",
        data="agent_id=x",
        content_type="application/x-www-form-urlencoded",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **HOST,
    )
    assert wrong_type.status_code == 403
    assert _json(wrong_type)["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"


def test_web_api_lists_and_errors_never_leak_internals(web_client, committed_world):
    response = web_client.post(
        "/api/web/v1/tasks/not-a-uuid/claim",
        data=json.dumps({}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **HOST,
    )
    text = response.content.decode("utf-8")
    for forbidden in ("Traceback", "SELECT ", "/Users/", "sqlite"):
        assert forbidden not in text, f"响应泄漏了内部细节：{forbidden}"
