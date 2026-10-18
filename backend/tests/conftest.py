"""Shared pytest fixtures.

The suite runs against whatever database ``DB_ENGINE`` points at. Tests marked ``mysql``
require a real MySQL connection and are skipped (never silently passed) on SQLite.
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection

from domain.contracts import ActorContext, ActorType
from tests import factories


@pytest.fixture
def world(db):
    """Public acceptance fixture from DEVELOP D20."""
    factories.ensure_claim_lock()
    return factories.World()


@pytest.fixture
def claim_lock(db):
    return factories.ensure_claim_lock()


@pytest.fixture
def web_actor() -> ActorContext:
    return ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))


@pytest.fixture
def system_actor() -> ActorContext:
    return ActorContext(actor_type=ActorType.SYSTEM, request_id=str(uuid.uuid4()))


def agent_actor(agent, key_version: int | None = None) -> ActorContext:
    return ActorContext(
        actor_type=ActorType.AGENT,
        request_id=str(uuid.uuid4()),
        agent_id=agent.pk,
        key_version=key_version if key_version is not None else agent.key_version,
    )


@pytest.fixture
def agent_actor_factory():
    return agent_actor


def pytest_collection_modifyitems(config, items):
    """Never let a MySQL-only case 'pass' by running against SQLite."""
    if connection.vendor == "mysql":
        return
    skip_mysql = pytest.mark.skip(
        reason=f"需要真实 MySQL（当前连接类型：{connection.vendor}）"
    )
    for item in items:
        if "mysql" in item.keywords:
            item.add_marker(skip_mysql)
