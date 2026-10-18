"""Environment loading and startup configuration validation (DEVELOP D02).

Configuration problems must fail loudly at start-up: illegal timeout mappings, zero
capacity, a non-writable file root or an attempt to give the two APIs independent
databases all raise ``ConfigError`` before any request is served.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Deployment must not be able to split the two APIs onto separate databases. A shared
# domain service over one write database is a contract requirement (DEVELOP D02/D19).
FORBIDDEN_SPLIT_DB_VARS = (
    "WEB_DATABASE_URL",
    "AGENT_DATABASE_URL",
    "WEB_DB_NAME",
    "AGENT_DB_NAME",
    "WEB_SQLITE_PATH",
    "AGENT_SQLITE_PATH",
    "DJANGO_DB_NAME",
    "FASTAPI_DB_NAME",
)


class ConfigError(RuntimeError):
    """Raised for invalid or incomplete startup configuration."""


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and (value is None or value.strip() == ""):
        raise ConfigError(f"缺少必需的环境变量 {name}")
    return "" if value is None else value.strip()


def _int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前为 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {name} 必须大于等于 {minimum}，当前为 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"环境变量 {name} 必须小于等于 {maximum}，当前为 {value}")
    return value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，当前为 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {name} 必须大于等于 {minimum}，当前为 {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"环境变量 {name} 必须是布尔值，当前为 {raw!r}")


def _list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class OssConfig:
    """Aliyun OSS access configuration (private bucket + presigned GET URLs).

    ``enabled=False`` keeps every upload purely local so tests and offline
    development never touch the network.
    """

    enabled: bool
    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    url_ttl_seconds: int

    @property
    def configured(self) -> bool:
        return bool(
            self.endpoint and self.bucket and self.access_key_id and self.access_key_secret
        )


@dataclass(frozen=True)
class EnvConfig:
    env_name: str
    db_engine: str
    sqlite_path: str
    mysql_host: str
    mysql_port: int
    mysql_name: str
    mysql_user: str
    mysql_password: str
    mysql_test_name: str
    redis_url: str
    file_storage_root: str
    allowed_hosts: list[str]
    allowed_origins: list[str]
    difficulty_timeouts: dict[int, int]
    timeout_scan_interval_seconds: float
    outbox_scan_interval_seconds: float
    outbox_max_attempts: int
    claim_lock_wait_seconds: float
    claim_compensation_limit: int
    claim_max_replans: int
    offline_threshold_seconds: int
    heartbeat_interval_seconds: int
    idempotency_retention_days: int
    max_upload_bytes: int
    max_project_documents: int
    max_task_reference_attachments: int
    max_submission_artifacts: int
    temp_file_retention_hours: int
    default_max_concurrency: int
    log_level: str
    database_retry_attempts: int
    sqlite_busy_timeout_ms: int
    # WebUI / WebAPI 共享访问密钥；为空表示不启用（本机回环开发默认如此）。
    webui_access_key: str = ""
    oss: OssConfig = field(
        default_factory=lambda: OssConfig(
            enabled=False, endpoint="", bucket="", access_key_id="",
            access_key_secret="", url_ttl_seconds=604800,
        )
    )
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def is_sqlite(self) -> bool:
        return self.db_engine == "sqlite"

    @property
    def is_mysql(self) -> bool:
        return self.db_engine == "mysql"


WEBUI_KEY_MIN_LENGTH = 16


def _webui_access_key() -> str:
    """解析 WebUI 访问密钥：优先环境变量，其次 secret 文件。

    部署时两种方式都可以：
    - ``.env`` 里写 ``WEBUI_ACCESS_KEY=...``；
    - 或把密钥写到 ``backend/secret/webui_access_key``（可用 ``WEBUI_ACCESS_KEY_FILE`` 改路径）。
    两者都没有时返回空串 —— 校验关闭，保持本机回环开发的原行为。
    """
    key = _env("WEBUI_ACCESS_KEY")
    if not key:
        raw_path = _env("WEBUI_ACCESS_KEY_FILE") or str(BASE_DIR / "secret" / "webui_access_key")
        path = Path(raw_path)
        if path.is_file():
            try:
                key = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"无法读取 WebUI 访问密钥文件 {path}：{exc}") from exc
    if key and len(key) < WEBUI_KEY_MIN_LENGTH:
        raise ConfigError(
            f"WEBUI_ACCESS_KEY 至少需要 {WEBUI_KEY_MIN_LENGTH} 个字符（当前 {len(key)}）"
        )
    return key


def load_env_file() -> None:
    """Load ``backend/.env`` when present; real environment variables always win."""
    external = set(os.environ)
    load_dotenv(BASE_DIR / ".env", override=False)
    apply_runtime_overrides(protected=external)


def apply_runtime_overrides(*, protected: set[str] | None = None) -> dict[str, str]:
    """Merge the environment overrides saved from the WebUI settings dialog (T43).

    Precedence, highest first: the real process environment, the saved overrides, then
    ``.env``. A value the deployment pinned explicitly is never displaced by the file, and
    a corrupt settings file simply contributes nothing.
    """
    from domain.settings_store import env_overrides

    skip = protected or set()
    applied: dict[str, str] = {}
    for key, value in env_overrides().items():
        if key in skip:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def build_config() -> EnvConfig:
    load_env_file()

    for var in FORBIDDEN_SPLIT_DB_VARS:
        if os.environ.get(var):
            raise ConfigError(
                f"检测到 {var}：两个 API 必须共用同一写数据库，不允许按 API 拆分数据库配置"
            )

    env_name = _env("HARNESSTASK_ENV", "development")

    db_engine = _env("DB_ENGINE", "sqlite").lower()
    if db_engine not in ("sqlite", "mysql"):
        raise ConfigError(f"DB_ENGINE 只支持 sqlite 或 mysql，当前为 {db_engine!r}")

    raw_sqlite = _env("SQLITE_PATH", str(BASE_DIR / "harvesstask.sqlite3"))
    sqlite_path = str(Path(raw_sqlite).expanduser())
    if db_engine == "sqlite":
        if not Path(sqlite_path).is_absolute():
            raise ConfigError(
                "SQLITE_PATH 必须是绝对路径，两个 API 进程必须指向同一数据库文件"
            )
        parent = Path(sqlite_path).parent
        if not parent.exists():
            raise ConfigError(f"SQLITE_PATH 的目录不存在：{parent}")
        if not os.access(parent, os.W_OK):
            raise ConfigError(f"SQLITE_PATH 的目录不可写：{parent}")

    file_root_raw = _env("FILE_STORAGE_ROOT", str(BASE_DIR / "var" / "files"))
    file_storage_root = str(Path(file_root_raw).expanduser())
    if not Path(file_storage_root).is_absolute():
        raise ConfigError("FILE_STORAGE_ROOT 必须是绝对路径")
    _ensure_writable_dir(file_storage_root)

    timeouts = {}
    for level in range(1, 6):
        timeouts[level] = _int(f"HARNESSTASK_TIMEOUT_L{level}", {1: 900, 2: 1800, 3: 7200, 4: 28800, 5: 86400}[level], minimum=1)
    ordered = [timeouts[level] for level in range(1, 6)]
    for index in range(1, 5):
        if ordered[index] <= ordered[index - 1]:
            raise ConfigError(
                "固定超时映射必须五级为正且严格递增，当前为 "
                + ", ".join(f"L{level}={timeouts[level]}" for level in range(1, 6))
            )

    allowed_hosts = _list("ALLOWED_HOSTS", "127.0.0.1,localhost")
    if not allowed_hosts:
        raise ConfigError("ALLOWED_HOSTS 不能为空")

    oss = _build_oss_config()

    config = EnvConfig(
        env_name=env_name,
        db_engine=db_engine,
        sqlite_path=sqlite_path,
        mysql_host=_env("MYSQL_HOST", "127.0.0.1"),
        mysql_port=_int("MYSQL_PORT", 3306, minimum=1, maximum=65535),
        mysql_name=_env("MYSQL_DATABASE", "harvesstask", required=db_engine == "mysql"),
        mysql_user=_env("MYSQL_USER", "root", required=db_engine == "mysql"),
        mysql_password=_env("MYSQL_PASSWORD", ""),
        mysql_test_name=_env("MYSQL_TEST_DATABASE", "harvesstask_test"),
        redis_url=_env("REDIS_URL", "redis://127.0.0.1:6379/0"),
        file_storage_root=file_storage_root,
        allowed_hosts=allowed_hosts,
        allowed_origins=_list(
            "ALLOWED_ORIGINS",
            "http://127.0.0.1:7100,http://localhost:7100,http://127.0.0.1:7101,http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000",
        ),
        difficulty_timeouts=timeouts,
        timeout_scan_interval_seconds=_float("TIMEOUT_SCAN_INTERVAL_SECONDS", 5.0, minimum=0.1),
        outbox_scan_interval_seconds=_float("OUTBOX_SCAN_INTERVAL_SECONDS", 5.0, minimum=0.1),
        outbox_max_attempts=_int("OUTBOX_MAX_ATTEMPTS", 5, minimum=1),
        claim_lock_wait_seconds=_float("CLAIM_LOCK_WAIT_SECONDS", 5.0, minimum=0.1),
        claim_compensation_limit=_int("CLAIM_COMPENSATION_LIMIT", 100, minimum=1, maximum=10000),
        claim_max_replans=_int("CLAIM_MAX_REPLANS", 3, minimum=0),
        offline_threshold_seconds=_int("AGENT_OFFLINE_THRESHOLD_SECONDS", 90, minimum=1),
        heartbeat_interval_seconds=_int("HEARTBEAT_INTERVAL_SECONDS", 30, minimum=1),
        idempotency_retention_days=_int("IDEMPOTENCY_RETENTION_DAYS", 7, minimum=1),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 20 * 1024 * 1024, minimum=1),
        max_project_documents=_int("MAX_PROJECT_DOCUMENTS", 100, minimum=1),
        max_task_reference_attachments=_int("MAX_TASK_REFERENCE_ATTACHMENTS", 20, minimum=1),
        max_submission_artifacts=_int("MAX_SUBMISSION_ARTIFACTS", 10, minimum=1),
        temp_file_retention_hours=_int("TEMP_FILE_RETENTION_HOURS", 24, minimum=1),
        default_max_concurrency=_int("DEFAULT_MAX_CONCURRENCY", 1, minimum=1),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        database_retry_attempts=_int("DATABASE_RETRY_ATTEMPTS", 3, minimum=0),
        sqlite_busy_timeout_ms=_int("SQLITE_BUSY_TIMEOUT_MS", 5000, minimum=0),
        webui_access_key=_webui_access_key(),
        oss=oss,
        extras={},
    )

    if config.redis_url in ("", "none"):
        raise ConfigError("REDIS_URL 不能为空")
    return config


def _build_oss_config() -> OssConfig:
    """Parse and validate the OSS block.

    阿里云对长期 AccessKey 签名 URL 的有效期上限是 7 天，TTL 配置越界直接拒绝
    启动，避免生成看似长期有效、实际早已失效的下载链接。
    """
    enabled = _bool("OSS_ENABLED", False)
    config = OssConfig(
        enabled=enabled,
        endpoint=_env("OSS_ENDPOINT", "").rstrip("/"),
        bucket=_env("OSS_BUCKET", ""),
        access_key_id=_env("OSS_ACCESS_KEY_ID", ""),
        access_key_secret=_env("OSS_ACCESS_KEY_SECRET", ""),
        url_ttl_seconds=_int("OSS_URL_TTL_SECONDS", 7 * 24 * 3600, minimum=60, maximum=604800),
    )
    if enabled and not config.configured:
        raise ConfigError(
            "OSS_ENABLED=true 时必须完整配置 OSS_ENDPOINT / OSS_BUCKET / "
            "OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET"
        )
    if config.endpoint and "://" in config.endpoint:
        raise ConfigError("OSS_ENDPOINT 不能包含协议前缀，应填写形如 oss-cn-shenzhen.aliyuncs.com")
    return config


def _ensure_writable_dir(path: str) -> None:
    directory = Path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"文件存储目录无法创建：{path}（{exc}）") from exc
    if not directory.is_dir():
        raise ConfigError(f"文件存储目录不是目录：{path}")
    probe = directory / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ConfigError(f"文件存储目录不可写：{path}（{exc}）") from exc
