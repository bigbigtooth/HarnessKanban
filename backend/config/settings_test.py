"""Settings for the pytest suite (``DJANGO_SETTINGS_MODULE`` in pyproject.toml).

pytest-django builds Django during ``pytest_load_initial_conftests`` — earlier than
any conftest can run — so the "no real OSS in unit tests" guard must live in a
dedicated settings module imported before :mod:`config.settings`. ``backend/.env``
may enable OSS for the running services; the suite must not mirror uploads to a real
bucket. Export ``HARNESSTASK_TEST_OSS=1`` to keep OSS enabled under pytest.
"""

from __future__ import annotations

import os

if os.environ.get("HARNESSTASK_TEST_OSS") != "1":
    os.environ["OSS_ENABLED"] = "false"

from config.settings import *  # noqa: E402,F401,F403
