"""T18 acceptance: atomic claim under the singleton global lock."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connections

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import IdempotencyRecord, OutboxEvent
from apps.tasks.models import Task, TaskExecution
from domain.claim_service import ClaimRequest, claim_task, web_claim_request
from domain.clock import db_now
from domain.contracts import (
    ActorContext,
    ActorType,
    ClaimMode,
    DIFFICULTY_TIMEOUT_SECONDS,
    TaskStatus,
)
from domain.errors import (
    CapacityExceededError,
    IdempotencyKeyReusedError,
    ResourceNotFoundError,
    TaskDifficultyMismatchError,
    VersionConflictError,
)
from domain.service_runner import run_request_service
from domain.transactions import run_write_service
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.claim, pytest.mark.api]


def do_claim(actor: ActorContext, request: ClaimRequest, key: str):
    return run_request_service(lambda: claim_task(actor, request, key), global_claim_lock=True)


def new_key() -> str:
    return str(uuid.uuid4())


def test_auto_claim_claims_exactly_one_task_and_reserves_one_slot(db):
    world = factories.World()
    task = factories.create_task(world.project_a, title="可领取任务", difficulty=2)
    actor = agent_actor(world.w1)

    result = do_claim(actor, ClaimRequest(levels=[1, 2]), new_key())

    assert result["data"] is not None
    data = result["data"]
    assert data["task_id"] == str(task.pk)
    assert data["status"] == TaskStatus.IN_PROGRESS.value
    assert data["execution_timeout_seconds"] == 1800
    assert data["assignee_agent_id"] == str(world.w1.pk)

    task.refresh_from_db()
    agent = Agent.objects.get(pk=world.w1.pk)
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.version == 2
    assert task.assignee_agent_id == world.w1.pk
    assert task.active_execution_id is not None
    assert agent.reserved_execution_count == 1

    execution = TaskExecution.objects.get(pk=task.active_execution_id)
    assert execution.sequence == 1
    assert execution.difficulty_snapshot == 2
    assert execution.execution_timeout_seconds == DIFFICULTY_TIMEOUT_SECONDS[2]
    delta = execution.execution_deadline_at - execution.claimed_at
    assert delta == timedelta(seconds=1800)
    assert execution.requirements_snapshot["title"] == "可领取任务"

    assert ActivityEvent.objects.filter(task=task, action="claimed").count() == 1
    assert OutboxEvent.objects.filter(entity_id=task.pk).count() == 1
    assert IdempotencyRecord.objects.filter(state="DONE", resource_id=task.pk).count() == 1


def test_all_zero_levels_return_empty_success_and_consume_no_capacity(db):
    world = factories.World()
    factories.create_task(world.project_a, title="不应被领取", difficulty=2)

    result = do_claim(agent_actor(world.w1), ClaimRequest(levels=[]), new_key())

    assert result["data"] is None
    assert result["reason"] == "NO_MATCHING_TASK"
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    assert TaskExecution.objects.count() == 0
    assert Task.objects.get(title="不应被领取").status == TaskStatus.TODO.value
    # The empty result is still recorded so the same key always returns the same answer.
    assert IdempotencyRecord.objects.filter(state="DONE", resource_id__isnull=True).count() == 1


def test_no_candidate_returns_empty_even_when_capacity_is_full(db):
    world = factories.World()
    # No TODO task at all, and w1's only slot is already taken.
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)

    result = do_claim(agent_actor(world.w1), ClaimRequest(levels=[1, 2, 3, 4, 5]), new_key())

    assert result["data"] is None
    assert result["reason"] == "NO_MATCHING_TASK"


def test_candidate_with_full_capacity_returns_capacity_exceeded(db):
    world = factories.World()
    factories.create_task(world.project_a, title="容量满时仍有候选", difficulty=1)
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)

    with pytest.raises(CapacityExceededError):
        do_claim(agent_actor(world.w1), ClaimRequest(levels=[1]), new_key())

    task = Task.objects.get(title="容量满时仍有候选")
    assert task.status == TaskStatus.TODO.value
    assert TaskExecution.objects.count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1


def test_specified_task_with_mismatched_level_is_409(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=5, title="L5 任务")

    with pytest.raises(TaskDifficultyMismatchError):
        do_claim(
            agent_actor(world.w1),
            ClaimRequest(levels=[1, 2], task_id=task.pk, expected_version=task.version),
            new_key(),
        )


def test_specified_task_outside_authorised_scope_is_404(db):
    world = factories.World()
    task = factories.create_task(world.project_b, difficulty=2, title="B 项目任务", claim_mode=ClaimMode.OPEN)

    with pytest.raises(ResourceNotFoundError):
        do_claim(
            agent_actor(world.w1),
            ClaimRequest(levels=[1, 2], task_id=task.pk, expected_version=task.version),
            new_key(),
        )


def test_specified_task_with_stale_version_is_409(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=2)

    with pytest.raises(VersionConflictError):
        do_claim(
            agent_actor(world.w1),
            ClaimRequest(levels=[1, 2], task_id=task.pk, expected_version=task.version + 5),
            new_key(),
        )


def test_assigned_to_someone_else_is_not_claimable(db):
    world = factories.World()
    task = factories.create_task(
        world.project_a, difficulty=2, claim_mode=ClaimMode.ASSIGNED, designated_agent=world.w2
    )

    auto = do_claim(agent_actor(world.w1), ClaimRequest(levels=[1, 2]), new_key())
    assert auto["data"] is None

    with pytest.raises(ResourceNotFoundError):
        do_claim(
            agent_actor(world.w1),
            ClaimRequest(levels=[1, 2], task_id=task.pk, expected_version=task.version),
            new_key(),
        )

    # The designated agent claims it.
    result = do_claim(agent_actor(world.w2), ClaimRequest(levels=[1, 2]), new_key())
    assert result["data"]["task_id"] == str(task.pk)


def test_replay_returns_original_execution_and_does_not_retime(db):
    world = factories.World()
    factories.create_task(world.project_a, difficulty=2)
    actor = agent_actor(world.w1)
    key = new_key()

    first = do_claim(actor, ClaimRequest(levels=[1, 2]), key)
    second = do_claim(actor, ClaimRequest(levels=[1, 2]), key)

    assert first["data"]["execution_id"] == second["data"]["execution_id"]
    assert first["data"]["execution_deadline_at"] == second["data"]["execution_deadline_at"]
    assert second["idempotent_replay"] is True
    assert TaskExecution.objects.count() == 1
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1


def test_same_key_with_different_parameters_is_rejected(db):
    world = factories.World()
    factories.create_task(world.project_a, difficulty=1)
    actor = agent_actor(world.w1)
    key = new_key()

    do_claim(actor, ClaimRequest(levels=[1]), key)
    with pytest.raises(IdempotencyKeyReusedError):
        do_claim(actor, ClaimRequest(levels=[1, 2]), key)


def test_replay_still_works_after_reassignment_when_read_access_remains(db):
    """A60/A11: a reassigned task must not hide a historical receipt the caller can read."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=2)
    actor = agent_actor(world.w1)
    key = new_key()
    first = do_claim(actor, ClaimRequest(levels=[1, 2]), key)
    execution_id = first["data"]["execution_id"]

    # Reclaim the task and let another agent take it.
    def reclaim():
        from domain.contracts import EndReason
        from domain.execution_service import end_execution

        return end_execution(
            agent_actor(world.s1),
            task_id=task.pk,
            execution_id=uuid.UUID(execution_id),
            reason=EndReason.RECLAIM,
            reason_text="改派",
            idem_key=new_key(),
        )

    run_request_service(lambda: reclaim(), require_write_lock=False)

    replay = do_claim(actor, ClaimRequest(levels=[1, 2]), key)
    assert replay["idempotent_replay"] is True
    assert replay["data"]["execution_id"] == execution_id
    assert TaskExecution.objects.count() == 1


@pytest.mark.timeout
def test_own_expired_slot_is_recovered_even_when_requesting_another_level(db):
    """A28: requesting L1 must still recover the agent's own expired L5 slot."""
    world = factories.World()
    l5_task = factories.create_task(world.project_a, difficulty=5, title="过期 L5")
    claimed_at = db_now() - timedelta(seconds=DIFFICULTY_TIMEOUT_SECONDS[5] + 60)

    execution = factories.create_execution(l5_task, world.w1, claimed_at=claimed_at, timeout_seconds=300)
    l5_task.status = TaskStatus.IN_PROGRESS.value
    l5_task.assignee_agent = world.w1
    l5_task.active_execution = execution
    l5_task.claimed_at = claimed_at
    l5_task.version = 2
    l5_task.save(update_fields=["status", "assignee_agent", "active_execution", "claimed_at", "version"])
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)

    factories.create_task(world.project_a, difficulty=1, title="新的 L1")

    result = do_claim(agent_actor(world.w1), ClaimRequest(levels=[1]), new_key())

    assert result["data"]["title"] == "新的 L1"
    l5_task.refresh_from_db()
    execution.refresh_from_db()
    assert execution.ended_at is not None
    assert execution.end_reason == "TIMEOUT"
    assert l5_task.status == TaskStatus.TODO.value
    assert l5_task.active_execution_id is None
    assert l5_task.assignee_agent_id is None
    assert l5_task.claim_mode == ClaimMode.OPEN.value
    # One slot released, one reserved for the new claim.
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1
    assert ActivityEvent.objects.filter(action="task.timeout_recovered").count() == 1


def test_web_proxy_claim_uses_the_same_lock_and_records_web_source(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=3, title="Web 代领任务")
    actor = ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))

    result = run_request_service(
        lambda: claim_task(
            actor,
            web_claim_request(
                agent_id=world.w2.pk, task_id=task.pk, expected_version=task.version
            ),
            new_key(),
        ),
        global_claim_lock=True,
    )

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.assignee_agent_id == world.w2.pk
    assert result["data"]["execution_timeout_seconds"] == DIFFICULTY_TIMEOUT_SECONDS[3]

    event = ActivityEvent.objects.get(task=task, action="claimed")
    assert event.actor_type == ActorType.WEB_OPERATOR.value
    assert event.actor_agent_id is None


def test_archived_project_tasks_are_not_claimable(db):
    world = factories.World()
    task = factories.create_task(world.archived_project, difficulty=2, title="归档项目任务")
    # s1 is a member of project B only; grant membership so visibility is not the blocker.
    factories.add_member(world.archived_project, world.s1)

    result = do_claim(agent_actor(world.s1), ClaimRequest(levels=[1, 2]), new_key())
    assert result["data"] is None or result["data"]["task_id"] != str(task.pk)


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_twenty_concurrent_claims_on_one_task_yield_exactly_one_winner(world):
    """A09: two API instances plus a web proxy claim, all contending for one task."""
    task = factories.create_task(world.project_a, difficulty=2, title="竞争任务")
    contenders = [world.w1, world.w2, world.s1, world.s2]
    barrier = threading.Barrier(20)
    successes: list[str] = []
    failures: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        agent = contenders[index % len(contenders)]
        try:
            barrier.wait(timeout=30)

            def service():
                actor = agent_actor(agent)
                if index % 5 == 4:
                    actor = ActorContext(
                        actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4())
                    )
                    request = web_claim_request(
                        agent_id=agent.pk, task_id=task.pk, expected_version=1
                    )
                else:
                    request = ClaimRequest(
                        levels=[2], task_id=task.pk, expected_version=1
                    )
                return claim_task(actor, request, str(uuid.uuid4()))

            result = run_request_service(service, global_claim_lock=True)
            with lock:
                successes.append(result["data"]["execution_id"])
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            with lock:
                failures.append(f"{type(exc).__name__}: {exc}")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    assert len(successes) == 1, f"successes={successes} failures={failures}"
    assert len(failures) == 19, failures

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert TaskExecution.objects.filter(task=task).count() == 1
    assert ActivityEvent.objects.filter(task=task, action="claimed").count() == 1
    assert OutboxEvent.objects.filter(entity_id=task.pk).count() == 1
    reserved = Agent.objects.get(pk=task.assignee_agent_id).reserved_execution_count
    assert reserved == 1


def _add_document_version(world, name: str):
    """Add one project document version and refresh the project's context version.

    Uses the real context recorder, so the frozen reference list has exactly one
    implementation (``domain.context_service``).
    """
    from apps.files.models import FileAsset
    from apps.projects.models import Document, DocumentVersion, Project, ProjectContextVersion
    from domain.context_service import record_context_version_locked

    asset = FileAsset.objects.create(
        storage_key=f"{uuid.uuid4().hex}.md",
        original_name=name,
        media_type="text/markdown",
        size=32,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        uploader_type="web_operator",
        project_id=world.project_a.pk,
        bound_at=db_now(),
    )
    document = Document.objects.create(project=world.project_a, name=name)
    version = DocumentVersion.objects.create(
        document=document,
        number=1,
        file_asset=asset,
        uploaded_by_type="web_operator",
    )
    Document.objects.filter(pk=document.pk).update(current_version=version)

    run_request_service(
        lambda: record_context_version_locked(Project.objects.get(pk=world.project_a.pk)),
        require_write_lock=False,
    )
    return document, version, ProjectContextVersion.objects.filter(project=world.project_a).order_by("-number").first()


def test_claim_snapshot_freezes_the_document_version_refs_at_claim_time(db):
    """A05: the claimed execution records which document versions it was based on."""
    world = factories.World()
    document_v1, version_v1, context_v1 = _add_document_version(world, "规格 V1.md")

    factories.create_task(world.project_a, difficulty=2, title="快照任务 V1")
    claimed = do_claim(agent_actor(world.w1), ClaimRequest(levels=[2]), new_key())
    execution = TaskExecution.objects.get(pk=claimed["data"]["execution_id"])

    assert execution.context_version_id == context_v1.pk
    refs = execution.requirements_snapshot["document_version_refs"]
    assert [ref["document_version_id"] for ref in refs] == [str(version_v1.pk)]
    assert refs[0]["document_id"] == str(document_v1.pk)
    assert refs[0]["name"] == "规格 V1.md"
    assert execution.requirements_snapshot["project_context_version"] == context_v1.number

    # A later document version must not rewrite the frozen evidence of the running execution.
    document_v2, version_v2, context_v2 = _add_document_version(world, "规格 V2.md")
    execution.refresh_from_db()
    frozen = execution.requirements_snapshot["document_version_refs"]
    assert [ref["document_version_id"] for ref in frozen] == [str(version_v1.pk)]
    assert execution.context_version_id == context_v1.pk

    # A new claim, taken after V2 exists, references the newer context instead.
    factories.create_task(world.project_a, difficulty=2, title="快照任务 V2")
    second = do_claim(agent_actor(world.w2), ClaimRequest(levels=[2]), new_key())
    second_execution = TaskExecution.objects.get(pk=second["data"]["execution_id"])
    assert second_execution.context_version_id == context_v2.pk
    later_refs = {
        ref["document_version_id"] for ref in second_execution.requirements_snapshot["document_version_refs"]
    }
    assert str(version_v2.pk) in later_refs
    assert str(version_v1.pk) in later_refs, "未移出参考列表的旧版本仍应保留在快照引用中"
