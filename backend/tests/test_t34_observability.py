"""T34 acceptance: queryable failures, manual recovery, and leak-free observability.

Covers the two selectors the task card names — ``fault and jobs`` (failures are visible and
recoverable, recovery does not repeat business, events are never silently dropped) and
``api and deployment`` (the public probe stays minimal and the management surface is not
reachable from the remote Agent entry point).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

import pytest
from django.db import OperationalError, connections

from apps.audit.models import ActivityEvent
from apps.jobs.models import OutboxEvent
from domain.clock import db_now
from domain.contracts import (
    ACTION_CAPACITY_REPAIRED,
    ACTION_LOCK_TIMEOUT,
    ACTION_OUTBOX_MANUAL_RETRY,
    ACTION_RETRY_EXHAUSTED,
    DeliveryState,
)
from domain.errors import DatabaseBusyError, InvalidTransitionError
from domain.ops_service import list_failures, redact, retry_outbox_event, run_capacity_audit
from domain.service_runner import run_request_service
from domain.transactions import run_write_service
from jobs import tasks as job_tasks
from tests import factories

pytestmark = [pytest.mark.fault, pytest.mark.jobs]

HOST = {"HTTP_HOST": "127.0.0.1:8000"}
OPS = "/api/web/v1/ops"


@pytest.fixture
def client():
    from django.test import Client

    return Client()


def web_actor():
    from domain.contracts import ActorContext, ActorType

    return ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))


def new_key() -> str:
    return str(uuid.uuid4())


def body(response):
    return json.loads(response.content)


def make_stuck_event(entity_id, *, attempts: int = 5, error: str = "broker 不可用") -> OutboxEvent:
    from domain.events import enqueue_outbox

    event = run_request_service(
        lambda: enqueue_outbox("task.claimed", entity_id, {"task_id": str(entity_id)}),
        require_write_lock=False,
    )
    OutboxEvent.objects.filter(pk=event.pk).update(
        delivery_state=DeliveryState.NEEDS_ATTENTION.value,
        attempts=attempts,
        last_error=error,
        next_retry_at=db_now() - timedelta(minutes=1),
    )
    event.refresh_from_db()
    return event


def test_stuck_event_is_queryable_with_its_reason(db, client):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    event = make_stuck_event(task.pk, attempts=5, error="Redis 连接被拒绝")

    response = client.get(f"{OPS}/failures", **HOST)

    assert response.status_code == 200, response.content
    data = body(response)["data"]
    assert data["total"] >= 1
    row = next(item for item in data["items"] if item["id"] == str(event.pk))
    assert row["record_type"] == "OUTBOX_EVENT"
    assert row["event_type"] == "task.claimed"
    assert row["delivery_state"] == DeliveryState.NEEDS_ATTENTION.value
    assert row["attempts"] == 5
    assert "Redis 连接被拒绝" in row["last_error"]
    assert row["recoverable"] is True


def test_manual_retry_makes_the_event_dispatchable_again_and_recovers(db, monkeypatch):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    event = make_stuck_event(task.pk, attempts=5)

    result = run_request_service(
        lambda: retry_outbox_event(web_actor(), event.pk, idem_key=new_key()),
        require_write_lock=False,
    )
    assert result["delivery_state"] == DeliveryState.PENDING.value
    assert result["attempts"] == 5  # attempt history is preserved, not reset

    event.refresh_from_db()
    assert event.delivery_state == DeliveryState.PENDING.value
    assert event.processed_at is None
    assert OutboxEvent.objects.filter(pk=event.pk).count() == 1  # same row, never recreated
    audit = ActivityEvent.objects.get(action=ACTION_OUTBOX_MANUAL_RETRY)
    assert audit.actor_type == "web_operator"
    assert audit.before["delivery_state"] == DeliveryState.NEEDS_ATTENTION.value

    # Recovery actually works: the dispatcher can now publish and the consumer completes once.
    monkeypatch.setattr(job_tasks, "_publish_to_broker", lambda payload: None)
    dispatch = job_tasks.outbox_dispatch(batch=10)
    assert dispatch["published"] >= 1
    consumed = job_tasks.consume_outbox_event(str(event.pk))
    assert consumed["status"] == "processed"
    again = job_tasks.consume_outbox_event(str(event.pk))
    assert again["status"] == "already_processed"


def test_retry_refuses_to_resurrect_a_consumed_event(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    event = make_stuck_event(task.pk)
    OutboxEvent.objects.filter(pk=event.pk).update(
        processed_at=db_now(), delivery_state=DeliveryState.PROCESSED.value
    )

    with pytest.raises(InvalidTransitionError):
        run_request_service(
            lambda: retry_outbox_event(web_actor(), event.pk, idem_key=new_key()),
            require_write_lock=False,
        )

    event.refresh_from_db()
    assert event.delivery_state == DeliveryState.PROCESSED.value
    assert event.processed_at is not None


def test_retry_replay_does_not_redo_the_recovery(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    event = make_stuck_event(task.pk)
    key = new_key()

    first = run_request_service(
        lambda: retry_outbox_event(web_actor(), event.pk, idem_key=key), require_write_lock=False
    )
    replay = run_request_service(
        lambda: retry_outbox_event(web_actor(), event.pk, idem_key=key), require_write_lock=False
    )

    assert replay["idempotent_replay"] is True
    assert replay["event_id"] == first["event_id"]
    assert ActivityEvent.objects.filter(action=ACTION_OUTBOX_MANUAL_RETRY).count() == 1


def test_retry_exhaustion_is_recorded_with_the_originating_request_id(db):
    """A bounded retry that runs out must leave a queryable record, not just a log line."""
    attempts = 0

    def always_busy():
        nonlocal attempts
        attempts += 1
        raise OperationalError("database is locked")

    with pytest.raises(DatabaseBusyError):
        run_write_service(always_busy, max_attempts=2, require_write_lock=False)

    assert attempts == 3  # 1 attempt + 2 retries
    record = ActivityEvent.objects.get(action=ACTION_RETRY_EXHAUSTED)
    assert record.actor_type == "system"
    assert record.request_id
    assert "database is locked" in record.reason
    assert record.after["stage"] == "retry_exhausted"
    assert record.after["claim_lock"] is False


def test_lock_wait_timeout_is_recorded_as_a_distinct_failure(db):
    """A lock-wait timeout is reported for the client to retry, and recorded for the operator."""
    def lock_wait():
        raise OperationalError(1205, "Lock wait timeout exceeded; try restarting transaction")

    with pytest.raises(Exception) as excinfo:
        run_write_service(lock_wait, require_write_lock=False)
    assert getattr(excinfo.value, "http_status", None) == 503

    record = ActivityEvent.objects.get(action=ACTION_LOCK_TIMEOUT)
    assert "Lock wait timeout exceeded" in record.reason
    assert record.after["driver_code"] == 1205
    assert record.after["stage"] == "lock_wait_timeout"


def test_capacity_audit_endpoint_repairs_drift_and_reports(db, client):
    world = factories.World()
    from apps.identity.models import Agent

    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)

    response = client.post(
        f"{OPS}/capacity-audit",
        data=json.dumps({}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )

    assert response.status_code == 200, response.content
    audit = body(response)["data"]["audit"]
    assert audit["repaired"] == 1
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    assert ActivityEvent.objects.filter(action=ACTION_CAPACITY_REPAIRED).count() == 1


def test_capacity_audit_requires_an_idempotency_key(db, client):
    response = client.post(f"{OPS}/capacity-audit", data=json.dumps({}), content_type="application/json", **HOST)

    assert response.status_code == 400
    assert body(response)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.api
@pytest.mark.deployment
def test_public_liveness_stays_minimal(db, client):
    response = client.get("/health/live", **HOST)

    assert response.status_code == 200
    payload = body(response)
    assert payload["data"] == {"status": "live"}
    text = response.content.decode("utf-8")
    for forbidden in ("mysql", "redis", "sqlite", "/Users/", "password", "celery"):
        assert forbidden.lower() not in text.lower(), forbidden


@pytest.mark.api
@pytest.mark.deployment
def test_management_surface_is_absent_from_the_remote_agent_api(db):
    """The Agent proxy forwards only /api/agent/v1, so ops paths must not exist there."""
    from agentapi.main import app

    paths = set(app.openapi()["paths"])
    assert not [path for path in paths if "ops" in path]
    assert not [path for path in paths if path.endswith("/health/ready")]


@pytest.mark.api
def test_ops_response_never_leaks_keys_or_private_paths(db, client):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    from django.conf import settings

    storage_root = str(settings.FILE_STORAGE_ROOT)
    secret = "agt_THIS_SHOULD_NEVER_APPEAR"
    make_stuck_event(
        task.pk,
        error=f"上传失败 key={secret} path={storage_root}/ab12.md",
    )

    # A secret stuffed into a payload must be dropped by the outbox sanitizer as well.
    event = make_stuck_event(task.pk)
    OutboxEvent.objects.filter(pk=event.pk).update(
        payload={"task_id": str(task.pk), "agent_key": secret}
    )

    response = client.get(f"{OPS}/failures", **HOST)
    text = response.content.decode("utf-8")

    assert secret not in text
    assert storage_root not in text
    assert "agt_<redacted>" in text or "ab12.md" not in text


def test_redact_helper_strips_keys_and_our_private_paths():
    from django.conf import settings

    root = str(settings.FILE_STORAGE_ROOT)
    cleaned = redact(f"error agt_ABC123 at {root}/deadbeef.md")

    assert "agt_ABC123" not in cleaned
    assert root not in cleaned
    assert "agt_<redacted>" in cleaned


@pytest.mark.api
def test_one_request_produces_one_structured_log_line_with_the_error_code(db, client, caplog):
    world = factories.World()
    caplog.set_level(logging.INFO, logger="harvesstask.access")

    ok = client.post(
        "/api/web/v1/projects",
        data=json.dumps({"title": "日志样本项目"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )
    assert ok.status_code == 201

    bad = client.post(
        "/api/web/v1/projects",
        data=json.dumps({"summary": "缺标题"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )
    assert bad.status_code == 400

    lines = [record.getMessage() for record in caplog.records if record.name == "harvesstask.access"]
    assert len(lines) == 2, lines

    success_line = next(line for line in lines if "status=201" in line)
    assert "method=POST" in success_line
    assert "route=api/web/v1/projects" in success_line
    assert "duration_ms=" in success_line
    assert "error_code=-" in success_line

    failure_line = next(line for line in lines if "status=400" in line)
    assert "error_code=INVALID_ARGUMENT" in failure_line

    # The chain is identifiable and leak-free.
    request_ids = {record.request_id for record in caplog.records if record.name == "harvesstask.access"}
    assert len(request_ids) == 2
    assert all(value and value != "-" for value in request_ids)
    for line in lines:
        assert "agt_" not in line
        assert "/Users/" not in line


@pytest.mark.api
def test_access_log_carries_the_task_id_for_a_task_route(db, client, caplog):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    caplog.set_level(logging.INFO, logger="harvesstask.access")

    response = client.get(f"/api/web/v1/tasks/{task.pk}", **HOST)
    assert response.status_code == 200

    line = next(
        record.getMessage() for record in caplog.records if record.name == "harvesstask.access"
    )
    assert f"task_id={task.pk}" in line
    assert "status=200" in line


def test_failure_list_paginates_and_reports_a_total(db, client):
    world = factories.World()
    for index in range(3):
        task = factories.create_task(world.project_a, difficulty=1, title=f"分页{index}")
        make_stuck_event(task.pk, error=f"错误 {index}")

    page_one = client.get(f"{OPS}/failures?page=1&page_size=2", **HOST)
    payload = body(page_one)["data"]
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert len(payload["items"]) == 2
    assert payload["total"] >= 3
