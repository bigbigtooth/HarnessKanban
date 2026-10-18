"""T02 acceptance: start-up configuration validation (DEVELOP D02).

Configuration problems must fail loudly before anything is served. These cases exercise
``config.env.build_config`` directly, which is the same function Django settings call at
import time, so a failure here means the process would refuse to start.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from config.env import FORBIDDEN_SPLIT_DB_VARS, ConfigError, build_config

pytestmark = [pytest.mark.unit, pytest.mark.deployment]

BASE_CONFIGURED_VARS = tuple(FORBIDDEN_SPLIT_DB_VARS) + (
    "DB_ENGINE",
    "SQLITE_PATH",
    "FILE_STORAGE_ROOT",
    "ALLOWED_HOSTS",
    "REDIS_URL",
    "HARNESSTASK_TIMEOUT_L1",
    "HARNESSTASK_TIMEOUT_L2",
    "HARNESSTASK_TIMEOUT_L3",
    "HARNESSTASK_TIMEOUT_L4",
    "HARNESSTASK_TIMEOUT_L5",
    "CLAIM_LOCK_WAIT_SECONDS",
    "CLAIM_COMPENSATION_LIMIT",
    "MAX_UPLOAD_BYTES",
    "DEFAULT_MAX_CONCURRENCY",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every variable the loader reads so each case starts from documented defaults."""
    for name in BASE_CONFIGURED_VARS:
        monkeypatch.delenv(name, raising=False)
    # The suite itself runs under DB_ENGINE/FILE_STORAGE_ROOT from the shell; the loader
    # also honours a dotenv file, so isolate the process from one if it exists.
    monkeypatch.setattr("config.env.load_dotenv", lambda *args, **kwargs: None)
    return monkeypatch


def test_default_configuration_is_valid(clean_env, tmp_path):
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))

    config = build_config()

    assert config.db_engine == "sqlite"
    assert config.difficulty_timeouts == {1: 900, 2: 1800, 3: 7200, 4: 28800, 5: 86400}
    assert config.claim_lock_wait_seconds > 0
    assert config.default_max_concurrency >= 1


@pytest.mark.parametrize(
    "timeouts",
    [
        {"L1": "1800", "L2": "900"},          # out of order
        {"L1": "900", "L2": "1800", "L3": "1800"},  # equal, not strictly increasing
        {"L5": "0"},                          # not positive
        {"L3": "-60"},                        # negative
        {"L2": "abc"},                        # not an integer
    ],
)
def test_illegal_timeout_mappings_are_refused(clean_env, tmp_path, timeouts):
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))
    for level, value in timeouts.items():
        clean_env.setenv(f"HARNESSTASK_TIMEOUT_{level}", value)

    with pytest.raises(ConfigError) as excinfo:
        build_config()

    message = str(excinfo.value)
    # Either the mapping-consistency check or the per-level range/type check may fire first,
    # depending on which level is invalid; both name the offending variable.
    # The mapping-consistency message names the levels as "L1=.., L2=.."; the per-level
    # message names the environment variable. Both identify the offending configuration.
    assert "固定超时映射" in message or "HARNESSTASK_TIMEOUT_" in message
    assert any(token in message for token in ("超时", "整数", "大于等于"))


def test_split_database_configuration_is_refused(clean_env, tmp_path):
    """The two APIs must share one write database; per-API overrides must not be possible."""
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))

    for name in ("WEB_DATABASE_URL", "AGENT_SQLITE_PATH"):
        clean_env.setenv(name, "postgres://elsewhere/db")
        with pytest.raises(ConfigError) as excinfo:
            build_config()
        assert name in str(excinfo.value)
        assert "共用同一写数据库" in str(excinfo.value)
        clean_env.delenv(name, raising=False)


def test_relative_sqlite_path_is_refused(clean_env, tmp_path):
    clean_env.setenv("SQLITE_PATH", "var/app.sqlite3")  # relative: two processes could differ
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))

    with pytest.raises(ConfigError) as excinfo:
        build_config()

    assert "绝对路径" in str(excinfo.value)


def test_sqlite_path_with_a_missing_directory_is_refused(clean_env, tmp_path):
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "not-there" / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))

    with pytest.raises(ConfigError) as excinfo:
        build_config()

    assert "目录不存在" in str(excinfo.value)


def test_unwritable_file_storage_root_is_refused(clean_env, tmp_path):
    storage = tmp_path / "files"
    storage.mkdir()
    storage.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read+execute only: cannot create files
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(storage))

    try:
        with pytest.raises(ConfigError) as excinfo:
            build_config()
        assert "不可写" in str(excinfo.value)
    finally:
        storage.chmod(stat.S_IRWXU)


def test_missing_required_values_are_refused(clean_env, tmp_path):
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))

    clean_env.setenv("ALLOWED_HOSTS", " , ")
    with pytest.raises(ConfigError) as hosts_error:
        build_config()
    assert "ALLOWED_HOSTS" in str(hosts_error.value)

    clean_env.setenv("ALLOWED_HOSTS", "127.0.0.1")
    clean_env.setenv("REDIS_URL", "")
    with pytest.raises(ConfigError) as redis_error:
        build_config()
    assert "REDIS_URL" in str(redis_error.value)


def test_unknown_database_engine_is_refused(clean_env, tmp_path):
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))
    clean_env.setenv("DB_ENGINE", "postgres")

    with pytest.raises(ConfigError) as excinfo:
        build_config()

    assert "DB_ENGINE" in str(excinfo.value)


def test_mysql_engine_reads_its_own_connection_settings(clean_env, tmp_path):
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))
    clean_env.setenv("DB_ENGINE", "mysql")
    clean_env.setenv("MYSQL_DATABASE", "harvesstask_cfgtest")
    clean_env.setenv("MYSQL_USER", "cfguser")
    clean_env.setenv("MYSQL_HOST", "127.0.0.9")
    clean_env.setenv("MYSQL_PORT", "3399")

    config = build_config()

    assert config.is_mysql
    assert config.mysql_name == "harvesstask_cfgtest"
    assert config.mysql_user == "cfguser"
    assert config.mysql_host == "127.0.0.9"
    assert config.mysql_port == 3399
    # A relative SQLite path is irrelevant under MySQL but the file root is still validated.
    assert Path(config.file_storage_root).is_dir()


@pytest.mark.parametrize(
    ("name", "value", "needle"),
    [
        ("CLAIM_LOCK_WAIT_SECONDS", "0", "大于等于"),
        ("CLAIM_COMPENSATION_LIMIT", "0", "大于等于"),
        ("DEFAULT_MAX_CONCURRENCY", "0", "大于等于"),
        ("MAX_UPLOAD_BYTES", "-1", "大于等于"),
        ("TIMEOUT_SCAN_INTERVAL_SECONDS", "0", "大于等于"),
    ],
)
def test_zero_or_negative_operational_limits_are_refused(clean_env, tmp_path, name, value, needle):
    clean_env.setenv("SQLITE_PATH", str(tmp_path / "app.sqlite3"))
    clean_env.setenv("FILE_STORAGE_ROOT", str(tmp_path / "files"))
    clean_env.setenv(name, value)

    with pytest.raises(ConfigError) as excinfo:
        build_config()

    assert name in str(excinfo.value)
    assert needle in str(excinfo.value)


def test_settings_module_does_not_import_with_invalid_configuration(clean_env, tmp_path):
    """The same validation runs at Django start-up, so an invalid environment cannot boot."""
    import subprocess
    import sys

    environment = dict(os.environ)
    for name in BASE_CONFIGURED_VARS + ("DJANGO_SETTINGS_MODULE",):
        environment.pop(name, None)
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "SQLITE_PATH": str(tmp_path / "app.sqlite3"),
            "FILE_STORAGE_ROOT": str(tmp_path / "files"),
            "HARNESSTASK_TIMEOUT_L1": "9999",
            "HARNESSTASK_TIMEOUT_L2": "900",  # L2 below L1: illegal mapping
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        capture_output=True,
    )

    assert completed.returncode != 0, "非法配置不应允许 Django 启动"
    assert b"ImproperlyConfigured" in completed.stderr
