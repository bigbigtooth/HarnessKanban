"""T44：WebUI 访问密钥。

覆盖三件事：
1. 未配置密钥时保持本机回环直连（不改变既有行为）；
2. 配置密钥后，WebAPI 无密钥/错密钥返回 403（错误信封），状态与登录端点豁免；
3. 登录下发 HttpOnly 签名 Cookie，之后同源请求（含文件下载链接）自动通过；
   轮换密钥会让旧 Cookie 立即失效。另外验证密钥可从 secret 文件读取、过短会拒绝启动。
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client, override_settings

from config.env import ConfigError, _webui_access_key
from webapi import auth

pytestmark = [pytest.mark.api]

BASE = "/api/web/v1"
KEY = "test-webui-key-0123456789abcdef"
IDEM = "Idempotency-Key"


@pytest.fixture
def web_client(db):
    return Client(SERVER_NAME="127.0.0.1")


def idem() -> dict[str, str]:
    return {IDEM: str(uuid.uuid4())}


def test_auth_disabled_keeps_local_behaviour(web_client):
    status = web_client.get(f"{BASE}/auth/status").json()["data"]
    assert status == {"required": False, "authenticated": True}
    assert web_client.get(f"{BASE}/projects?page_size=1").status_code == 200


@override_settings(WEBUI_ACCESS_KEY=KEY)
def test_webapi_requires_the_key(web_client):
    status = web_client.get(f"{BASE}/auth/status").json()["data"]
    assert status == {"required": True, "authenticated": False}

    missing = web_client.get(f"{BASE}/projects?page_size=1")
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "WEBUI_KEY_REQUIRED"

    wrong = web_client.get(f"{BASE}/projects?page_size=1", headers={"X-WebUI-Key": "nope"})
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "WEBUI_KEY_INVALID"

    ok = web_client.get(f"{BASE}/projects?page_size=1", headers={"X-WebUI-Key": KEY})
    assert ok.status_code == 200
    authenticated = web_client.get(
        f"{BASE}/auth/status", headers={"X-WebUI-Key": KEY}
    ).json()["data"]
    assert authenticated == {"required": True, "authenticated": True}


@override_settings(WEBUI_ACCESS_KEY=KEY)
def test_password_header_works_for_every_method(web_client):
    # 写操作同样要校验（403 发生在解析 body 之前）
    response = web_client.post(
        f"{BASE}/projects",
        data="{}",
        content_type="application/json",
        headers=idem(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "WEBUI_KEY_REQUIRED"

    login = web_client.post(
        f"{BASE}/auth/login",
        data='{"key": "%s"}' % KEY,
        content_type="application/json",
        headers=idem(),
    )
    assert login.status_code == 200
    cookie = login.cookies.get(auth.COOKIE_NAME)
    assert cookie is not None
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Lax"

    # 测试客户端会带上 Cookie：之后的读请求无需再带 header
    assert web_client.get(f"{BASE}/projects?page_size=1").status_code == 200

    logout = web_client.post(f"{BASE}/auth/logout", data="{}", content_type="application/json", headers=idem())
    assert logout.status_code == 200
    assert web_client.get(f"{BASE}/projects?page_size=1").status_code == 403


@override_settings(WEBUI_ACCESS_KEY=KEY)
def test_login_rejects_empty_or_wrong_key(web_client):
    empty = web_client.post(
        f"{BASE}/auth/login", data="{}", content_type="application/json", headers=idem()
    )
    assert empty.status_code == 403
    assert empty.json()["error"]["code"] == "WEBUI_KEY_REQUIRED"

    wrong = web_client.post(
        f"{BASE}/auth/login",
        data='{"key": "wrong-key-0000000000"}',
        content_type="application/json",
        headers=idem(),
    )
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "WEBUI_KEY_INVALID"
    assert auth.COOKIE_NAME not in wrong.cookies


@override_settings(WEBUI_ACCESS_KEY=KEY)
def test_rotating_the_key_invalidates_old_cookies(web_client):
    login = web_client.post(
        f"{BASE}/auth/login",
        data='{"key": "%s"}' % KEY,
        content_type="application/json",
        headers=idem(),
    )
    assert login.status_code == 200
    old_cookie = login.cookies[auth.COOKIE_NAME].value
    assert web_client.get(f"{BASE}/projects?page_size=1").status_code == 200

    with override_settings(WEBUI_ACCESS_KEY="rotated-key-0123456789abcdef"):
        # 旧 Cookie 里的指纹对不上新密钥 → 立即失效
        response = web_client.get(
            f"{BASE}/projects?page_size=1", headers={"Cookie": f"{auth.COOKIE_NAME}={old_cookie}"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "WEBUI_KEY_REQUIRED"


@override_settings(WEBUI_ACCESS_KEY=KEY)
def test_health_and_admin_are_not_gated(web_client):
    assert web_client.get("/health/live").status_code == 200
    # admin 有自己的登录会话，不属于 WebAPI 的密钥边界
    assert web_client.get("/admin/login/").status_code == 200


@override_settings(WEBUI_ACCESS_KEY=KEY)
def test_file_download_links_are_protected_too(web_client):
    response = web_client.get(f"{BASE}/files/not-a-file/download")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "WEBUI_KEY_REQUIRED"


# ---------------------------------------------------------------------------
# 密钥来源：.env / secret 文件
# ---------------------------------------------------------------------------


def test_key_can_come_from_a_secret_file(tmp_path, monkeypatch):
    secret = tmp_path / "webui_access_key"
    secret.write_text("file-based-key-0123456789\n", encoding="utf-8")
    monkeypatch.delenv("WEBUI_ACCESS_KEY", raising=False)
    monkeypatch.setenv("WEBUI_ACCESS_KEY_FILE", str(secret))
    assert _webui_access_key() == "file-based-key-0123456789"


def test_env_key_wins_over_file(tmp_path, monkeypatch):
    secret = tmp_path / "webui_access_key"
    secret.write_text("file-based-key-0123456789", encoding="utf-8")
    monkeypatch.setenv("WEBUI_ACCESS_KEY", "env-based-key-0123456789")
    monkeypatch.setenv("WEBUI_ACCESS_KEY_FILE", str(secret))
    assert _webui_access_key() == "env-based-key-0123456789"


def test_short_key_is_rejected(monkeypatch):
    monkeypatch.setenv("WEBUI_ACCESS_KEY", "short")
    with pytest.raises(ConfigError):
        _webui_access_key()


def test_missing_key_disables_auth(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBUI_ACCESS_KEY", raising=False)
    monkeypatch.setenv("WEBUI_ACCESS_KEY_FILE", str(tmp_path / "does-not-exist"))
    assert _webui_access_key() == ""
