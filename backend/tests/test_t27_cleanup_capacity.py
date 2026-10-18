"""T27 acceptance: temporary file cleanup and reserved-slot consistency."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.test import override_settings

from apps.audit.models import ActivityEvent
from apps.files.models import AttachmentLink, FileAsset
from apps.identity.models import Agent
from apps.tasks.models import TaskExecution
from domain.capacity_audit import audit_capacity_slots
from domain.clock import db_now
from domain.contracts import ActorType, AttachmentPurpose, ProjectStatus
from domain.file_cleanup import cleanup_unreferenced_temp_files, sweep_orphan_staging_files
from domain.service_runner import run_request_service
from tests import factories

pytestmark = [pytest.mark.files, pytest.mark.jobs]


def make_asset(
    project,
    *,
    age_hours: int = 48,
    bound: bool = False,
    write_file: bool = True,
    uploader=None,
):
    key = f"{uuid.uuid4().hex}.md"
    asset = FileAsset.objects.create(
        storage_key=key,
        original_name="report.md",
        media_type="text/markdown",
        size=10,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        uploader_type=ActorType.AGENT.value,
        uploader_agent=uploader,
        project_id=project.pk,
        bound_at=db_now() if bound else None,
    )
    if age_hours:
        old = db_now() - timedelta(hours=age_hours)
        FileAsset.objects.filter(pk=asset.pk).update(created_at=old)
        asset.refresh_from_db()
    if write_file:
        path = _object_dir() / key
        path.write_bytes(b"staged")
    return asset


def _object_dir():
    from domain.file_service import object_directory

    return object_directory()


def test_aged_unreferenced_staging_file_is_removed(db, tmp_path):
    with override_settings(FILE_STORAGE_ROOT=str(tmp_path)):
        world = factories.World()
        asset = make_asset(world.project_a, age_hours=48)
        path = _object_dir() / asset.storage_key
        assert path.exists()

        result = run_request_service(
            lambda: cleanup_unreferenced_temp_files(limit=50), require_write_lock=False
        )

    assert result["deleted"] == 1
    assert not FileAsset.objects.filter(pk=asset.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_disk_file_is_unlinked_only_after_the_transaction_commits(tmp_path):
    with override_settings(FILE_STORAGE_ROOT=str(tmp_path)):
        factories.World()
        project = factories.create_project("清理项目", status=ProjectStatus.ACTIVE)
        asset = make_asset(project, age_hours=48)
        path = _object_dir() / asset.storage_key
        assert path.exists()

        run_request_service(lambda: cleanup_unreferenced_temp_files(limit=50), require_write_lock=False)

        assert not FileAsset.objects.filter(pk=asset.pk).exists()
        assert not path.exists(), "提交后应删除磁盘文件"


def test_recent_staging_file_is_kept(db, tmp_path):
    with override_settings(FILE_STORAGE_ROOT=str(tmp_path)):
        world = factories.World()
        asset = make_asset(world.project_a, age_hours=1)

        result = run_request_service(
            lambda: cleanup_unreferenced_temp_files(limit=50), require_write_lock=False
        )

    assert result["deleted"] == 0
    assert FileAsset.objects.filter(pk=asset.pk).exists()


def test_bound_and_referenced_files_are_never_deleted(db, tmp_path):
    with override_settings(FILE_STORAGE_ROOT=str(tmp_path)):
        world = factories.World()
        task = factories.create_task(world.project_a, difficulty=1)
        now = db_now()
        execution = factories.create_execution(task, world.w1, claimed_at=now)

        bound_asset = make_asset(world.project_a, age_hours=96, bound=True)
        referenced = make_asset(world.project_a, age_hours=96, uploader=world.w1)
        AttachmentLink.objects.create(
            file_asset=referenced,
            project_id=world.project_a.pk,
            task_id=task.pk,
            execution_id=execution.pk,
            purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
        )

        result = run_request_service(
            lambda: cleanup_unreferenced_temp_files(limit=50), require_write_lock=False
        )

    assert result["deleted"] == 0
    assert FileAsset.objects.filter(pk__in=[bound_asset.pk, referenced.pk]).count() == 2
    # The referenced-but-unflagged asset is corrected rather than deleted.
    referenced.refresh_from_db()
    assert referenced.bound_at is not None


def test_orphan_staging_file_without_a_database_row_is_removed(db, tmp_path):
    with override_settings(FILE_STORAGE_ROOT=str(tmp_path)):
        factories.World()
        orphan = _object_dir() / f"{uuid.uuid4().hex}.md"
        orphan.write_bytes(b"orphan")

        result = sweep_orphan_staging_files(limit=50)

        assert result["orphans_removed"] >= 1
        assert not orphan.exists()


def test_capacity_audit_repairs_drift_and_records_a_system_event(db):
    """A slot count higher than the real open executions is repaired with evidence.

    ``ck_agent_reserved_within_limit`` means the count can never exceed max_concurrency, so
    the reachable drift is a stale count inside the limit (here 1 reserved, 0 executions).
    """
    world = factories.World()
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)

    result = run_request_service(
        lambda: audit_capacity_slots(limit=50), require_write_lock=False
    )

    assert result["repaired"] == 1
    agent = Agent.objects.get(pk=world.w1.pk)
    assert agent.reserved_execution_count == 0
    event = ActivityEvent.objects.get(action="agent.capacity_repaired")
    assert event.actor_type == ActorType.SYSTEM.value
    assert event.before["reserved_execution_count"] == 1
    assert event.after["reserved_execution_count"] == 0
    assert "已按真实执行数修复" in event.reason


def test_capacity_audit_counts_expired_but_unreclaimed_executions(db):
    """An expired execution that was not reclaimed still holds its slot."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    claimed_at = db_now() - timedelta(hours=5)
    factories.create_execution(task, world.w1, claimed_at=claimed_at, timeout_seconds=60)
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=0)

    result = run_request_service(
        lambda: audit_capacity_slots(limit=50), require_write_lock=False
    )

    assert result["repaired"] == 1
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1


def test_capacity_audit_is_a_no_op_for_consistent_agents(db):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    now = db_now()
    execution = factories.create_execution(task, world.w1, claimed_at=now)
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)
    ActivityEvent.objects.all().delete()

    first = run_request_service(lambda: audit_capacity_slots(limit=50), require_write_lock=False)
    second = run_request_service(lambda: audit_capacity_slots(limit=50), require_write_lock=False)

    assert first["repaired"] == 0
    assert second["repaired"] == 0
    assert ActivityEvent.objects.filter(action="agent.capacity_repaired").count() == 0
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 1
    # The ended execution no longer holds a slot.
    TaskExecution.objects.filter(pk=execution.pk).update(ended_at=db_now(), end_reason="RELEASE")
    run_request_service(lambda: audit_capacity_slots(limit=50), require_write_lock=False)
    assert Agent.objects.get(pk=world.w1.pk).reserved_execution_count == 0
