"""T23 acceptance: task archiving, restoring and traceable history.

Archiving is an independent marker: it never becomes a sixth status, never deletes
evidence, and never lets a worker read a PREPARING task through a history endpoint.
"""

from __future__ import annotations

import json
import uuid

import pytest

from apps.audit.models import ActivityEvent
from apps.files.models import AttachmentLink, FileAsset
from apps.identity.models import Agent
from apps.tasks.models import Task, TaskExecution, TaskReview, TaskSubmission
from domain.claim_service import ClaimRequest, claim_task
from domain.clock import db_now
from domain.contracts import AttachmentPurpose, ClaimMode, ProjectStatus, TaskStatus
from domain.file_cleanup import cleanup_unreferenced_temp_files
from domain.review_service import review_submission
from domain.service_runner import run_request_service
from domain.submission_service import submit_result
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.workflow, pytest.mark.api]

HOST = {"HTTP_HOST": "127.0.0.1:8000"}
TASKS = "/api/web/v1/tasks"


@pytest.fixture
def client():
    from django.test import Client

    return Client()


def new_key() -> str:
    return str(uuid.uuid4())


def archive(client, task, *, reason="历史清理", version=None):
    return client.post(
        f"{TASKS}/{task.pk}/archive",
        data=json.dumps({"expected_version": task.version if version is None else version, "reason": reason}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )


def restore(client, task, *, version=None):
    return client.post(
        f"{TASKS}/{task.pk}/restore",
        data=json.dumps({"expected_version": task.version if version is None else version}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )


def body(response):
    return json.loads(response.content)


def complete_round(world, *, approver=None, difficulty=2):
    """Drive one real claim -> submit -> approve round and return the artefacts."""
    task = factories.create_task(world.project_a, difficulty=difficulty)
    claimed = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[difficulty]), new_key()),
        global_claim_lock=True,
    )
    execution = TaskExecution.objects.get(pk=claimed["data"]["execution_id"])
    task.refresh_from_db()
    submitted = run_request_service(
        lambda: submit_result(
            agent_actor(world.w1),
            task_id=task.pk,
            payload={
                "execution_id": str(execution.pk),
                "expected_version": task.version,
                "summary": "第一轮交付",
                "content": "说明",
                "self_check": [],
                "artifacts": [],
                "links": [],
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    submission = TaskSubmission.objects.get(pk=submitted["data"]["submission_id"])
    run_request_service(
        lambda: review_submission(
            agent_actor(approver or world.s1),
            task_id=task.pk,
            payload={
                "submission_id": str(submission.pk),
                "expected_version": task.version,
                "decision": "APPROVE",
                "comment": "通过",
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    return task, execution, submission


def _claim_only(world, *, difficulty=2, title="进行中"):
    """Claim a task for real and leave it IN_PROGRESS."""
    task = factories.create_task(world.project_a, difficulty=difficulty, title=title)
    claimed = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[difficulty]), new_key()),
        global_claim_lock=True,
    )
    task.refresh_from_db()
    return task, TaskExecution.objects.get(pk=claimed["data"]["execution_id"])


def _submitted_only(world, *, difficulty=2, title="待审核"):
    """Claim and submit for real, leaving the task IN_REVIEW with no decision yet."""
    task, execution = _claim_only(world, difficulty=difficulty, title=title)
    run_request_service(
        lambda: submit_result(
            agent_actor(world.w1),
            task_id=task.pk,
            payload={
                "execution_id": str(execution.pk),
                "expected_version": task.version,
                "summary": "待审核交付",
                "content": "",
                "self_check": [],
                "artifacts": [],
                "links": [],
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    return task, execution


def test_archive_is_refused_while_an_execution_or_review_is_live(db, client):
    world = factories.World()
    # This scenario holds two live executions for the same worker at once.
    world.w1.max_concurrency = 5
    world.w1.save(update_fields=["max_concurrency"])

    # PREPARING is archivable.
    preparing = factories.create_task(
        world.project_a, status=TaskStatus.PREPARING, difficulty=1, title="准备中"
    )
    ok = archive(client, preparing)
    assert ok.status_code == 200, ok.content
    preparing.refresh_from_db()
    assert preparing.is_archived is True
    assert preparing.status == TaskStatus.PREPARING.value

    # IN_PROGRESS with a live execution is not.
    in_progress, execution = _claim_only(world)
    assert in_progress.status == TaskStatus.IN_PROGRESS.value
    assert in_progress.active_execution_id == execution.pk
    blocked = archive(client, in_progress)
    assert blocked.status_code == 409, blocked.content
    assert body(blocked)["error"]["code"] == "INVALID_TRANSITION"
    in_progress.refresh_from_db()
    assert in_progress.is_archived is False
    assert in_progress.status == TaskStatus.IN_PROGRESS.value

    # IN_REVIEW is not archivable either, because it awaits a supervisor decision.
    in_review, _ = _submitted_only(world)
    assert in_review.status == TaskStatus.IN_REVIEW.value
    blocked_review = archive(client, in_review)
    assert blocked_review.status_code == 409, blocked_review.content
    in_review.refresh_from_db()
    assert in_review.is_archived is False

    # DONE is archivable: the evidence stays queryable.
    done_task, _, _ = complete_round(world)
    assert done_task.status == TaskStatus.DONE.value
    archived_done = archive(client, done_task)
    assert archived_done.status_code == 200, archived_done.content
    done_task.refresh_from_db()
    assert done_task.is_archived is True
    assert done_task.status == TaskStatus.DONE.value


def test_archive_requires_a_reason_and_a_current_version(db, client):
    world = factories.World()
    task = factories.create_task(world.project_a, status=TaskStatus.TODO, difficulty=1)

    no_reason = client.post(
        f"{TASKS}/{task.pk}/archive",
        data=json.dumps({"expected_version": task.version, "reason": "   "}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )
    assert no_reason.status_code == 400
    assert body(no_reason)["error"]["code"] == "INVALID_ARGUMENT"

    stale = archive(client, task, version=task.version + 7)
    assert stale.status_code == 409
    assert body(stale)["error"]["code"] == "VERSION_CONFLICT"

    task.refresh_from_db()
    assert task.is_archived is False
    assert task.version == 1
    assert ActivityEvent.objects.filter(task=task, action="task.archived").count() == 0


def test_archive_keeps_the_whole_history_and_formal_evidence(db, client, tmp_path):
    from django.test import override_settings

    with override_settings(FILE_STORAGE_ROOT=str(tmp_path)):
        world = factories.World()
        task, execution, submission = complete_round(world)
        assert task.status == TaskStatus.DONE.value

        # Attach a formal artifact to the submission so cleanup has something to consider.
        asset = FileAsset.objects.create(
            storage_key=f"{uuid.uuid4().hex}.md",
            original_name="delivery.md",
            media_type="text/markdown",
            size=10,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            uploader_type="agent",
            uploader_agent=world.w1,
            project_id=world.project_a.pk,
            bound_at=db_now(),
        )
        AttachmentLink.objects.create(
            file_asset=asset,
            project_id=world.project_a.pk,
            task_id=task.pk,
            execution_id=execution.pk,
            submission_id=submission.pk,
            purpose=AttachmentPurpose.SUBMISSION_ARTIFACT.value,
        )

        response = archive(client, task)
        assert response.status_code == 200, response.content

        task.refresh_from_db()
        assert task.is_archived is True
        # Archiving is a marker: the five-state status is untouched.
        assert task.status == TaskStatus.DONE.value
        assert task.current_submission_id == submission.pk
        assert TaskExecution.objects.filter(task=task).count() == 1
        assert TaskSubmission.objects.filter(task=task).count() == 1
        assert TaskReview.objects.filter(task=task, decision="APPROVE").count() == 1

        # Formal evidence survives an archiving + cleanup sweep.
        run_request_service(
            lambda: cleanup_unreferenced_temp_files(limit=50), require_write_lock=False
        )
        assert FileAsset.objects.filter(pk=asset.pk).exists()
        assert AttachmentLink.objects.filter(file_asset=asset).count() == 1

        # Nothing is deleted and no file reference is dropped.
        asset.refresh_from_db()
        assert asset.bound_at is not None


def test_restore_requires_an_editable_project_and_preserves_business_state(db, client):
    world = factories.World()
    task, execution, submission = complete_round(world)
    archive(client, task)
    task.refresh_from_db()
    assert task.is_archived is True

    world.project_a.status = ProjectStatus.ARCHIVED.value
    world.project_a.archived_from_status = ProjectStatus.ACTIVE.value
    world.project_a.save(update_fields=["status", "archived_from_status"])

    blocked = restore(client, task)
    assert blocked.status_code == 409
    task.refresh_from_db()
    assert task.is_archived is True

    world.project_a.status = ProjectStatus.ACTIVE.value
    world.project_a.save(update_fields=["status"])
    response = restore(client, task)
    assert response.status_code == 200, response.content

    task.refresh_from_db()
    assert task.is_archived is False
    # Restoring does not rewind the business status or drop history.
    assert task.status == TaskStatus.DONE.value
    assert task.current_submission_id == submission.pk
    assert TaskExecution.objects.filter(task=task).count() == 1
    assert TaskSubmission.objects.filter(task=task).count() == 1
    assert TaskReview.objects.filter(task=task).count() == 1


def test_archived_number_is_never_reused(db, client):
    world = factories.World()
    first = factories.create_task(world.project_a, status=TaskStatus.PREPARING, difficulty=1)
    first_number = first.number
    archive(client, first)

    later = factories.create_task(world.project_a, status=TaskStatus.PREPARING, difficulty=1)
    assert later.number != first_number
    assert Task.objects.filter(project=world.project_a, number=first_number).count() == 1


def test_history_endpoints_expose_both_rework_rounds(db, client):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=2)

    # Round 1: claim -> submit -> reject (exclusive assignment back to w1).
    first = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[2]), new_key()),
        global_claim_lock=True,
    )
    task.refresh_from_db()
    first_execution = TaskExecution.objects.get(pk=first["data"]["execution_id"])
    submitted = run_request_service(
        lambda: submit_result(
            agent_actor(world.w1),
            task_id=task.pk,
            payload={
                "execution_id": str(first_execution.pk),
                "expected_version": task.version,
                "summary": "第一轮",
                "content": "",
                "self_check": [],
                "artifacts": [],
                "links": [],
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    first_submission = TaskSubmission.objects.get(pk=submitted["data"]["submission_id"])
    run_request_service(
        lambda: review_submission(
            agent_actor(world.s1),
            task_id=task.pk,
            payload={
                "submission_id": str(first_submission.pk),
                "expected_version": task.version,
                "decision": "REJECT",
                "comment": "验收标准未满足",
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )

    # Round 2: re-claim -> submit -> approve.
    task.refresh_from_db()
    second = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[2]), new_key()),
        global_claim_lock=True,
    )
    second_execution = TaskExecution.objects.get(pk=second["data"]["execution_id"])
    task.refresh_from_db()
    submitted_again = run_request_service(
        lambda: submit_result(
            agent_actor(world.w1),
            task_id=task.pk,
            payload={
                "execution_id": str(second_execution.pk),
                "expected_version": task.version,
                "summary": "第二轮",
                "content": "",
                "self_check": [],
                "artifacts": [],
                "links": [],
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    second_submission = TaskSubmission.objects.get(pk=submitted_again["data"]["submission_id"])
    run_request_service(
        lambda: review_submission(
            agent_actor(world.s2),
            task_id=task.pk,
            payload={
                "submission_id": str(second_submission.pk),
                "expected_version": task.version,
                "decision": "APPROVE",
                "comment": "通过",
            },
            idem_key=new_key(),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    assert task.status == TaskStatus.DONE.value

    detail = client.get(f"{TASKS}/{task.pk}", **HOST)
    assert detail.status_code == 200, detail.content
    data = body(detail)["data"]
    assert len(data["execution_history"]) == 2
    assert len(data["submission_history"]) == 2
    assert len(data["review_history"]) == 2
    assert [row["decision"] for row in data["review_history"]] == ["REJECT", "APPROVE"]
    assert {row["summary"] for row in data["submission_history"]} == {"第一轮", "第二轮"}

    activity = client.get(f"{TASKS}/{task.pk}/activity", **HOST)
    assert activity.status_code == 200, activity.content
    actions = [item["action"] for item in body(activity)["data"]["items"]]
    assert "task.rejected" in actions
    assert "task.approved" in actions
    assert "claimed" in actions

    # History keeps being readable after archiving, and archiving adds its own event.
    archive(client, task)
    archived_activity = client.get(f"{TASKS}/{task.pk}/activity", **HOST)
    archived_actions = [item["action"] for item in body(archived_activity)["data"]["items"]]
    assert "archived" in archived_actions  # task_service records the bare action name
    assert "task.rejected" in archived_actions


def test_worker_cannot_read_a_preparing_task_through_history(db):
    """A worker must not reach PREPARING content via the detail or activity endpoints."""
    from domain.errors import ResourceNotFoundError
    from domain import read_task_query

    world = factories.World()
    preparing = factories.create_task(
        world.project_a, status=TaskStatus.PREPARING, difficulty=1, title="准备中机密"
    )
    worker_actor = agent_actor(world.w1)

    with pytest.raises(ResourceNotFoundError):
        run_request_service(
            lambda: read_task_query.get_task_detail(worker_actor, preparing.pk),
            require_write_lock=False,
        )
    with pytest.raises(ResourceNotFoundError):
        run_request_service(
            lambda: read_task_query.list_task_activity(worker_actor, preparing.pk),
            require_write_lock=False,
        )

    # The supervisor and the local administrator still can.
    supervisor_view = run_request_service(
        lambda: read_task_query.get_task_detail(agent_actor(world.s1), preparing.pk),
        require_write_lock=False,
    )
    assert supervisor_view["title"] == "准备中机密"


def test_outsider_and_unknown_task_history_are_404(db):
    from domain.errors import ResourceNotFoundError
    from domain import read_task_query

    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)

    with pytest.raises(ResourceNotFoundError):
        run_request_service(
            lambda: read_task_query.list_task_activity(agent_actor(world.u), task.pk),
            require_write_lock=False,
        )
    with pytest.raises(ResourceNotFoundError):
        run_request_service(
            lambda: read_task_query.list_task_activity(agent_actor(world.w1), uuid.uuid4()),
            require_write_lock=False,
        )


def test_archived_tasks_are_excluded_by_default_and_listable_on_request(db):
    """Archived work leaves the default board list but stays reachable on request."""
    from domain import read_task_query
    from domain.read_task_query import TaskListFilters

    world = factories.World()
    task = factories.create_task(world.project_a, status=TaskStatus.TODO, difficulty=1)
    actor = agent_actor(world.w1)

    def list_for(view_actor, *, include_archived):
        return run_request_service(
            lambda: read_task_query.list_tasks(
                view_actor,
                TaskListFilters(levels=tuple(range(1, 6)), include_archived=include_archived),
                page_number=1,
                page_size=50,
            ),
            require_write_lock=False,
        )

    before = list_for(actor, include_archived=False)
    assert str(task.pk) in {item["id"] for item in before["items"]}

    task.is_archived = True
    task.save(update_fields=["is_archived"])

    after = list_for(actor, include_archived=False)
    assert str(task.pk) not in {item["id"] for item in after["items"]}
    # Archiving never moves the task out of its five-state column.
    task.refresh_from_db()
    assert task.status == TaskStatus.TODO.value

    included = list_for(actor, include_archived=True)
    assert str(task.pk) in {item["id"] for item in included["items"]}
