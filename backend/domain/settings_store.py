"""On-disk store for the runtime settings edited from the local WebUI (T43).

Kept free of Django imports so ``config.env`` can read the environment-variable overrides
at start-up without a circular import. The file is JSON, written atomically with owner-only
permissions because it may hold secrets (API keys, passwords) that must never be world
readable even on a single-user machine.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STORE_PATH = BASE_DIR / "var" / "runtime_settings.json"
PATH_ENV_VAR = "HARNESSTASK_RUNTIME_SETTINGS_PATH"

STORE_VERSION = 1


def store_path() -> Path:
    """Location of the settings file (overridable for tests via an env var)."""
    override = os.environ.get(PATH_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_STORE_PATH


def empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "env": {}, "llm": {}, "updated_at": None}


def read_store() -> dict[str, Any]:
    """Return the stored settings; a missing or unreadable file reads as empty.

    A corrupt settings file must never stop the services from starting: the file only
    carries overrides, so ignoring it degrades to "no override" instead of a crash.
    """
    path = store_path()
    if not path.is_file():
        return empty_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_store()
    if not isinstance(raw, dict):
        return empty_store()
    store = empty_store()
    env = raw.get("env")
    if isinstance(env, dict):
        store["env"] = {
            str(key): str(value) for key, value in env.items() if isinstance(value, (str, int, float))
        }
    llm = raw.get("llm")
    if isinstance(llm, dict):
        store["llm"] = {str(key): value for key, value in llm.items()}
    updated_at = raw.get("updated_at")
    store["updated_at"] = updated_at if isinstance(updated_at, str) else None
    return store


def write_store(store: dict[str, Any]) -> Path:
    """Atomically replace the settings file with ``store`` (mode 0600)."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".runtime_settings-", delete=False
    )
    try:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, path)
    return path


def env_overrides() -> dict[str, str]:
    """Environment-variable overrides saved from the WebUI."""
    return dict(read_store()["env"])
