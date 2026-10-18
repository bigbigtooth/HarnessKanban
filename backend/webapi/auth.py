"""WebUI 访问密钥（共享密钥）的签名 Cookie 与请求校验。

设计要点：
- 密钥来自部署配置（``WEBUI_ACCESS_KEY`` 或 ``backend/secret/webui_access_key``），
  配置为空时整体关闭校验，保持本机回环开发的原行为。
- 浏览器登录后拿到的是一枚**签名 Cookie**（HttpOnly + SameSite=Lax），不把明文密钥
  放进 localStorage；Cookie 里只放密钥的指纹，轮换密钥会立即让旧 Cookie 失效。
- 同时接受 ``X-WebUI-Key`` 请求头，方便 curl / 脚本直接调用（也用于契约测试）。
- 用 ``hmac.compare_digest`` 做常量时间比较。
"""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings
from django.core import signing

COOKIE_NAME = "harvesstask_webui"
COOKIE_SALT = "harvesstask.webui.auth"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def configured_key() -> str:
    """当前生效的访问密钥；空串表示未启用。"""
    return str(getattr(settings, "WEBUI_ACCESS_KEY", "") or "")


def is_enabled() -> bool:
    return bool(configured_key())


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def key_matches(provided: str) -> bool:
    expected = configured_key()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def make_session_cookie_value(key: str) -> str:
    return signing.dumps({"fp": key_fingerprint(key)}, salt=COOKIE_SALT, compress=True)


def session_cookie_is_valid(raw: str | None) -> bool:
    if not raw or not is_enabled():
        return False
    try:
        payload = signing.loads(raw, salt=COOKIE_SALT, max_age=COOKIE_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("fp") == key_fingerprint(configured_key())


def request_is_authenticated(request) -> bool:
    """密钥未启用时视为已认证；启用时接受签名 Cookie 或 X-WebUI-Key 头。"""
    if not is_enabled():
        return True
    if session_cookie_is_valid(request.COOKIES.get(COOKIE_NAME)):
        return True
    header = request.headers.get("X-WebUI-Key", "")
    return key_matches(header)
