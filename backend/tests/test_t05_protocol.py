"""T05 protocol acceptance: unified envelope, request-id propagation, Host/Origin/content-type
boundaries, header-alias parsing and the guarantee that framework validation errors never leak
stack traces, SQL or filesystem paths.

These tests exercise both adapters through their real HTTP entry points (Django test client
for the WebAPI, a Starlette/FastAPI ``TestClient`` for the AgentAPI). Nothing is mocked: the
services, the database and the shared domain layer are the real ones.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.test.utils import override_settings
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from agentapi.deps import read_agent_token
from agentapi.main import app as agent_app
from domain.errors import AgentTokenMissingError
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.unit]

WEB = "/api/web/v1"
AGENT = "/api/agent/v1"
ALLOWED_HOST = "127.0.0.1:8000"

# Anything that would indicate a leaked framework/ORM/internal detail in a response body.
LEAK_PROBES = ("Traceback", "SELECT ", "/Users/", "site-packages", ".venv")


def _idem() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def web(client):
    """Django test client with an allow-listed Host (the default 'testserver' is not)."""
    client.defaults["HTTP_HOST"] = ALLOWED_HOST
    return client


def _agent_client() -> TestClient:
    return TestClient(agent_app)


def _assert_envelope_request_id(response, payload: dict) -> None:
    assert response.headers["X-Request-Id"] == payload["request_id"]
    assert payload["request_id"]


# --------------------------------------------------------------------------------------
# 1. Envelope and request id
# --------------------------------------------------------------------------------------


def test_success_response_carries_data_and_request_id_header(web, world):
    response = web.get(f"{WEB}/agents")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"data", "request_id"}
    assert set(payload["data"].keys()) == {"items", "total", "page", "page_size"}
    _assert_envelope_request_id(response, payload)


def test_error_response_carries_error_and_request_id_header(web, world):
    response = web.get(f"{WEB}/agents/{uuid.uuid4()}")

    assert response.status_code == 404
    payload = response.json()
    assert set(payload.keys()) == {"error", "request_id"}
    assert set(payload["error"].keys()) == {"code", "message", "details"}
    assert payload["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]
    assert isinstance(payload["error"]["details"], dict)
    _assert_envelope_request_id(response, payload)


def test_inbound_request_id_is_used_for_tracing(web, world):
    response = web.get(f"{WEB}/agents", HTTP_X_REQUEST_ID="trace-me-123")

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "trace-me-123"
    assert response.json()["request_id"] == "trace-me-123"


@pytest.mark.django_db(transaction=True)
def test_agentapi_success_and_error_envelopes(world):
    # ``transaction=True``: the FastAPI sync route runs in a worker thread, so the agent row
    # must be committed (visible to that thread's own connection), not held in a test txn.
    agent, key = factories.create_agent(factories.unique_name("PROTO"), "WORKER")
    client = _agent_client()

    ok = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": key})
    assert ok.status_code == 200
    payload = ok.json()
    assert set(payload.keys()) == {"data", "request_id"}
    assert ok.headers["X-Request-Id"] == payload["request_id"]

    missing = client.get(f"{AGENT}/me")
    assert missing.status_code == 401
    error_payload = missing.json()
    assert set(error_payload.keys()) == {"error", "request_id"}
    assert error_payload["error"]["code"] == "AGENT_TOKEN_MISSING"
    assert missing.headers["X-Request-Id"] == error_payload["request_id"]


# --------------------------------------------------------------------------------------
# 2. Missing Idempotency-Key on a create
# --------------------------------------------------------------------------------------


def test_missing_idempotency_key_on_create_is_400(web, world):
    response = web.post(
        f"{WEB}/agents",
        data=json.dumps({"name": factories.unique_name("NOIDEM"), "role": "WORKER"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert response.headers["X-Request-Id"] == payload["request_id"]


def test_blank_idempotency_key_on_create_is_400(web, world):
    response = web.post(
        f"{WEB}/agents",
        data=json.dumps({"name": factories.unique_name("NOIDEM"), "role": "WORKER"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="   ",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# --------------------------------------------------------------------------------------
# 3. Host and Origin boundaries
# --------------------------------------------------------------------------------------


@pytest.mark.deployment
def test_foreign_origin_is_forbidden(web, world):
    response = web.get(f"{WEB}/agents", HTTP_ORIGIN="http://evil.example.com")

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"
    assert payload["error"]["details"]["origin"] == "http://evil.example.com"
    assert response.headers["X-Request-Id"] == payload["request_id"]


@pytest.mark.deployment
def test_allowed_origin_is_accepted(web, world):
    response = web.get(f"{WEB}/agents", HTTP_ORIGIN="http://127.0.0.1:5173")

    assert response.status_code == 200
    assert response.json()["data"]["items"]  # the local admin request is served


@pytest.mark.deployment
def test_bad_host_is_forbidden_with_the_documented_body(web, world):
    """A Host outside the local allow-list must never reach a view and must be enveloped.

    Every disallowed host answers the frozen 403 ``REQUEST_ORIGIN_FORBIDDEN`` body. The
    boundary middleware now runs before ``CommonMiddleware`` and validates the raw header
    itself, so Django's own ``DisallowedHost`` boundary (which would answer a non-enveloped
    400) is never reached first.
    """
    response = web.get(f"{WEB}/agents", HTTP_HOST="127.0.0.1.")

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"
    assert payload["error"]["details"]["host"] == "127.0.0.1."
    assert response.headers["X-Request-Id"] == payload["request_id"]
    assert "data" not in payload

    outside = web.get(f"{WEB}/agents", HTTP_HOST="evil.example.com")
    assert outside.status_code == 403
    outside_payload = outside.json()
    assert outside_payload["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"
    assert outside_payload["error"]["details"]["host"] == "evil.example.com"
    assert outside_payload["request_id"]
    assert b'"data"' not in outside.content
    assert b"DisallowedHost" not in outside.content


@pytest.mark.deployment
def test_host_boundary_also_applies_with_a_wildcard_allow_list(world):
    """Same rule with a wildcard entry: Django accepts the suffix, the boundary does not."""
    with override_settings(ALLOWED_HOSTS=["127.0.0.1", "localhost", ".not-allowed.test"]):
        client = _fresh_client()
        response = client.get(f"{WEB}/agents", HTTP_HOST="evil.not-allowed.test")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"


def _fresh_client():
    from django.test import Client

    return Client()


# --------------------------------------------------------------------------------------
# 4. Content type boundary
# --------------------------------------------------------------------------------------


@pytest.mark.deployment
def test_wrong_content_type_on_a_json_write_is_403(web, world):
    response = web.post(
        f"{WEB}/agents",
        data=json.dumps({"name": factories.unique_name("CT"), "role": "WORKER"}),
        content_type="text/plain",
        HTTP_IDEMPOTENCY_KEY=_idem(),
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"
    assert payload["error"]["details"]["content_type"] == "text/plain"
    assert payload["error"]["details"]["allowed"] == ["application/json"]
    assert response.headers["X-Request-Id"] == payload["request_id"]


def test_malformed_json_body_is_400_not_a_stack_trace(web, world):
    response = web.post(
        f"{WEB}/agents",
        data='{"name": "broken"',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=_idem(),
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    _assert_envelope_request_id(response, payload)


# --------------------------------------------------------------------------------------
# 5. GET on the AgentAPI claim path is never a claim
# --------------------------------------------------------------------------------------


def test_get_on_the_agentapi_claim_path_is_method_not_allowed(world):
    """``GET /api/agent/v1/get_task/`` must never claim a task.

    Recorded today: **405 METHOD_NOT_ALLOWED**. The path is registered for POST by the claim
    task, so Starlette answers 405 for GET; before that route existed it answered 404 (no
    route at all). The assertion accepts either outcome (the contract's own GET rule is 405)
    and only pins that the answer is never 200 and never carries a claimed task.
    """
    agent, key = factories.create_agent(factories.unique_name("CLAIMGET"), "WORKER")
    response = _agent_client().get(f"{AGENT}/get_task/", headers={"AGENT_TOKEN": key})

    assert response.status_code in (404, 405)
    payload = response.json()
    assert "data" not in payload
    if response.status_code == 405:
        assert payload["error"]["code"] == "METHOD_NOT_ALLOWED"
    else:
        assert payload["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert response.headers["X-Request-Id"] == payload["request_id"]


# --------------------------------------------------------------------------------------
# 6. AGENT_TOKEN header handling
# --------------------------------------------------------------------------------------


def _starlette_request(headers: list[tuple[bytes, bytes]]) -> StarletteRequest:
    """Build a request scope the way an ASGI server delivers it (header names lowercased)."""
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/agent/v1/me",
            "headers": headers,
            "query_string": b"",
        }
    )


@pytest.mark.identity
def test_read_agent_token_keeps_the_underscore_name():
    # HTTP header names are case-insensitive: the canonical upper-case lookup name is used
    # against the lower-case raw name a conforming ASGI server delivers.
    assert read_agent_token(_starlette_request([(b"agent_token", b"agt_abc")])) == "agt_abc"
    assert read_agent_token(_starlette_request([])) is None


@pytest.mark.identity
def test_read_agent_token_rejects_the_hyphen_variant():
    with pytest.raises(AgentTokenMissingError) as excinfo:
        read_agent_token(_starlette_request([(b"agent-token", b"agt_abc")]))

    assert excinfo.value.code == "AGENT_TOKEN_MISSING"
    assert excinfo.value.details["received_header"] == "AGENT-TOKEN"


@pytest.mark.identity
@pytest.mark.django_db(transaction=True)
def test_hyphen_header_does_not_authenticate_over_http(world):
    agent, key = factories.create_agent(factories.unique_name("HYPHEN"), "WORKER")
    client = _agent_client()

    accepted = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": key})
    assert accepted.status_code == 200
    assert accepted.json()["data"]["id"] == str(agent.pk)

    rejected = client.get(f"{AGENT}/me", headers={"AGENT-TOKEN": key})
    assert rejected.status_code == 401
    payload = rejected.json()
    assert payload["error"]["code"] == "AGENT_TOKEN_MISSING"
    assert payload["error"]["details"]["received_header"] == "AGENT-TOKEN"


@pytest.mark.identity
def test_invalid_and_missing_tokens_are_401(world):
    client = _agent_client()

    missing = client.get(f"{AGENT}/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AGENT_TOKEN_MISSING"

    invalid = client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": "agt_not-a-real-key"})
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "AGENT_TOKEN_INVALID"


# --------------------------------------------------------------------------------------
# 7. Framework validation errors never leak internals
# --------------------------------------------------------------------------------------


def _assert_clean_error_envelope(label: str, response, expected_status: int) -> dict:
    text = response.content.decode("utf-8", "replace")
    for probe in LEAK_PROBES:
        assert probe not in text, f"{label} leaked {probe!r}: {text[:400]}"
    payload = json.loads(text)
    assert set(payload.keys()) == {"error", "request_id"}, label
    assert payload["request_id"], label
    assert response.headers["X-Request-Id"] == payload["request_id"], label
    assert response.status_code == expected_status, (label, payload)
    return payload


def test_django_validation_errors_leak_no_internals(web, world):
    cases = {
        "malformed json": (
            web.post(
                f"{WEB}/agents",
                data="{not json",
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY=_idem(),
            ),
            400,
        ),
        "unsupported parameter": (web.get(f"{WEB}/agents?nonsense=1"), 400),
        "missing idempotency key": (
            web.post(
                f"{WEB}/agents",
                data=json.dumps({"name": "x", "role": "WORKER"}),
                content_type="application/json",
            ),
            400,
        ),
        "wrong content type": (
            web.post(
                f"{WEB}/agents",
                data="{}",
                content_type="application/xml",
                HTTP_IDEMPOTENCY_KEY=_idem(),
            ),
            403,
        ),
    }

    for label, (response, expected_status) in cases.items():
        _assert_clean_error_envelope(label, response, expected_status)


@pytest.mark.django_db(transaction=True)
def test_agentapi_validation_errors_leak_no_internals(world):
    agent, key = factories.create_agent(factories.unique_name("LEAK"), "WORKER")
    client = _agent_client()
    headers = {"AGENT_TOKEN": key}

    cases = {
        "body validation": (
            client.post(f"{AGENT}/heartbeat", headers=headers, json={"runtime": []}),
            400,
        ),
        "unmatched route": (client.get(f"{AGENT}/nope/", headers=headers), 404),
        "wrong method": (client.get(f"{AGENT}/get_task/", headers=headers), None),
        "invalid token": (client.get(f"{AGENT}/me", headers={"AGENT_TOKEN": "agt_garbage"}), 401),
    }

    for label, (response, expected_status) in cases.items():
        payload = _assert_clean_error_envelope(
            label,
            response,
            expected_status if expected_status is not None else response.status_code,
        )
        if label == "wrong method":
            assert response.status_code in (404, 405)
        assert "SELECT " not in json.dumps(payload)
        assert key not in json.dumps(payload)
