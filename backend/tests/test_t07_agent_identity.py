"""T07 acceptance: agent creation, key lifecycle, base attributes, capacity boundary and the
AgentAPI self/heartbeat surface.

Every assertion reads the database rows (``Agent.key_hash``/``key_version``,
``IdempotencyRecord``, ``ActivityEvent``, ``OutboxEvent``, ``TaskExecution``), not only the
HTTP status. Nothing is mocked: the WebAPI is driven through the Django test client and the
AgentAPI through a real ``TestClient`` against the FastAPI app.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

import pytest
from django.db import connections
from fastapi.testclient import TestClient

from agentapi.main import app as agent_app
from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import IdempotencyRecord, OutboxEvent
from apps.tasks.models import Task, TaskExecution
from domain.clock import db_now
from domain.contracts import AgentRole, TaskStatus
from domain.security import hash_agent_key
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.identity]

WEB = "/api/web/v1"
AGENT = "/api/agent/v1"
ALLOWED_HOST = "127.0.0.1:8000"


def _idem() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def web(client):
    client.defaults["HTTP_HOST"] = ALLOWED_HOST
    return client


def _agent_client() -> TestClient:
    return TestClient(agent_app)


def post_json(web, url: str, payload: dict | None = None, *, key: str | None = None, **extra):
    headers = dict(extra)
    if key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    return web.post(url, data=json.dumps(payload or {}), content_type="application/json", **headers)


def patch_json(web, url: str, payload: dict, *, key: str | None = None, **extra):
    headers = dict(extra)
    if key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    return web.patch(url, data=json.dumps(payload), content_type="application/json", **headers)


def create_agent_api(web, **overrides) -> dict:
    body = {"name": factories.unique_name("T07"), "role": "WORKER", "max_concurrency": 2}
    body.update(overrides)
    response = post_json(web, f"{WEB}/agents", body, key=_idem())
    assert response.status_code == 201, response.content
    return response.json()["data"]


def hold_execution(agent: Agent, project, *, difficulty: int = 2, timeout_seconds: int = 1800):
    """Give ``agent`` one IN_PROGRESS task with an open execution and one reserved slot."""
    agent.reserved_execution_count = agent.reserved_execution_count + 1
    if agent.max_concurrency < agent.reserved_execution_count:
        agent.max_concurrency = agent.reserved_execution_count
    agent.save(update_fields=["reserved_execution_count", "max_concurrency"])

    task = factories.create_task(project, title="执行中的任务", difficulty=difficulty)
    now = db_now()
    execution = factories.create_execution(
        task, agent, claimed_at=now, timeout_seconds=timeout_seconds
    )
    task.status = TaskStatus.IN_PROGRESS.value
    task.assignee_agent = agent
    task.active_execution = execution
    task.claimed_at = now
    task.version = task.version + 1
    task.save(
        update_fields=["status", "assignee_agent", "active_execution", "claimed_at", "version"]
    )
    return task, execution


def _all_json_text() -> str:
    """Every JSON payload stored outside the Agent row, as one searchable string."""
    parts: list[str] = []
    for record in IdempotencyRecord.objects.all():
        parts.append(json.dumps(record.response_data, default=str))
    for event in ActivityEvent.objects.all():
        parts.append(json.dumps({"before": event.before, "after": event.after}, default=str))
        parts.append(event.reason or "")
    for outbox in OutboxEvent.objects.all():
        parts.append(json.dumps(outbox.payload, default=str))
    for agent in Agent.objects.all():
        parts.append(json.dumps(agent.runtime_info, default=str))
        parts.append(json.dumps(agent.capabilities, default=str))
        parts.append(agent.description or "")
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# Create: at least 256-bit key, digest-only storage, sensitive receipt
# --------------------------------------------------------------------------------------


def test_create_agent_stores_only_the_digest_and_delivers_the_key_once(web, world):
    name = factories.unique_name("T07CREATE")
    response = post_json(
        web,
        f"{WEB}/agents",
        {
            "name": name,
            "role": "SUPERVISOR",
            "max_concurrency": 3,
            "description": "擅长数据处理",
            "capabilities": ["python", "vue", "python"],
        },
        key=_idem(),
    )

    assert response.status_code == 201
    payload = response.json()
    data = payload["data"]
    raw_key = data["agent_key"]
    digest = hash_agent_key(raw_key)

    # >= 256 bits of entropy from the secure random source, prefix ``agt_``.
    assert raw_key.startswith("agt_")
    assert len(raw_key) >= 44

    agent = Agent.objects.get(name=name)
    assert str(agent.pk) == data["id"]
    assert agent.key_hash == digest
    assert agent.key_hash != raw_key
    assert agent.key_version == 1
    assert agent.version == 1
    assert agent.enabled is True
    assert agent.role == AgentRole.SUPERVISOR.value
    assert agent.max_concurrency == 3
    assert agent.reserved_execution_count == 0
    assert agent.capabilities == ["python", "vue"]  # de-duplicated, order preserved
    assert data["key_version"] == 1
    assert data["capacity"] == {
        "max_concurrency": 3,
        "reserved_execution_count": 0,
        "available_capacity": 3,
    }

    # The plaintext (and the digest) never appear in any other read or stored payload.
    assert digest not in response.content.decode()
    detail = web.get(f"{WEB}/agents/{agent.pk}")
    assert detail.status_code == 200
    detail_text = detail.content.decode()
    assert raw_key not in detail_text
    assert digest not in detail_text
    assert "key_hash" not in detail_text
    assert detail.json()["data"]["project_ids"] == []

    stored = _all_json_text()
    assert raw_key not in stored
    assert digest not in stored

    record = IdempotencyRecord.objects.get()
    assert record.sensitive is True
    assert record.http_status == 201
    assert record.resource_type == "agent"
    assert record.resource_id == agent.pk
    assert "agent_key" not in record.response_data
    assert record.response_data["key_delivery_unavailable"] is True

    events = list(ActivityEvent.objects.all())
    assert len(events) == 1
    assert events[0].actor_type == "web_operator"
    assert events[0].action == "agent.create"
    outbox = list(OutboxEvent.objects.all())
    assert len(outbox) == 1
    assert outbox[0].entity_id == agent.pk
    assert outbox[0].event_type == "agent.created"


def test_create_accepts_both_roles_and_validates_the_request(web, world):
    supervisor = create_agent_api(web, role="SUPERVISOR", max_concurrency=1)
    worker = create_agent_api(web, role="WORKER", max_concurrency=5)
    assert Agent.objects.get(pk=supervisor["id"]).role == AgentRole.SUPERVISOR.value
    assert Agent.objects.get(pk=worker["id"]).role == AgentRole.WORKER.value

    invalid_cases = [
        {"name": factories.unique_name("BAD"), "role": "ADMIN"},
        {"role": "WORKER"},
        {"name": "x" * 101, "role": "WORKER"},
        {"name": factories.unique_name("BAD"), "role": "WORKER", "max_concurrency": 0},
        {"name": factories.unique_name("BAD"), "role": "WORKER", "max_concurrency": -1},
        {"name": factories.unique_name("BAD"), "role": "WORKER", "max_concurrency": "3"},
        {"name": factories.unique_name("BAD"), "role": "WORKER", "capabilities": "python"},
        {"name": factories.unique_name("BAD"), "role": "WORKER", "enabled": False},
    ]
    for body in invalid_cases:
        response = post_json(web, f"{WEB}/agents", body, key=_idem())
        assert response.status_code == 400, (body, response.content)
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT", body

    assert Agent.objects.filter(name__startswith="BAD-").count() == 0


@pytest.mark.fault
def test_key_material_never_reaches_a_log_line(web, world, caplog):
    """Create, rotation, detail and list: no plaintext key and no digest in any log line."""
    with caplog.at_level(logging.DEBUG):
        data = create_agent_api(web, role="WORKER")
        rotated = post_json(web, f"{WEB}/agents/{data['id']}/rotate-key", {}, key=_idem())
        assert rotated.status_code == 200
        web.get(f"{WEB}/agents/{data['id']}")
        web.get(f"{WEB}/agents?name={data['name']}")

    known_keys = (data["agent_key"], rotated.json()["data"]["agent_key"])
    log_text = "\n".join(f"{record.name} {record.getMessage()}" for record in caplog.records)
    for key in known_keys:
        assert key not in log_text
        assert key[:20] not in log_text
        assert hash_agent_key(key) not in log_text


def test_duplicate_name_is_a_conflict(web, world):
    name = factories.unique_name("T07DUP")
    create_agent_api(web, name=name)

    response = post_json(web, f"{WEB}/agents", {"name": name, "role": "WORKER"}, key=_idem())

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "AGENT_NAME_CONFLICT"
    assert payload["error"]["details"]["name"] == name
    assert Agent.objects.filter(name=name).count() == 1


@pytest.mark.fault
def test_create_replay_returns_metadata_without_a_second_key(web, world):
    name = factories.unique_name("T07REPLAY")
    body = {"name": name, "role": "WORKER", "max_concurrency": 2}
    idem_key = _idem()

    first = post_json(web, f"{WEB}/agents", body, key=idem_key)
    assert first.status_code == 201
    raw_key = first.json()["data"]["agent_key"]
    agent = Agent.objects.get(name=name)

    second = post_json(web, f"{WEB}/agents", body, key=idem_key)

    assert second.status_code == 200  # lost first response: metadata, never a second key
    data = second.json()["data"]
    assert data["key_delivery_unavailable"] is True
    assert "agent_key" not in data
    assert data["idempotent_replay"] is True
    assert data["id"] == str(agent.pk)

    agent.refresh_from_db()
    assert agent.key_hash == hash_agent_key(raw_key)
    assert agent.key_version == 1
    assert Agent.objects.filter(name=name).count() == 1
    assert IdempotencyRecord.objects.count() == 1

    # The same key with different parameters is a reuse, not a replay.
    changed = post_json(web, f"{WEB}/agents", {**body, "max_concurrency": 4}, key=idem_key)
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.django_db(transaction=True)
def test_created_agent_can_authenticate_and_unknown_tokens_cannot(web, world):
    data = create_agent_api(web, role="WORKER")
    client = _agent_client()

    ok = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": data["agent_key"]})
    assert ok.status_code == 200, ok.content
    assert ok.json()["data"]["id"] == data["id"]

    bogus = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": "agt_" + "x" * 43})
    assert bogus.status_code == 401
    assert bogus.json()["error"]["code"] == "AGENT_TOKEN_INVALID"


# --------------------------------------------------------------------------------------
# Base attributes (PATCH)
# --------------------------------------------------------------------------------------


def test_patch_updates_only_base_attributes(web, world):
    data = create_agent_api(web, role="SUPERVISOR", max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])
    assert agent.version == 1

    response = patch_json(
        web,
        f"{WEB}/agents/{agent.pk}",
        {
            "name": factories.unique_name("T07PATCH"),
            "description": "更新后的描述",
            "capabilities": ["go"],
            "max_concurrency": 4,
            "expected_version": 1,
        },
        key=_idem(),
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    agent.refresh_from_db()
    assert agent.name == payload["name"]
    assert agent.description == "更新后的描述"
    assert agent.capabilities == ["go"]
    assert agent.max_concurrency == 4
    assert agent.version == 2
    assert payload["version"] == 2
    assert agent.role == AgentRole.SUPERVISOR.value  # untouched
    assert agent.enabled is True  # untouched
    assert agent.key_version == 1  # untouched

    events = ActivityEvent.objects.filter(action="agent.update")
    assert events.count() == 1
    assert events.first().before["max_concurrency"] == 2
    assert events.first().after["max_concurrency"] == 4


@pytest.mark.claim
def test_patch_rejects_role_enabled_and_protected_fields(web, world):
    data = create_agent_api(web, role="WORKER", max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])

    for body in (
        {"role": "SUPERVISOR", "expected_version": 1},
        {"enabled": False, "expected_version": 1},
        {"key_hash": "0" * 64, "expected_version": 1},
        {"key_version": 9, "expected_version": 1},
        {"reserved_execution_count": 5, "expected_version": 1},
        {"version": 7, "expected_version": 1},
        {"capabilities": ["x"], "unexpected_field": 1, "expected_version": 1},
    ):
        response = patch_json(web, f"{WEB}/agents/{agent.pk}", body, key=_idem())
        assert response.status_code == 400, (body, response.content)
        payload = response.json()
        assert payload["error"]["code"] == "INVALID_ARGUMENT", body
        assert payload["error"]["details"]["fields"], body

    agent.refresh_from_db()
    assert agent.role == AgentRole.WORKER.value
    assert agent.enabled is True
    assert agent.key_version == 1
    assert agent.reserved_execution_count == 0
    assert agent.version == 1
    assert Agent.objects.filter(pk=agent.pk).count() == 1


def test_patch_requires_expected_version_and_detects_conflicts(web, world):
    data = create_agent_api(web, max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])

    missing = patch_json(web, f"{WEB}/agents/{agent.pk}", {"description": "x"}, key=_idem())
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "INVALID_ARGUMENT"

    stale = patch_json(
        web, f"{WEB}/agents/{agent.pk}", {"description": "x", "expected_version": 5}, key=_idem()
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == 1

    ok = patch_json(
        web, f"{WEB}/agents/{agent.pk}", {"description": "x", "expected_version": 1}, key=_idem()
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["version"] == 2


def test_patch_name_conflict_and_missing_idempotency_key(web, world):
    first = create_agent_api(web)
    second = create_agent_api(web)

    conflict = patch_json(
        web,
        f"{WEB}/agents/{second['id']}",
        {"name": first["name"], "expected_version": 1},
        key=_idem(),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "AGENT_NAME_CONFLICT"

    without_key = web.patch(
        f"{WEB}/agents/{second['id']}",
        data=json.dumps({"description": "x", "expected_version": 1}),
        content_type="application/json",
    )
    assert without_key.status_code == 400
    assert without_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.claim
def test_max_concurrency_cannot_drop_below_reserved_slots(web, world):
    data = create_agent_api(web, max_concurrency=3)
    agent = Agent.objects.get(pk=data["id"])
    hold_execution(agent, world.project_a)
    hold_execution(agent, world.project_b)
    agent.refresh_from_db()
    assert agent.reserved_execution_count == 2

    below = patch_json(
        web, f"{WEB}/agents/{agent.pk}", {"max_concurrency": 1, "expected_version": 1}, key=_idem()
    )
    assert below.status_code == 409
    assert below.json()["error"]["code"] == "CAPACITY_BELOW_RESERVED"
    assert below.json()["error"]["details"]["reserved_execution_count"] == 2

    agent.refresh_from_db()
    assert agent.max_concurrency == 3
    assert agent.version == 1

    equal = patch_json(
        web, f"{WEB}/agents/{agent.pk}", {"max_concurrency": 2, "expected_version": 1}, key=_idem()
    )
    assert equal.status_code == 200
    agent.refresh_from_db()
    assert agent.max_concurrency == 2
    assert agent.reserved_execution_count == 2


# --------------------------------------------------------------------------------------
# Key rotation
# --------------------------------------------------------------------------------------


@pytest.mark.claim
def test_rotate_key_keeps_the_agent_and_every_execution_deadline(web, world):
    data = create_agent_api(web, role="SUPERVISOR", max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    deadline_before = execution.execution_deadline_at
    claimed_before = execution.claimed_at

    response = post_json(web, f"{WEB}/agents/{agent.pk}/rotate-key", {}, key=_idem())

    assert response.status_code == 200
    payload = response.json()["data"]
    new_key = payload["agent_key"]
    agent.refresh_from_db()
    assert payload["id"] == str(agent.pk) == data["id"]
    assert payload["key_version"] == 2
    assert agent.key_version == 2
    assert agent.key_hash == hash_agent_key(new_key)
    assert agent.key_hash != hash_agent_key(data["agent_key"])
    assert agent.enabled is True
    assert agent.role == AgentRole.SUPERVISOR.value

    execution.refresh_from_db()
    task.refresh_from_db()
    assert execution.execution_deadline_at == deadline_before
    assert execution.claimed_at == claimed_before
    assert execution.ended_at is None
    assert execution.end_reason is None
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.version == 2
    assert agent.reserved_execution_count == 1

    detail = web.get(f"{WEB}/agents/{agent.pk}")
    assert new_key not in detail.content.decode()
    assert agent.key_hash not in detail.content.decode()


@pytest.mark.django_db(transaction=True)
def test_rotated_key_is_accepted_and_the_old_one_is_rejected(web, world):
    data = create_agent_api(web, role="WORKER")
    agent_id = data["id"]
    old_key = data["agent_key"]

    rotated = post_json(web, f"{WEB}/agents/{agent_id}/rotate-key", {}, key=_idem())
    assert rotated.status_code == 200
    new_key = rotated.json()["data"]["agent_key"]
    assert new_key != old_key

    client = _agent_client()
    assert client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": new_key}).status_code == 200
    stale = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": old_key})
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "AGENT_TOKEN_INVALID"


@pytest.mark.fault
def test_rotate_key_replay_does_not_rotate_twice(web, world):
    data = create_agent_api(web)
    agent = Agent.objects.get(pk=data["id"])
    idem_key = _idem()

    first = post_json(web, f"{WEB}/agents/{agent.pk}/rotate-key", {}, key=idem_key)
    assert first.status_code == 200
    first_key_version = first.json()["data"]["key_version"]
    assert first.json()["data"]["agent_key"]

    second = post_json(web, f"{WEB}/agents/{agent.pk}/rotate-key", {}, key=idem_key)

    assert second.status_code == 200
    assert second.json()["data"]["key_delivery_unavailable"] is True
    assert "agent_key" not in second.json()["data"]
    assert second.json()["data"]["idempotent_replay"] is True

    agent.refresh_from_db()
    assert agent.key_version == first_key_version == 2
    assert Agent.objects.filter(pk=agent.pk).count() == 1
    assert ActivityEvent.objects.filter(action="agent.key.rotate").count() == 1


# --------------------------------------------------------------------------------------
# List / detail reads
# --------------------------------------------------------------------------------------


def test_agent_list_filters_and_pagination(web, world):
    for agent in world.agents():
        assert agent.enabled is True

    all_agents = web.get(f"{WEB}/agents")
    assert all_agents.status_code == 200
    body = all_agents.json()["data"]
    assert body["total"] == len(world.agents())
    assert body["page"] == 1 and body["page_size"] == 20
    assert len(body["items"]) == len(world.agents())

    supervisors = web.get(f"{WEB}/agents?role=SUPERVISOR").json()["data"]
    assert supervisors["total"] == 2
    assert {item["name"] for item in supervisors["items"]} == {"S1", "S2"}

    by_name = web.get(f"{WEB}/agents?name=W1").json()["data"]
    assert by_name["total"] == 1
    assert by_name["items"][0]["id"] == str(world.w1.pk)

    disabled = web.get(f"{WEB}/agents?enabled=false").json()["data"]
    assert disabled["total"] == 0

    nobody_online = web.get(f"{WEB}/agents?online=true").json()["data"]
    assert nobody_online["total"] == 0

    Agent.objects.filter(pk=world.s1.pk).update(last_heartbeat_at=db_now())
    online = web.get(f"{WEB}/agents?online=true").json()["data"]
    assert online["total"] == 1
    assert online["items"][0]["id"] == str(world.s1.pk)
    assert online["items"][0]["online_state"] == "ONLINE_IDLE"
    offline = web.get(f"{WEB}/agents?online=false").json()["data"]
    assert offline["total"] == len(world.agents()) - 1

    page_two = web.get(f"{WEB}/agents?page=2&page_size=2").json()["data"]
    assert page_two["total"] == len(world.agents())
    assert page_two["page"] == 2
    assert page_two["page_size"] == 2
    assert len(page_two["items"]) == 2
    page_three = web.get(f"{WEB}/agents?page=3&page_size=2").json()["data"]
    assert len(page_three["items"]) == 1

    unsupported = web.get(f"{WEB}/agents?nonsense=1")
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"

    oversized = web.get(f"{WEB}/agents?page_size=101")
    assert oversized.status_code == 400
    assert oversized.json()["error"]["code"] == "INVALID_ARGUMENT"

    bad_role = web.get(f"{WEB}/agents?role=ADMIN")
    assert bad_role.status_code == 400

    bad_uuid = web.get(f"{WEB}/agents/not-a-uuid")
    assert bad_uuid.status_code == 400
    assert bad_uuid.json()["error"]["code"] == "INVALID_ARGUMENT"

    missing = web.get(f"{WEB}/agents/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_agent_detail_reports_authorisation_capacity_and_executions(web, world):
    data = create_agent_api(web, role="SUPERVISOR", max_concurrency=2)
    agent = Agent.objects.get(pk=data["id"])
    factories.add_member(world.project_a, agent)
    factories.add_member(world.project_b, agent)
    task, execution = hold_execution(agent, world.project_a)

    response = web.get(f"{WEB}/agents/{agent.pk}")

    assert response.status_code == 200
    body = response.json()["data"]
    assert set(body["project_ids"]) == {str(world.project_a.pk), str(world.project_b.pk)}
    assert {item["id"] for item in body["projects"]} == {
        str(world.project_a.pk),
        str(world.project_b.pk),
    }
    assert body["capacity"] == {
        "max_concurrency": 2,
        "reserved_execution_count": 1,
        "available_capacity": 1,
    }
    summary = body["execution_summary"]
    assert summary["open_count"] == 1
    assert summary["effective_count"] == 1
    assert summary["pending_reclaim_count"] == 0
    assert summary["completed_count"] == 0
    assert summary["timeout_count"] == 0
    assert summary["rework_count"] == 0
    current = body["current_executions"]
    assert [item["execution_id"] for item in current] == [str(execution.pk)]
    assert current[0]["task_id"] == str(task.pk)
    assert current[0]["expired"] is False
    assert body["online_state"] == "NEVER_CONNECTED"


# --------------------------------------------------------------------------------------
# AgentAPI /me and /heartbeat
# --------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_me_reports_role_capacity_authorisation_and_online_state(world):
    agent, key = factories.create_agent(factories.unique_name("T07ME"), "SUPERVISOR", max_concurrency=3)
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    client = _agent_client()

    response = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": key})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == str(agent.pk)
    assert data["role"] == "SUPERVISOR"
    assert data["enabled"] is True
    assert data["capacity"] == {
        "max_concurrency": 3,
        "reserved_execution_count": 1,
        "available_capacity": 2,
    }
    assert data["project_ids"] == [str(world.project_a.pk)]
    assert [item["execution_id"] for item in data["effective_executions"]] == [str(execution.pk)]
    assert data["pending_reclaim_executions"] == []
    assert data["execution_summary"]["effective_count"] == 1
    assert data["last_heartbeat_at"] is None
    assert data["online_state"] == "NEVER_CONNECTED"
    assert data["key_version"] == agent.key_version


@pytest.mark.django_db(transaction=True)
def test_heartbeat_records_server_time_and_never_extends_deadlines(world):
    agent, key = factories.create_agent(factories.unique_name("T07HB"), "WORKER", max_concurrency=2)
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a)
    deadline_before = execution.execution_deadline_at
    before = db_now()
    client = _agent_client()

    response = client.post(
        f"{AGENT}/heartbeat",
        headers={"AGENT_TOKEN": key},
        json={"runtime": {"version": "1.2.3", "host": "worker-1"}},
    )

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["agent_id"] == str(agent.pk)
    assert data["online_state"] == "ONLINE_WORKING"

    agent.refresh_from_db()
    assert agent.last_heartbeat_at is not None
    assert agent.last_heartbeat_at >= before
    assert agent.last_heartbeat_at <= db_now()
    assert agent.runtime_info == {"version": "1.2.3", "host": "worker-1"}
    assert agent.version == 1  # a heartbeat is not a business action
    assert agent.key_version == 1

    # No deadline extension, no task state change, no capacity change, no audit row.
    execution.refresh_from_db()
    task.refresh_from_db()
    assert execution.execution_deadline_at == deadline_before
    assert execution.ended_at is None
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.version == 2
    assert agent.reserved_execution_count == 1
    assert ActivityEvent.objects.filter(action__startswith="agent.").count() == 0
    assert OutboxEvent.objects.count() == 0

    me = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": key}).json()["data"]
    assert me["online_state"] == "ONLINE_WORKING"
    assert me["last_heartbeat_at"] == data["last_heartbeat_at"]

    # Body is optional.
    empty = client.post(f"{AGENT}/heartbeat", headers={"AGENT_TOKEN": key})
    assert empty.status_code == 200

    invalid = client.post(
        f"{AGENT}/heartbeat", headers={"AGENT_TOKEN": key}, json={"runtime": ["not", "an", "object"]}
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_ARGUMENT"
    agent.refresh_from_db()
    assert agent.last_heartbeat_at is not None


@pytest.mark.django_db(transaction=True)
def test_expired_execution_is_pending_reclaim_not_effective(world):
    agent, key = factories.create_agent(factories.unique_name("T07EXP"), "WORKER", max_concurrency=1)
    factories.add_member(world.project_a, agent)
    task, execution = hold_execution(agent, world.project_a, timeout_seconds=1)
    # Move the fixed window into the past: deadline passed, no reclamation ran yet.
    past = db_now() - timedelta(seconds=60)
    TaskExecution.objects.filter(pk=execution.pk).update(
        claimed_at=past, execution_deadline_at=past + timedelta(seconds=1)
    )
    execution.refresh_from_db()

    response = _agent_client().get(f"{AGENT}/me", headers={"AGENT_TOKEN": key})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["effective_executions"] == []
    assert [item["execution_id"] for item in data["pending_reclaim_executions"]] == [
        str(execution.pk)
    ]
    assert data["execution_summary"]["effective_count"] == 0
    assert data["execution_summary"]["pending_reclaim_count"] == 1
    assert data["online_state"] == "NEVER_CONNECTED"
    # Capacity stays occupied until the reclamation transaction runs.
    assert data["capacity"]["reserved_execution_count"] == 1
    assert data["capacity"]["available_capacity"] == 0


@pytest.mark.django_db(transaction=True)
def test_disabled_agent_key_is_rejected_with_401(web, world):
    agent, key = factories.create_agent(factories.unique_name("T07OFF"), "WORKER", enabled=False)

    response = _agent_client().get(f"{AGENT}/me", headers={"AGENT_TOKEN": key})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AGENT_DISABLED"
    assert agent.enabled is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.fault
def test_agentapi_heartbeat_restores_a_missing_sqlite_write_lock(world):
    """SQLite writes use the singleton as their write lock, so a wiped row is restored first.

    MySQL serialises ordinary writes with row locks and does not touch the singleton, so
    heartbeat leaves a missing claim-lock row alone. Claims still restore it.
    """
    from apps.tasks.models import TaskClaimLock

    TaskClaimLock.objects.filter(id=1).delete()
    agent, key = factories.create_agent(factories.unique_name("T07LOCK"), "WORKER")

    response = _agent_client().post(f"{AGENT}/heartbeat", headers={"AGENT_TOKEN": key})

    assert response.status_code == 200
    agent.refresh_from_db()
    assert agent.last_heartbeat_at is not None
    if connections["default"].vendor == "sqlite":
        assert TaskClaimLock.objects.get(id=1).revision >= 1
    else:
        assert TaskClaimLock.objects.filter(id=1).count() == 0

    factories.ensure_claim_lock()  # leave the shared fixture usable for the next test
    connections.close_all()
