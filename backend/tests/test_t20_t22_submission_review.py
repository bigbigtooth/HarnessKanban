"""T20/T21/T22 acceptance: formal submission, review decisions, rework and reopen."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connections

from apps.files.models import AttachmentLink, FileAsset
from apps.identity.models import Agent
from apps.tasks.models import Task, TaskExecution, TaskReview, TaskSubmission
from domain.claim_service import ClaimRequest, claim_task
from domain.clock import db_now, override_db_clock
from domain.contracts import (
    ActorContext,
    ActorType,
    AttachmentPurpose,
    ClaimMode,
    ProjectStatus,
    ReviewDecision,
    TaskStatus,
)
from domain.errors import (
    ExecutionTimeoutError,
    InvalidArgumentError,
    ProjectNotActiveError,
    ReviewAlreadyDecidedError,
    RoleForbiddenError,
    SelfReviewForbiddenError,
    VersionConflictError,
)
from domain.review_service import reopen_task, review_submission
from domain.service_runner import run_request_service
from domain.submission_service import submit_result
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.review, pytest.mark.workflow, pytest.mark.api]


def new_key() -> str:
    return str(uuid.uuid4())


def claim(world, agent=None, *, difficulty: int = 2, task=None):
    agent = agent or world.w1
    task = task or factories.create_task(world.project_a, difficulty=difficulty)
    result = run_request_service(
        lambda: claim_task(agent_actor(agent), ClaimRequest(levels=[difficulty]), new_key()),
        global_claim_lock=True,
    )
    assert result["data"] is not None, result
    task.refresh_from_db()
    return task, TaskExecution.objects.get(pk=result["data"]["execution_id"])


def attach_artifact(execution, task, *, agent=None, name="report.md"):
    asset = FileAsset.objects.create(
        storage_key=f"{uuid.uuid4().hex}.md",
        original_name=name,
        media_type="text/markdown",
        size=128,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        uploader_type=ActorType.AGENT.value if agent else ActorType.WEB_OPERATOR.value,
        uploader_agent=agent,
        project_id=task.project_id,
    )
    AttachmentLink.objects.create(
        file_asset=asset,
        project_id=task.project_id,
        task_id=task.pk,
        execution_id=execution.pk,
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
    )
    return asset


def submit(actor, task, execution, *, key=None, payload=None, artifacts=()):
    body = {
        "execution_id": str(execution.pk),
        "expected_version": task.version,
        "summary": "已完成实现",
        "content": "变更说明与验证步骤",
        "self_check": [{"criterion": "验收标准一", "passed": True, "evidence": "记录"}],
        "links": [{"title": "交付链接", "url": "https://example.com/delivery/42"}],
        "artifacts": [{"file_id": str(item.pk)} for item in artifacts],
    }
    if payload:
        body.update(payload)
    # Domain write services require the outermost transaction; the HTTP adapters use the
    # same wrapper, so tests exercise the real boundary instead of calling services bare.
    return run_request_service(
        lambda: submit_result(actor, task_id=task.pk, payload=body, idem_key=key or new_key()),
        require_write_lock=False,
    )


def review(actor, task, submission, *, decision, comment="", key=None, payload=None):
    body = {
        "submission_id": str(submission.pk),
        "expected_version": task.version,
        "decision": decision,
        "comment": comment,
    }
    if payload:
        body.update(payload)
    return run_request_service(
        lambda: review_submission(
            actor, task_id=task.pk, payload=body, idem_key=key or new_key()
        ),
        require_write_lock=False,
    )


@pytest.mark.files
def test_submit_moves_to_in_review_releases_slot_and_freezes_evidence(db):
    world = factories.World()
    task, execution = claim(world)
    artifact = attach_artifact(execution, task, agent=world.w1)
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1

    result = submit(agent_actor(world.w1), task, execution, artifacts=[artifact])

    task.refresh_from_db()
    execution.refresh_from_db()
    assert task.status == TaskStatus.IN_REVIEW.value
    assert task.version == 3
    assert task.current_submission_id is not None
    assert task.assignee_agent_id == world.w1.pk
    assert execution.ended_at is not None
    assert execution.end_reason == "SUBMIT"
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0

    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    assert submission.artifact_refs == [str(artifact.pk)]
    assert submission.requirements_version == task.requirements_version
    assert submission.self_check[0]["criterion"] == "验收标准一"
    assert result["data"]["submission_id"] == str(submission.pk)
    assert TaskReview.objects.count() == 0

    # The submitted file is frozen as a submission artifact for cleanup/authorisation.
    assert AttachmentLink.objects.filter(
        file_asset=artifact, purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value
    ).count() == 1
    artifact.refresh_from_db()
    assert artifact.bound_at is not None


@pytest.mark.timeout
def test_submission_after_the_deadline_is_rejected(db):
    world = factories.World()
    task, execution = claim(world)

    with override_db_clock(execution.execution_deadline_at):
        with pytest.raises(ExecutionTimeoutError):
            submit(agent_actor(world.w1), task, execution, payload={"expected_version": task.version})

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert TaskSubmission.objects.count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1


@pytest.mark.files
def test_submission_rejects_unknown_and_foreign_artifacts(db):
    world = factories.World()
    task, execution = claim(world)

    with pytest.raises(InvalidArgumentError):
        submit(
            agent_actor(world.w1),
            task,
            execution,
            payload={"artifacts": [{"file_id": str(uuid.uuid4())}]},
        )

    # A file uploaded for someone else's execution must not be submittable.
    other_task, other_execution = claim(world, agent=world.w2, difficulty=2)
    foreign = attach_artifact(other_execution, other_task, agent=world.w2)
    with pytest.raises(InvalidArgumentError):
        submit(
            agent_actor(world.w1),
            task,
            execution,
            payload={"artifacts": [{"file_id": str(foreign.pk)}]},
        )

    assert TaskSubmission.objects.count() == 0


@pytest.mark.timeout
def test_replay_after_the_deadline_returns_the_original_submission(db):
    """A14: a lost response must be recoverable without a second submission or slot release."""
    world = factories.World()
    task, execution = claim(world)
    key = new_key()
    first = submit(agent_actor(world.w1), task, execution, key=key)
    submission_id = first["data"]["submission_id"]

    # Replay long after the original deadline elapsed.
    with override_db_clock(execution.execution_deadline_at + timedelta(days=2)):
        replay = submit(agent_actor(world.w1), task, execution, key=key)

    assert replay["idempotent_replay"] is True
    assert replay["data"]["submission_id"] == submission_id
    assert TaskSubmission.objects.count() == 1
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    task.refresh_from_db()
    assert task.status == TaskStatus.IN_REVIEW.value


def test_second_submission_for_the_same_execution_is_impossible(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)

    task.refresh_from_db()
    with pytest.raises(VersionConflictError):
        submit(
            agent_actor(world.w1),
            task,
            execution,
            payload={"expected_version": task.version - 1},
        )
    assert TaskSubmission.objects.count() == 1


def test_supervisor_approval_completes_the_task(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)

    result = review(agent_actor(world.s1), task, submission, decision="APPROVE", comment="已核对")

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE.value
    assert task.version == 4
    assert task.completed_at is not None
    assert task.assignee_agent_id == world.w1.pk
    assert task.current_submission_id == submission.pk
    assert result["data"]["review"]["reviewer_agent_id"] == str(world.s1.pk)
    assert result["data"]["review"]["reviewer_identity_verified"] is True
    # Approval never releases or consumes capacity.
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0


def test_self_review_and_worker_review_are_refused(db):
    world = factories.World()
    task, execution = claim(world, agent=world.s1, difficulty=2)
    submit(agent_actor(world.s1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)

    with pytest.raises(SelfReviewForbiddenError):
        review(agent_actor(world.s1), task, submission, decision="APPROVE")

    with pytest.raises(RoleForbiddenError):
        review(agent_actor(world.w1), task, submission, decision="APPROVE", payload={"reviewer_agent_id": str(world.w1.pk)})

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_REVIEW.value
    assert TaskReview.objects.count() == 0


def test_web_review_records_an_unverified_proxy_identity(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)

    web_actor = ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))
    result = review(
        web_actor,
        task,
        submission,
        decision="APPROVE",
        comment="本地管理代审核",
        payload={"reviewer_agent_id": str(world.s2.pk)},
    )

    review_row = TaskReview.objects.get(pk=result["data"]["review"]["review_id"])
    assert review_row.actor_type == ActorType.WEB_OPERATOR.value
    assert review_row.reviewer_agent_id == world.s2.pk
    assert review_row.reviewer_identity_verified is False


def test_web_review_must_choose_a_valid_supervisor(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    web_actor = ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))

    with pytest.raises(RoleForbiddenError):
        review(
            web_actor,
            task,
            submission,
            decision="APPROVE",
            payload={"reviewer_agent_id": str(world.w1.pk)},
        )

    disabled, _ = factories.create_agent("禁用主管", "SUPERVISOR", enabled=False)
    factories.add_member(world.project_a, disabled)
    with pytest.raises(InvalidArgumentError):
        review(
            web_actor,
            task,
            submission,
            decision="APPROVE",
            payload={"reviewer_agent_id": str(disabled.pk)},
        )


def test_reject_requires_reason_and_returns_the_task_to_todo_with_exclusive_assignment(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)

    with pytest.raises(InvalidArgumentError):
        review(agent_actor(world.s1), task, submission, decision="REJECT", comment="  ")

    result = review(
        agent_actor(world.s1),
        task,
        submission,
        decision="REJECT",
        comment="验收标准未满足",
    )

    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    assert task.current_submission_id is None
    assert task.assignee_agent_id is None
    assert task.active_execution_id is None
    assert task.claimed_at is None
    assert task.submitted_at is None
    assert task.claim_mode == ClaimMode.ASSIGNED.value
    assert task.designated_agent_id == world.w1.pk
    assert result["data"]["rework_assignment"] == "assigned_to_original_executor"
    # History is preserved and the slot was released only once.
    assert TaskSubmission.objects.count() == 1
    assert TaskReview.objects.count() == 1
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0


def test_only_one_review_decision_per_submission(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)

    review(agent_actor(world.s1), task, submission, decision="APPROVE", comment="通过")

    task.refresh_from_db()
    with pytest.raises(VersionConflictError):
        review(agent_actor(world.s2), task, submission, decision="REJECT", comment="驳回", payload={"expected_version": task.version})
    assert TaskReview.objects.count() == 1


@pytest.mark.claim
def test_rejected_submission_cannot_be_reviewed_again(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    review(agent_actor(world.s1), task, submission, decision="REJECT", comment="返工")

    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value
    with pytest.raises(VersionConflictError):
        review(agent_actor(world.s1), task, submission, decision="APPROVE")

    # The database itself enforces one review per submission, independent of the service.
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        run_request_service(
            lambda: TaskReview.objects.create(
                submission=submission,
                task=task,
                decision="APPROVE",
                comment="",
                actor_type=ActorType.AGENT.value,
                reviewer_agent=world.s1,
                reviewer_identity_verified=True,
                created_at=db_now(),
            )
        )
    assert TaskReview.objects.filter(submission=submission).count() == 1


@pytest.mark.claim
def test_two_round_rework_keeps_full_history_and_isolates_the_old_execution(db):
    """A16: reject -> re-claim -> resubmit -> approve, with both rounds preserved."""
    world = factories.World()
    task, first_execution = claim(world)
    submit(agent_actor(world.w1), task, first_execution)
    task.refresh_from_db()
    first_submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    review(agent_actor(world.s1), task, first_submission, decision="REJECT", comment="需要补充验证")

    task.refresh_from_db()
    assert task.claim_mode == ClaimMode.ASSIGNED.value
    assert task.designated_agent_id == world.w1.pk

    # Another worker cannot take the exclusive rework assignment.
    other = run_request_service(
        lambda: claim_task(agent_actor(world.w2), ClaimRequest(levels=[2]), new_key()),
        global_claim_lock=True,
    )
    assert other["data"] is None

    second = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[2]), new_key()),
        global_claim_lock=True,
    )
    second_execution = TaskExecution.objects.get(pk=second["data"]["execution_id"])
    assert second_execution.pk != first_execution.pk
    # A new claim gets a new execution with its own fixed deadline.
    assert second_execution.sequence == 2
    assert second_execution.execution_deadline_at > second_execution.claimed_at

    task.refresh_from_db()
    submit(agent_actor(world.w1), task, second_execution, payload={"summary": "第二轮补充完成"})
    task.refresh_from_db()
    second_submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    assert second_submission.pk != first_submission.pk

    review(agent_actor(world.s2), task, second_submission, decision="APPROVE", comment="通过")

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE.value
    assert TaskExecution.objects.filter(task=task).count() == 2
    assert TaskSubmission.objects.filter(task=task).count() == 2
    assert TaskReview.objects.filter(task=task).count() == 2
    # The old submission stays read-only and still readable.
    first_submission.refresh_from_db()
    assert first_submission.summary == "已完成实现"
    review_rows = TaskReview.objects.filter(task=task).order_by("created_at")
    assert [row.decision for row in review_rows] == ["REJECT", "APPROVE"]


@pytest.mark.claim
def test_old_execution_cannot_submit_into_the_new_round(db):
    world = factories.World()
    task, first_execution = claim(world)
    submit(agent_actor(world.w1), task, first_execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    review(agent_actor(world.s1), task, submission, decision="REJECT", comment="返工")

    task.refresh_from_db()
    with pytest.raises(VersionConflictError):
        submit(
            agent_actor(world.w1),
            task,
            first_execution,
            payload={"expected_version": task.version, "summary": "旧执行回写"},
        )
    assert TaskSubmission.objects.filter(task=task).count() == 1


def test_reopen_done_task_requires_active_project_and_reason(db):
    world = factories.World()
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    review(agent_actor(world.s1), task, submission, decision="APPROVE", comment="通过")
    task.refresh_from_db()
    assert task.status == TaskStatus.DONE.value

    with pytest.raises(InvalidArgumentError):
        reopen_task(agent_actor(world.s1), task_id=task.pk, reason="   ", expected_version=task.version, idem_key=new_key())

    world.project_a.status = ProjectStatus.COMPLETED.value
    world.project_a.save(update_fields=["status"])
    with pytest.raises(ProjectNotActiveError):
        reopen_task(
            agent_actor(world.s1),
            task_id=task.pk,
            reason="需要返工",
            expected_version=task.version,
            idem_key=new_key(),
        )

    world.project_a.status = ProjectStatus.ACTIVE.value
    world.project_a.save(update_fields=["status"])
    result = reopen_task(
        agent_actor(world.s1),
        task_id=task.pk,
        reason="客户要求补充",
        expected_version=task.version,
        idem_key=new_key(),
    )

    task.refresh_from_db()
    assert result["data"]["status"] == TaskStatus.TODO.value
    assert task.status == TaskStatus.TODO.value
    assert task.current_submission_id is None
    assert task.assignee_agent_id is None
    assert task.completed_at is None
    # Completed history is preserved.
    assert TaskSubmission.objects.filter(task=task).count() == 1
    assert TaskReview.objects.filter(task=task, decision="APPROVE").count() == 1


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.review
@pytest.mark.django_db(transaction=True)
def test_concurrent_approve_and_reject_allow_only_one_decision(world):
    task, execution = claim(world)
    submit(agent_actor(world.w1), task, execution)
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    expected_version = task.version

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(agent, decision: str) -> None:
        try:
            barrier.wait(timeout=20)
            run_request_service(
                lambda: review_submission(
                    agent_actor(agent),
                    task_id=task.pk,
                    payload={
                        "submission_id": str(submission.pk),
                        "expected_version": expected_version,
                        "decision": decision,
                        "comment": f"{decision} 决定",
                    },
                    idem_key=str(uuid.uuid4()),
                )
            )
            with lock:
                outcomes.append(decision)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=worker, args=(world.s1, "APPROVE")),
        threading.Thread(target=worker, args=(world.s2, "REJECT")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == 1, f"outcomes={outcomes} errors={errors}"
    assert len(errors) == 1, errors
    assert TaskReview.objects.filter(submission=submission).count() == 1
    task.refresh_from_db()
    expected_status = TaskStatus.DONE.value if outcomes[0] == "APPROVE" else TaskStatus.TODO.value
    assert task.status == expected_status


def test_web_proxy_submit_attributes_the_submission_to_the_executing_agent(db):
    """The local administrator may proxy a submission; it must still record a submitter.

    Regression: the proxied path previously passed ``submitted_by_agent=None`` into a NOT
    NULL column and the resulting IntegrityError was mis-mapped to SUBMISSION_EXISTS, so
    every web-proxied submit failed with a misleading 409.
    """
    from apps.audit.models import ActivityEvent

    world = factories.World()
    task, execution = claim(world)
    web_actor = ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))

    result = submit(web_actor, task, execution)

    task.refresh_from_db()
    assert task.status == TaskStatus.IN_REVIEW.value
    submission = TaskSubmission.objects.get(pk=task.current_submission_id)
    assert submission.submitted_by_agent_id == world.w1.pk
    assert submission.submitted_at is not None
    assert submission.execution_id == execution.pk

    # The proxied source stays visible in the audit trail.
    event = ActivityEvent.objects.get(task=task, action="task.submitted")
    assert event.actor_type == ActorType.WEB_OPERATOR.value
    assert event.actor_agent_id is None
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
    assert result["data"]["submission_id"] == str(submission.pk)


@pytest.mark.timeout
def test_web_proxy_submit_replay_after_the_deadline_returns_the_same_submission(db):
    import uuid as _uuid

    world = factories.World()
    task, execution = claim(world)
    web_actor = ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(_uuid.uuid4()))
    key = new_key()
    first = submit(web_actor, task, execution, key=key)

    with override_db_clock(execution.execution_deadline_at + timedelta(days=1)):
        replay = submit(web_actor, task, execution, key=key)

    assert replay["idempotent_replay"] is True
    assert replay["data"]["submission_id"] == first["data"]["submission_id"]
    assert TaskSubmission.objects.count() == 1
