"""Runtime settings for the local admin console (T43): environment variables and the LLM
connection profile (base URL / API key / model) edited from the WebUI settings dialog.

Why this exists: the two APIs are configured entirely by environment variables, and there
was no way to inspect or change them without editing ``backend/.env`` by hand. This service
stores the WebUI-edited values in ``var/runtime_settings.json``; ``config.env`` merges them
in at start-up with the precedence *real process environment > saved overrides > .env*, so a
deployment can always pin a value and a saved override never silently wins over it.

Secret handling: values whose key looks like a credential are never returned — the view
reports only whether a value is set. Empty strings clear an environment override; the LLM
``api_key`` is only replaced when a new value is supplied, so saving the form does not wipe
a key the operator cannot read back.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from config.env import FORBIDDEN_SPLIT_DB_VARS
from domain.contracts import ActorContext, ActorType
from domain.errors import InvalidArgumentError, RoleForbiddenError
from domain.settings_store import empty_store, read_store, store_path, write_store

# ---------------------------------------------------------------------------
# Environment variable catalog: what the settings dialog shows, grouped and
# described, mirroring backend/.env.example.
# ---------------------------------------------------------------------------

ENV_CATALOG: tuple[dict[str, str], ...] = (
    # 基础
    {"group": "基础", "key": "HARNESSTASK_ENV", "description": "运行环境标识（local / staging / production）"},
    {"group": "基础", "key": "LOG_LEVEL", "description": "日志级别（DEBUG / INFO / WARNING / ERROR）"},
    {"group": "基础", "key": "ALLOWED_HOSTS", "description": "WebAPI 允许的 Host，逗号分隔"},
    {"group": "基础", "key": "ALLOWED_ORIGINS", "description": "WebAPI 允许的 Origin，逗号分隔"},
    # 数据库
    {"group": "数据库", "key": "DB_ENGINE", "description": "数据库引擎（sqlite / mysql）"},
    {"group": "数据库", "key": "SQLITE_PATH", "description": "SQLite 数据文件路径"},
    {"group": "数据库", "key": "MYSQL_HOST", "description": "MySQL 主机"},
    {"group": "数据库", "key": "MYSQL_PORT", "description": "MySQL 端口"},
    {"group": "数据库", "key": "MYSQL_DATABASE", "description": "MySQL 库名"},
    {"group": "数据库", "key": "MYSQL_USER", "description": "MySQL 用户"},
    {"group": "数据库", "key": "MYSQL_PASSWORD", "description": "MySQL 密码", "secret": "1"},
    {"group": "数据库", "key": "REDIS_URL", "description": "Redis / Celery broker 地址"},
    # 存储
    {"group": "存储", "key": "FILE_STORAGE_ROOT", "description": "本地上传文件根目录"},
    {"group": "存储", "key": "MAX_UPLOAD_BYTES", "description": "单个上传文件大小上限（字节）"},
    {"group": "存储", "key": "MAX_PROJECT_DOCUMENTS", "description": "单项目文档数量上限"},
    {"group": "存储", "key": "MAX_TASK_REFERENCE_ATTACHMENTS", "description": "任务参考附件数量上限"},
    {"group": "存储", "key": "MAX_SUBMISSION_ARTIFACTS", "description": "一次提交的产物数量上限"},
    {"group": "存储", "key": "TEMP_FILE_RETENTION_HOURS", "description": "临时文件保留小时数"},
    # OSS
    {"group": "OSS", "key": "OSS_ENABLED", "description": "是否启用 OSS（true / false）"},
    {"group": "OSS", "key": "OSS_ENDPOINT", "description": "OSS Endpoint"},
    {"group": "OSS", "key": "OSS_BUCKET", "description": "OSS Bucket"},
    {"group": "OSS", "key": "OSS_ACCESS_KEY_ID", "description": "OSS AccessKeyId"},
    {"group": "OSS", "key": "OSS_ACCESS_KEY_SECRET", "description": "OSS AccessKeySecret", "secret": "1"},
    {"group": "OSS", "key": "OSS_URL_TTL_SECONDS", "description": "OSS 签名链接有效期（秒）"},
    # 超时与扫描
    {"group": "超时与扫描", "key": "HARNESSTASK_TIMEOUT_L1", "description": "L1 执行超时（秒）"},
    {"group": "超时与扫描", "key": "HARNESSTASK_TIMEOUT_L2", "description": "L2 执行超时（秒）"},
    {"group": "超时与扫描", "key": "HARNESSTASK_TIMEOUT_L3", "description": "L3 执行超时（秒）"},
    {"group": "超时与扫描", "key": "HARNESSTASK_TIMEOUT_L4", "description": "L4 执行超时（秒）"},
    {"group": "超时与扫描", "key": "HARNESSTASK_TIMEOUT_L5", "description": "L5 执行超时（秒）"},
    {"group": "超时与扫描", "key": "TIMEOUT_SCAN_INTERVAL_SECONDS", "description": "超时扫描间隔（秒）"},
    {"group": "超时与扫描", "key": "OUTBOX_SCAN_INTERVAL_SECONDS", "description": "Outbox 扫描间隔（秒）"},
    {"group": "超时与扫描", "key": "OUTBOX_MAX_ATTEMPTS", "description": "Outbox 最大投递次数"},
    # 容量与并发
    {"group": "容量与并发", "key": "DEFAULT_MAX_CONCURRENCY", "description": "Agent 默认并发容量"},
    {"group": "容量与并发", "key": "HEARTBEAT_INTERVAL_SECONDS", "description": "Agent 心跳间隔（秒）"},
    {"group": "容量与并发", "key": "AGENT_OFFLINE_THRESHOLD_SECONDS", "description": "Agent 判定离线阈值（秒）"},
    {"group": "容量与并发", "key": "CLAIM_LOCK_WAIT_SECONDS", "description": "抢单锁等待上限（秒）"},
    {"group": "容量与并发", "key": "CLAIM_COMPENSATION_LIMIT", "description": "抢单补偿扫描上限"},
    {"group": "容量与并发", "key": "CLAIM_MAX_REPLANS", "description": "抢单最大重规划次数"},
    {"group": "容量与并发", "key": "DATABASE_RETRY_ATTEMPTS", "description": "数据库重试次数"},
    {"group": "容量与并发", "key": "SQLITE_BUSY_TIMEOUT_MS", "description": "SQLite busy 超时（毫秒）"},
    {"group": "容量与并发", "key": "IDEMPOTENCY_RETENTION_DAYS", "description": "幂等记录保留天数"},
)

ENV_GROUP_ORDER: tuple[str, ...] = ("基础", "数据库", "存储", "OSS", "超时与扫描", "容量与并发", "自定义")

ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_KEY_PATTERN = re.compile(r"(SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE)")

# Keys the console refuses to store: an override here would break the single-database rule.
PROTECTED_ENV_KEYS = frozenset(FORBIDDEN_SPLIT_DB_VARS) | {
    "HARNESSTASK_RUNTIME_SETTINGS_PATH",
    # 认证边界不能被“运行时设置”改写（否则拿到密钥的人可以换掉它，或误把自己锁在外面）。
    "WEBUI_ACCESS_KEY",
    "WEBUI_ACCESS_KEY_FILE",
}

ENV_VALUE_MAX_LENGTH = 2000

LLM_DEFAULTS: dict[str, Any] = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.7,
    "max_tokens": 2048,
    "system_prompt": "",
}

LLM_FIELDS = frozenset(LLM_DEFAULTS)
LLM_SECRET_FIELDS = frozenset({"api_key"})

LLM_CONNECT_TIMEOUT_SECONDS = 10.0
LLM_SAMPLE_MODELS = 20


def _require_web_operator(actor: ActorContext) -> None:
    if actor.actor_type is not ActorType.WEB_OPERATOR:
        raise RoleForbiddenError("只有本机管理员可以读取或修改系统设置")


def _is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_PATTERN.search(key.upper()))


def _effective_env_value(key: str, overrides: dict[str, str]) -> tuple[str | None, str]:
    """Return ``(value, source)`` for one environment key.

    ``source`` is ``override`` (saved from the WebUI), ``environment`` (present in the
    process environment / .env) or ``unset``.
    """
    if key in overrides:
        return overrides[key], "override"
    if key in os.environ:
        return os.environ[key], "environment"
    return None, "unset"


def _env_entry(spec: dict[str, str], overrides: dict[str, str]) -> dict[str, Any]:
    key = spec["key"]
    value, source = _effective_env_value(key, overrides)
    secret = bool(spec.get("secret")) or _is_secret_key(key)
    entry: dict[str, Any] = {
        "key": key,
        "group": spec.get("group", "自定义"),
        "description": spec.get("description", ""),
        "secret": secret,
        "source": source,
        "value": None if secret else (value or ""),
        "has_value": bool(value),
    }
    return entry


def _env_extras(overrides: dict[str, str], catalog_keys: set[str]) -> list[dict[str, Any]]:
    extras = []
    for key in sorted(overrides):
        if key in catalog_keys:
            continue
        secret = _is_secret_key(key)
        extras.append(
            {
                "key": key,
                "group": "自定义",
                "description": "自定义变量",
                "secret": secret,
                "source": "override",
                "value": None if secret else overrides[key],
                "has_value": bool(overrides[key]),
            }
        )
    return extras


def _llm_view(store: dict[str, Any]) -> dict[str, Any]:
    llm = {**LLM_DEFAULTS, **store.get("llm", {})}
    view: dict[str, Any] = {
        # ``temperature``/``max_tokens`` are numbers; everything else is text.
        "base_url": str(llm.get("base_url") or ""),
        "model": str(llm.get("model") or ""),
        "temperature": llm.get("temperature", LLM_DEFAULTS["temperature"]),
        "max_tokens": llm.get("max_tokens", LLM_DEFAULTS["max_tokens"]),
        "system_prompt": str(llm.get("system_prompt") or ""),
        "api_key_set": bool(str(llm.get("api_key") or "").strip()),
    }
    return view


def get_settings(actor: ActorContext) -> dict[str, Any]:
    """The masked settings view rendered by the WebUI settings dialog."""
    _require_web_operator(actor)
    store = read_store()
    overrides = dict(store["env"])
    catalog_keys = {spec["key"] for spec in ENV_CATALOG}
    entries = [_env_entry(spec, overrides) for spec in ENV_CATALOG]
    entries.extend(_env_extras(overrides, catalog_keys))
    return {
        "llm": _llm_view(store),
        "env": entries,
        "env_groups": list(ENV_GROUP_ORDER),
        "updated_at": store.get("updated_at"),
        "settings_file": str(store_path()),
        "notes": "环境变量改动在服务重启后生效；留空的密文字段表示保持原值。",
    }


def _validate_env_payload(payload: Any) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        raise InvalidArgumentError("env 必须是一个对象", details={"field": "env"})
    validated: dict[str, str | None] = {}
    for raw_key, raw_value in payload.items():
        key = str(raw_key).strip()
        if not ENV_KEY_PATTERN.match(key):
            raise InvalidArgumentError(
                "环境变量名必须以大写字母开头，只包含大写字母、数字与下划线",
                details={"field": "env", "key": key},
            )
        if key in PROTECTED_ENV_KEYS:
            raise InvalidArgumentError(
                "该系统变量不允许在控制台覆盖", details={"field": "env", "key": key}
            )
        if raw_value is None:
            validated[key] = None
            continue
        if isinstance(raw_value, bool):
            validated[key] = "true" if raw_value else "false"
            continue
        if not isinstance(raw_value, (str, int, float)):
            raise InvalidArgumentError(
                "环境变量值必须是字符串、数字或布尔值", details={"field": "env", "key": key}
            )
        value = str(raw_value).strip()
        if len(value) > ENV_VALUE_MAX_LENGTH:
            raise InvalidArgumentError(
                "环境变量值过长", details={"field": "env", "key": key, "max_length": ENV_VALUE_MAX_LENGTH}
            )
        if "\n" in value or "\r" in value or "\x00" in value:
            raise InvalidArgumentError(
                "环境变量值不能包含换行或空字符", details={"field": "env", "key": key}
            )
        validated[key] = value
    return validated


def _validate_llm_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidArgumentError("llm 必须是一个对象", details={"field": "llm"})
    unknown = sorted(set(payload) - LLM_FIELDS)
    if unknown:
        raise InvalidArgumentError(
            "LLM 设置包含不支持的字段", details={"field": "llm", "fields": unknown}
        )
    validated: dict[str, Any] = {}
    if "base_url" in payload:
        base_url = payload["base_url"]
        if base_url is None:
            base_url = ""
        if not isinstance(base_url, str):
            raise InvalidArgumentError("base_url 必须是字符串", details={"field": "llm.base_url"})
        base_url = base_url.strip().rstrip("/")
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            raise InvalidArgumentError(
                "base_url 必须以 http:// 或 https:// 开头", details={"field": "llm.base_url"}
            )
        if len(base_url) > 500:
            raise InvalidArgumentError("base_url 过长", details={"field": "llm.base_url"})
        validated["base_url"] = base_url
    if "api_key" in payload:
        api_key = payload["api_key"]
        if api_key is None:
            validated["api_key"] = ""  # explicit clear
        else:
            if not isinstance(api_key, str):
                raise InvalidArgumentError("api_key 必须是字符串", details={"field": "llm.api_key"})
            api_key = api_key.strip()
            if len(api_key) > 500:
                raise InvalidArgumentError("api_key 过长", details={"field": "llm.api_key"})
            if "\n" in api_key or "\r" in api_key:
                raise InvalidArgumentError("api_key 不能包含换行", details={"field": "llm.api_key"})
            if api_key:
                validated["api_key"] = api_key
    if "model" in payload:
        model = payload["model"] or ""
        if not isinstance(model, str):
            raise InvalidArgumentError("model 必须是字符串", details={"field": "llm.model"})
        model = model.strip()
        if len(model) > 200:
            raise InvalidArgumentError("model 过长", details={"field": "llm.model"})
        validated["model"] = model
    if "temperature" in payload:
        temperature = payload["temperature"]
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise InvalidArgumentError("temperature 必须是数字", details={"field": "llm.temperature"})
        if not 0 <= float(temperature) <= 2:
            raise InvalidArgumentError(
                "temperature 需要在 0 到 2 之间", details={"field": "llm.temperature"}
            )
        validated["temperature"] = round(float(temperature), 2)
    if "max_tokens" in payload:
        max_tokens = payload["max_tokens"]
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise InvalidArgumentError("max_tokens 必须是整数", details={"field": "llm.max_tokens"})
        if not 1 <= max_tokens <= 200000:
            raise InvalidArgumentError(
                "max_tokens 需要在 1 到 200000 之间", details={"field": "llm.max_tokens"}
            )
        validated["max_tokens"] = max_tokens
    if "system_prompt" in payload:
        system_prompt = payload["system_prompt"] or ""
        if not isinstance(system_prompt, str):
            raise InvalidArgumentError("system_prompt 必须是字符串", details={"field": "llm.system_prompt"})
        if len(system_prompt) > 8000:
            raise InvalidArgumentError("system_prompt 过长", details={"field": "llm.system_prompt"})
        validated["system_prompt"] = system_prompt
    return validated


def update_settings(actor: ActorContext, payload: dict[str, Any], *, idem_key: str) -> dict[str, Any]:
    """Merge ``payload`` into the saved settings and return the new masked view.

    The write is naturally idempotent (it replaces the whole file with the merged state),
    so a replayed request simply produces the same stored settings again.
    """
    _require_web_operator(actor)
    unknown = sorted(set(payload) - {"env", "llm"})
    if unknown:
        raise InvalidArgumentError("不支持的设置字段", details={"fields": unknown})
    if "env" not in payload and "llm" not in payload:
        raise InvalidArgumentError("至少需要提供 env 或 llm 中的一项")

    store = read_store()
    if "env" in payload:
        for key, value in _validate_env_payload(payload["env"]).items():
            if value is None:
                store["env"].pop(key, None)
            else:
                store["env"][key] = value
    if "llm" in payload:
        store["llm"].update(_validate_llm_payload(payload["llm"]))

    # ``updated_at`` is a wall-clock marker for the dialog, not a business timestamp.
    store["version"] = empty_store()["version"]
    store["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_store(store)
    return get_settings(actor)


def test_llm_connection(actor: ActorContext) -> dict[str, Any]:
    """Probe the configured LLM endpoint (``GET {base_url}/models``) and report the result.

    The probe never echoes the API key back and never raises for an unreachable endpoint:
    an unreachable provider is an expected answer for this button, not a server error.
    """
    _require_web_operator(actor)
    llm = _llm_view(read_store())
    base_url = str(llm["base_url"] or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "message": "",
        "url": "",
        "status_code": None,
        "latency_ms": None,
        "model_count": None,
        "model_found": None,
        "models": [],
    }
    if not base_url:
        result["message"] = "尚未配置 base_url，请先填写并保存"
        return result

    url = f"{base_url.rstrip('/')}/models"
    result["url"] = url
    headers = {"Accept": "application/json"}
    if llm["api_key_set"]:
        headers["Authorization"] = f"Bearer {read_store()['llm'].get('api_key', '')}"

    started = time.perf_counter()
    try:
        response = httpx.get(
            url, headers=headers, timeout=LLM_CONNECT_TIMEOUT_SECONDS, trust_env=False
        )
    except httpx.HTTPError as exc:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        result["message"] = f"无法连接 {url}（{type(exc).__name__}）"
        return result
    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    result["status_code"] = response.status_code
    if response.status_code >= 400:
        result["message"] = f"服务返回 HTTP {response.status_code}，请检查 base_url 与 api_key"
        return result

    models: list[str] = []
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        for item in payload["data"]:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
    result["model_count"] = len(models)
    result["models"] = models[:LLM_SAMPLE_MODELS]
    configured_model = str(llm["model"] or "").strip()
    if configured_model:
        result["model_found"] = configured_model in models
    result["ok"] = True
    result["message"] = f"连接成功（HTTP {response.status_code}，发现 {len(models)} 个模型）"
    if configured_model and result["model_found"] is False:
        result["message"] += f"，但未在列表中找到 {configured_model}"
    return result
