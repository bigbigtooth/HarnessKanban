"""T43 acceptance: the WebUI settings dialog can read and write environment variables and
the LLM profile, secrets are never echoed back, and start-up merges the saved overrides
without displacing a deployment-pinned value.

The LLM probe is exercised against a local HTTP stub so the test owns the "provider" side:
one case answers ``/models``, one is unreachable — the button must report both without
raising a server error.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.test import Client

from config import env as env_module
from domain import settings_store

pytestmark = [pytest.mark.api]

BASE = "/api/web/v1"
IDEM = "Idempotency-Key"


@pytest.fixture
def web_client(db):
    return Client(SERVER_NAME="127.0.0.1")


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    """Point the settings file at a temp directory for the whole process."""
    path = tmp_path / "runtime_settings.json"
    monkeypatch.setenv(settings_store.PATH_ENV_VAR, str(path))
    return path


def put_settings(client: Client, payload: dict) -> dict:
    response = client.put(
        f"{BASE}/settings",
        data=json.dumps(payload),
        content_type="application/json",
        headers={IDEM: str(uuid.uuid4())},
    )
    return response


def probe_llm(client: Client) -> dict:
    """``POST /settings/llm/test``；写请求必须带 JSON 内容类型与幂等键。"""
    response = client.post(
        f"{BASE}/settings/llm/test",
        data=json.dumps({}),
        content_type="application/json",
        headers={IDEM: str(uuid.uuid4())},
    )
    return response.json()


def env_entry(view: dict, key: str) -> dict:
    for entry in view["env"]:
        if entry["key"] == key:
            return entry
    raise AssertionError(f"{key} 不在环境变量视图中")


class _ModelsHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible ``/models`` endpoint."""

    authorized: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        _ModelsHandler.authorized.append(self.headers.get("Authorization", ""))
        body = json.dumps({"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        return


@pytest.fixture
def llm_stub():
    server = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Read / write round trip
# ---------------------------------------------------------------------------


def test_settings_round_trip_masks_secrets(web_client, store_path):
    view = web_client.get(f"{BASE}/settings").json()["data"]
    assert view["llm"] == {
        "base_url": "",
        "model": "",
        "temperature": 0.7,
        "max_tokens": 2048,
        "system_prompt": "",
        "api_key_set": False,
    }
    # The catalog is grouped and each known variable carries a description.
    assert {"基础", "数据库", "存储", "OSS"} <= set(view["env_groups"])
    assert env_entry(view, "DB_ENGINE")["description"]
    assert view["settings_file"] == str(store_path)
    assert view["updated_at"] is None

    response = put_settings(
        web_client,
        {
            "llm": {"base_url": "https://api.example.com/v1/", "api_key": "sk-secret-123456", "model": "gpt-4o-mini"},
            "env": {"LOG_LEVEL": "DEBUG", "OSS_ENABLED": True},
        },
    )
    assert response.status_code == 200, response.content
    saved = response.json()["data"]
    # base_url is normalised (trailing slash removed) and the key is never returned.
    assert saved["llm"]["base_url"] == "https://api.example.com/v1"
    assert saved["llm"]["api_key_set"] is True
    assert "api_key" not in saved["llm"]
    assert "sk-secret-123456" not in response.content.decode()
    assert env_entry(saved, "LOG_LEVEL") == {
        "key": "LOG_LEVEL",
        "group": "基础",
        "description": "日志级别（DEBUG / INFO / WARNING / ERROR）",
        "secret": False,
        "source": "override",
        "value": "DEBUG",
        "has_value": True,
    }
    assert env_entry(saved, "OSS_ENABLED")["value"] == "true"
    assert saved["updated_at"]

    # Secret environment variables report only that a value is set.
    put_settings(web_client, {"env": {"MYSQL_PASSWORD": "p@ssw0rd"}})
    reread = web_client.get(f"{BASE}/settings").json()["data"]
    password = env_entry(reread, "MYSQL_PASSWORD")
    assert password["secret"] is True and password["has_value"] is True
    assert password["value"] is None

    # Persisted on disk with owner-only permissions.
    on_disk = json.loads(store_path.read_text())
    assert on_disk["llm"]["api_key"] == "sk-secret-123456"
    assert on_disk["env"]["MYSQL_PASSWORD"] == "p@ssw0rd"
    assert (store_path.stat().st_mode & 0o777) == 0o600


def test_saving_without_api_key_keeps_the_stored_key(web_client, store_path):
    put_settings(web_client, {"llm": {"api_key": "sk-keep-me"}})
    # The dialog cannot read the key back, so a save that omits it must not clear it.
    saved = put_settings(web_client, {"llm": {"model": "qwen-max"}}).json()["data"]
    assert saved["llm"]["api_key_set"] is True and saved["llm"]["model"] == "qwen-max"
    cleared = put_settings(web_client, {"llm": {"api_key": None}}).json()["data"]
    assert cleared["llm"]["api_key_set"] is False


def test_env_override_can_be_removed(web_client, store_path):
    put_settings(web_client, {"env": {"LOG_LEVEL": "DEBUG"}})
    view = put_settings(web_client, {"env": {"LOG_LEVEL": None}}).json()["data"]
    # Dropping the override exposes the .env / process value again (or nothing).
    assert env_entry(view, "LOG_LEVEL")["source"] in ("environment", "unset")


def test_custom_variables_are_accepted(web_client, store_path):
    view = put_settings(
        web_client, {"env": {"MY_TEAM_FLAG": "on", "LLM_PROXY_TOKEN": "tok-abcdef"}}
    ).json()["data"]
    custom = env_entry(view, "MY_TEAM_FLAG")
    assert custom["group"] == "自定义" and custom["value"] == "on"
    # A custom key that looks like a credential is masked by name as well.
    assert env_entry(view, "LLM_PROXY_TOKEN")["value"] is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "field"),
    (
        ({"env": {"lower_case": "x"}}, "env"),
        ({"env": {"1BAD": "x"}}, "env"),
        ({"env": {"WEB_DATABASE_URL": "sqlite://x"}}, "env"),
        ({"env": {"OK_KEY": "line\nbreak"}}, "env"),
        ({"llm": {"base_url": "ftp://example.com"}}, "llm"),
        ({"llm": {"temperature": 5}}, "llm"),
        ({"llm": {"max_tokens": 0}}, "llm"),
        ({"llm": {"unknown_field": "x"}}, "llm"),
        ({}, None),
        ({"unexpected": {}}, None),
    ),
)
def test_invalid_payloads_are_rejected(web_client, store_path, payload, field):
    response = put_settings(web_client, payload)
    assert response.status_code == 400, response.content
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    if field:
        # 具体字段路径（如 llm.base_url）也算命中该分组。
        assert error["details"]["field"].startswith(field)


def test_write_requires_an_idempotency_key(web_client, store_path):
    response = web_client.put(
        f"{BASE}/settings",
        data=json.dumps({"env": {"LOG_LEVEL": "INFO"}}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------------------
# Start-up merge
# ---------------------------------------------------------------------------


def test_saved_overrides_apply_at_startup_without_beating_process_env(store_path, monkeypatch):
    settings_store.write_store(
        settings_store.empty_store()
        | {"env": {"LOG_LEVEL": "WARNING", "MY_STARTUP_FLAG": "from-file"}}
    )
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("MY_STARTUP_FLAG", raising=False)
    monkeypatch.setenv("HARNESSTASK_ENV", "pinned-by-deployment")

    applied = env_module.apply_runtime_overrides(protected={"HARNESSTASK_ENV"})

    assert applied == {"LOG_LEVEL": "WARNING", "MY_STARTUP_FLAG": "from-file"}
    import os

    assert os.environ["LOG_LEVEL"] == "WARNING"
    assert os.environ["MY_STARTUP_FLAG"] == "from-file"
    assert "HARNESSTASK_ENV" not in applied


def test_corrupt_settings_file_degrades_to_empty(store_path):
    store_path.write_text("{not json")
    assert settings_store.read_store() == settings_store.empty_store()


# ---------------------------------------------------------------------------
# LLM connection probe
# ---------------------------------------------------------------------------


def test_llm_probe_without_base_url(web_client, store_path):
    data = probe_llm(web_client)["data"]
    assert data["ok"] is False and "base_url" in data["message"]


def test_llm_probe_reports_success_and_model_presence(web_client, store_path, llm_stub):
    put_settings(
        web_client,
        {"llm": {"base_url": llm_stub, "api_key": "sk-probe-token", "model": "gpt-4o-mini"}},
    )
    _ModelsHandler.authorized.clear()
    data = probe_llm(web_client)["data"]
    assert data["ok"] is True
    assert data["model_count"] == 2 and data["model_found"] is True
    assert data["models"] == ["gpt-4o-mini", "gpt-4o"]
    assert data["latency_ms"] is not None
    # The probe authenticates with the saved key and never echoes it.
    assert _ModelsHandler.authorized == ["Bearer sk-probe-token"]
    assert "sk-probe-token" not in json.dumps(data)


def test_llm_probe_reports_an_unreachable_endpoint(web_client, store_path):
    put_settings(web_client, {"llm": {"base_url": "http://127.0.0.1:9/v1"}})
    data = probe_llm(web_client)["data"]
    assert data["ok"] is False
    assert data["status_code"] is None and data["latency_ms"] is not None
    assert "无法连接" in data["message"]


def test_llm_probe_reports_an_http_error(web_client, store_path):
    # 占一个端口后立刻关闭：``serve_forever`` 从未启动，所以只能 close，不能 shutdown（会死等）。
    probe = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    port = probe.server_port
    probe.server_close()
    # Nothing listens on ``port`` any more; the probe must answer, not raise.
    put_settings(web_client, {"llm": {"base_url": f"http://127.0.0.1:{port}/v1"}})
    data = probe_llm(web_client)["data"]
    assert data["ok"] is False
