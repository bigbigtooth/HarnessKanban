"""Django settings for the HarnessKanban WebAPI and shared domain services.

Shared write database, UTC sessions, no request-level implicit atomic blocks: domain
write services open their own outermost transaction (DEVELOP D05).
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config.env import BASE_DIR, ConfigError, build_config

try:
    ENV = build_config()
except ConfigError as exc:  # fail loudly before serving anything
    raise ImproperlyConfigured(str(exc)) from exc

# 生产部署必须通过环境变量注入随机密钥（admin 会话 Cookie 用它签名）。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "harvesstask-local-development-key")
DEBUG = ENV.env_name == "development"
ALLOWED_HOSTS = ENV.allowed_hosts
# WebUI / WebAPI 访问密钥：配置了就启用登录与 403 校验；留空则保持本机回环直连。
WEBUI_ACCESS_KEY = ENV.webui_access_key

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 本地管理台之外的生产管理入口（task.opcdesk.top/admin）：
    # 只读查看领域数据 + 管理登录用户；业务写操作仍必须走 WebAPI 的领域动作。
    "django.contrib.admin",
    "rest_framework",
    "apps.identity",
    "apps.projects",
    "apps.tasks",
    "apps.files",
    "apps.audit",
    "apps.jobs",
]

MIDDLEWARE = [
    "webapi.middleware.RequestContextMiddleware",
    # 访问密钥校验（仅作用于 /api/web/v1，登录/状态端点豁免）
    "webapi.middleware.WebUiAuthMiddleware",
    # RequestBoundaryMiddleware must run before CommonMiddleware: CommonMiddleware calls
    # request.get_host() itself, which raises DisallowedHost for a host outside
    # ALLOWED_HOSTS and would return a non-enveloped 400 before our 403 body is built.
    "webapi.middleware.RequestBoundaryMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # 注意：不启用 CsrfViewMiddleware —— WebAPI 的 JSON 写请求不带 CSRF token，
    # 启用会让所有业务写操作 403。admin 的登录表单本身仍使用 csrf_token 模板标签。
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES: list[dict] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

def _build_database_config(env) -> dict:
    if env.is_sqlite:
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env.sqlite_path,
            "OPTIONS": {"timeout": env.sqlite_busy_timeout_ms / 1000.0},
        }
    return {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env.mysql_name,
        "USER": env.mysql_user,
        "PASSWORD": env.mysql_password,
        "HOST": env.mysql_host,
        "PORT": str(env.mysql_port),
        "OPTIONS": {
            "charset": "utf8mb4",
            "isolation_level": "read committed",
            # Without this the server default (50s) would decide how long a claim waits for
            # the global lock, so CLAIM_LOCK_WAIT_SECONDS would be a configuration value that
            # silently does nothing.
            "init_command": (
                "SET SESSION innodb_lock_wait_timeout = "
                f"{max(int(env.claim_lock_wait_seconds), 1)}"
            ),
        },
        "TEST": {"NAME": env.mysql_test_name},
    }


DATABASES = {
    "default": _build_database_config(ENV),
}
DATABASES["default"]["CONN_MAX_AGE"] = 0
# Domain write services open the outermost transaction explicitly.
DATABASES["default"]["ATOMIC_REQUESTS"] = False

AUTH_PASSWORD_VALIDATORS: list[dict] = []

LANGUAGE_CODE = "zh-hans"
# admin 的中文界面需要 i18n；领域代码不使用 gettext，开启没有副作用。
USE_I18N = True
TIME_ZONE = "UTC"
USE_TZ = True

STATIC_URL = "static/"
# 生产环境由 nginx 直接提供 admin 的静态资源（collectstatic 的输出目录）。
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "EXCEPTION_HANDLER": "webapi.envelope.exception_handler",
    "UNAUTHENTICATED_USER_NONE": True,
}

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

FILE_STORAGE_ROOT = ENV.file_storage_root
REDIS_URL = ENV.redis_url

# 对象存储（阿里云 OSS）：私有 bucket + 签名 GET URL。disabled 时完全本地。
OSS = {
    "enabled": ENV.oss.enabled,
    "endpoint": ENV.oss.endpoint,
    "bucket": ENV.oss.bucket,
    "access_key_id": ENV.oss.access_key_id,
    "access_key_secret": ENV.oss.access_key_secret,
    "url_ttl_seconds": ENV.oss.url_ttl_seconds,
}

HARNESSTASK = {
    "env_name": ENV.env_name,
    "db_engine": ENV.db_engine,
    "difficulty_timeouts": ENV.difficulty_timeouts,
    "timeout_scan_interval_seconds": ENV.timeout_scan_interval_seconds,
    "outbox_scan_interval_seconds": ENV.outbox_scan_interval_seconds,
    "outbox_max_attempts": ENV.outbox_max_attempts,
    "claim_lock_wait_seconds": ENV.claim_lock_wait_seconds,
    "claim_compensation_limit": ENV.claim_compensation_limit,
    "claim_max_replans": ENV.claim_max_replans,
    "offline_threshold_seconds": ENV.offline_threshold_seconds,
    "heartbeat_interval_seconds": ENV.heartbeat_interval_seconds,
    "idempotency_retention_days": ENV.idempotency_retention_days,
    "max_upload_bytes": ENV.max_upload_bytes,
    "max_project_documents": ENV.max_project_documents,
    "max_task_reference_attachments": ENV.max_task_reference_attachments,
    "max_submission_artifacts": ENV.max_submission_artifacts,
    "temp_file_retention_hours": ENV.temp_file_retention_hours,
    "default_max_concurrency": ENV.default_max_concurrency,
    "database_retry_attempts": ENV.database_retry_attempts,
    "allowed_origins": ENV.allowed_origins,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": (
                "ts=%(asctime)s level=%(levelname)s logger=%(name)s "
                "request_id=%(request_id)s %(message)s"
            ),
        },
    },
    "filters": {
        "request_context": {"()": "webapi.logging.RequestContextFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
            "filters": ["request_context"],
        },
    },
    "root": {"handlers": ["console"], "level": ENV.log_level},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}

if ENV.is_sqlite:
    Path(ENV.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    # A file-backed test database keeps the configured single absolute path semantics and
    # lets threaded tests use separate connections instead of a private in-memory DB.
    _sqlite_file = Path(ENV.sqlite_path)
    DATABASES["default"]["TEST"] = {
        "NAME": str(_sqlite_file.with_name(f"test_{_sqlite_file.name}")),
    }
