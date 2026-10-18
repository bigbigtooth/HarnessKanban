"""T09 acceptance: an authenticated write context must not survive a key reset or revocation.

The guard is the important half of "authentication is not authorisation": a request that
authenticated with key version N must fail if the key is rotated or the membership is
revoked before the write lands, and it must fail *without* leaving a partial write behind.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connections

from apps.identity.models import Agent
from apps.projects.models import ProjectMember
from apps.tasks.models import TaskExecution
from domain.claim_service import ClaimRequest, claim_task
from domain.clock import db_now
from domain.contracts import ActorContext, ActorType, TaskStatus
from domain.errors import AgentTokenInvalidError, ResourceNotFoundError
from domain.execution_service import update_progress
from domain.permissions import authenticate_agent_token, load_actor_agent_locked
from domain.security import generate_agent_key, hash_agent_key
from domain.service_runner import run_request_service
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.identity, pytest.mark.api]


def rotate_key(agent: Agent) -> str:
    """Replace the stored digest and bump the version, as the rotate endpoint does."""
    new_key = generate_agent_key()
    Agent.objects.filter(pk=agent.pk).update(
        key_hash=hash_agent_key(new_key), key_version=agent.key_version + 1
    )
    agent.refresh_from_db()
    return new_key


def test_rotated_key_invalidates_the_write_context_before_the_write_lands(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=2)
    actor = agent_actor(world.w1)  # authenticated at key version 1
    assert actor.key_version == world.w1.key_version

    rotate_key(world.w1)

    with pytest.raises(AgentTokenInvalidError) as excinfo:
        run_request_service(
            lambda: claim_task(actor, ClaimRequest(levels=[1, 2]), str(uuid.uuid4())),
            global_claim_lock=True,
        )
    assert "Key" in str(excinfo.value) or "重置" in str(excinfo.value)

    # No partial write: the task, the executions and the slot are untouched.
    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.version == 1
    assert TaskExecution.objects.filter(task=task).count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0

    # The pre-rotation token no longer authenticates at all.
    old_token = world.w1_key
    with pytest.raises(AgentTokenInvalidError):
        authenticate_agent_token(old_token)
    assert Agent.objects.get(pk=world.w1.pk).key_version == 2


def test_a_rotated_key_still_serves_the_agent_that_holds_the_new_one(db):
    """Rotation must not strand the agent: the new key authenticates and can claim."""
    world = factories.World()
    factories.create_task(world.project_a, difficulty=2)
    new_key = rotate_key(world.w1)

    agent = authenticate_agent_token(new_key)
    assert agent.pk == world.w1.pk
    assert agent.key_version == 2

    fresh_actor = ActorContext(
        actor_type=ActorType.AGENT,
        request_id=str(uuid.uuid4()),
        agent_id=agent.pk,
        key_version=agent.key_version,
    )
    result = run_request_service(
        lambda: claim_task(fresh_actor, ClaimRequest(levels=[1, 2]), str(uuid.uuid4())),
        global_claim_lock=True,
    )
    assert result["data"] is not None, "新 Key 应能继续领取，且不改变既有执行截止"


def test_revoked_membership_stops_an_in_flight_write_without_side_effects(db):
    """A live execution must not accept writes once the agent is removed from the project."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=2)
    claimed = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[1, 2]), str(uuid.uuid4())),
        global_claim_lock=True,
    )
    execution = TaskExecution.objects.get(pk=claimed["data"]["execution_id"])
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1

    # Authorisation is revoked while the execution is still open.
    ProjectMember.objects.filter(project=world.project_a, agent=world.w1).delete()

    with pytest.raises(ResourceNotFoundError):
        run_request_service(
            lambda: update_progress(
                agent_actor(world.w1),
                task_id=task.pk,
                execution_id=execution.pk,
                message="撤权后写入",
                percent=10,
                idem_key=str(uuid.uuid4()),
            ),
            require_write_lock=False,
        )

    execution.refresh_from_db()
    assert execution.progress_message == ""
    assert execution.progress_percent is None
    assert execution.ended_at is None


def test_disabled_agent_cannot_continue_writing(db):
    world = factories.World()
    factories.create_task(world.project_a, difficulty=2)
    actor = agent_actor(world.w1)

    Agent.objects.filter(pk=world.w1.pk).update(enabled=False)

    with pytest.raises(Exception) as excinfo:
        run_request_service(
            lambda: load_actor_agent_locked(actor), require_write_lock=False
        )
    assert getattr(excinfo.value, "code", "") == "AGENT_DISABLED"
    assert TaskExecution.objects.count() == 0


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_key_reset_rejects_the_in_flight_write(world):
    """Real concurrency: authenticate, then rotate on another connection, then write."""
    task = factories.create_task(world.project_a, difficulty=2, title="T09 并发轮换")
    actor = agent_actor(world.w1)
    barrier = threading.Barrier(2)
    rotations: list[str] = []
    writes: list[str] = []
    guard = threading.Lock()

    def writer() -> None:
        try:
            barrier.wait(timeout=20)

            def service():
                return claim_task(actor, ClaimRequest(levels=[1, 2]), str(uuid.uuid4()))

            run_request_service(service, global_claim_lock=True)
            with guard:
                writes.append("claimed")
        except BaseException as exc:  # noqa: BLE001 - asserted below
            with guard:
                writes.append(type(exc).__name__)
        finally:
            connections.close_all()

    def rotator() -> None:
        try:
            barrier.wait(timeout=20)

            def service():
                new_key = generate_agent_key()
                Agent.objects.filter(pk=world.w1.pk).update(
                    key_hash=hash_agent_key(new_key), key_version=world.w1.key_version + 1
                )
                return new_key

            with guard:
                rotations.append(run_request_service(service))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=writer), threading.Thread(target=rotator)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(rotations) == 1, rotations
    # Depending on interleaving the write either lost to the rotation (rejected) or won and
    # committed first; what must never happen is a write that both succeeded and reflects the
    # stale key version.
    agent = Agent.objects.get(pk=world.w1.pk)
    if writes == ["claimed"]:
        check_consistent_after_win(task, agent)
    else:
        assert writes and writes[0] in ("AgentTokenInvalidError", "DatabaseBusyError"), writes
        task.refresh_from_db()
        assert task.status == TaskStatus.TODO.value
        assert TaskExecution.objects.filter(task=task).count() == 0
        assert agent.reserved_execution_count == 0


def check_consistent_after_win(task, agent: Agent) -> None:
    """If the claim committed first, the row set must still be exactly one coherent claim."""
    task.refresh_from_db()
    executions = TaskExecution.objects.filter(task=task)
    assert executions.count() == 1
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert agent.reserved_execution_count == 1
    execution = executions.first()
    assert execution.ended_at is None
    assert execution.execution_deadline_at > execution.claimed_at
    assert (execution.execution_deadline_at - execution.claimed_at) == timedelta(seconds=1800)
    assert db_now() >= execution.claimed_at
