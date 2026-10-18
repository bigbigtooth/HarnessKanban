"""T35/T36 deployment acceptance plus the MySQL-only concurrency selectors.

These cases exist to satisfy the acceptance selectors the task cards name explicitly:
``mysql and deployment`` (T03 single lock row survives deployment, T35 shared write database
and UTC), ``deployment and files`` (T36 private file storage is not publicly served),
``mysql and concurrency and jobs`` (T26 two dispatchers cannot publish one event twice) and
``mysql and concurrency and fault`` (T19/A29 a held claim lock refuses rather than degrading).
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.conf import settings as django_settings
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder

from apps.jobs.models import OutboxEvent
from apps.tasks.models import Task, TaskClaimLock, TaskExecution
from domain.claim_service import ClaimRequest, claim_task
from domain.clock import db_now
from domain.contracts import DeliveryState, TaskStatus
from domain.errors import ClaimLockTimeoutError
from domain.events import enqueue_outbox
from domain.service_runner import run_request_service
from domain.transactions import run_write_service
from jobs import tasks as job_tasks
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.deployment]


# --------------------------------------------------------------------------------------
# mysql and deployment — T03 single lock row, T35 shared write database and UTC
# --------------------------------------------------------------------------------------


@pytest.mark.mysql
def test_the_single_lock_row_comes_from_the_data_migration(db):
    """A deployed database must own exactly one lock row, created by the migration flow."""
    applied = MigrationRecorder(connections["default"]).migration_qs.filter(
        app="tasks", name="0002_claim_lock_row"
    )
    assert applied.exists(), "数据迁移未记录，锁行可能由运行时代码创建"

    rows = list(TaskClaimLock.objects.values_list("id", "revision"))
    assert len(rows) == 1, f"锁行必须恰有一条，实际 {rows}"
    assert rows[0][0] == 1

    # Re-deploying (re-running the creation) must not add a second row.
    factories.ensure_claim_lock()
    factories.ensure_claim_lock()
    assert TaskClaimLock.objects.count() == 1


@pytest.mark.mysql
def test_both_apis_share_one_write_database_and_read_utc(db):
    """No second database alias and no per-API database: one vendor, one clock."""
    assert connections["default"].vendor == "mysql"
    assert set(django_settings.DATABASES) == {"default"}, "不得为两套 API 配置独立数据库"

    options = django_settings.DATABASES["default"]["OPTIONS"]
    assert options.get("isolation_level") == "read committed"
    assert "innodb_lock_wait_timeout" in options.get("init_command", "")

    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT UTC_TIMESTAMP(6)")
        database_now = cursor.fetchone()[0]
    adapter_now = db_now()
    assert adapter_now.utcoffset() == timedelta(0)
    # The adapter reads the database clock, not the process clock.
    assert abs((adapter_now.replace(tzinfo=None) - database_now).total_seconds()) < 5


@pytest.mark.mysql
def test_a_missing_lock_row_is_restored_and_the_claim_holds_it(db):
    """A wiped singleton is restored, then the claim locks that same row."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1, title="缺锁任务")
    TaskClaimLock.objects.filter(id=1).delete()

    result = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[1]), str(uuid.uuid4())),
        global_claim_lock=True,
    )

    task.refresh_from_db()
    assert result["data"]["task_id"] == str(task.pk)
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert TaskExecution.objects.filter(task=task).count() == 1
    assert TaskClaimLock.objects.get(id=1).revision == 1


# --------------------------------------------------------------------------------------
# deployment and files — the private store is not published
# --------------------------------------------------------------------------------------


@pytest.mark.mysql
@pytest.mark.files
def test_private_file_storage_is_not_served_publicly(db):
    """Files are reachable only through the authorised API, never as a public path."""
    from django.urls import get_resolver

    root = str(django_settings.FILE_STORAGE_ROOT)
    # ``MEDIA_URL`` normalises to "/" when unset, so the meaningful check is the pairing:
    # the private store must never be what a public media prefix would serve.
    media_root = getattr(django_settings, "MEDIA_ROOT", "")
    assert not media_root, f"不得配置 MEDIA_ROOT（当前 {media_root!r}）"
    assert str(media_root or "") != root, "不得把私有存储目录配成公开 media 根"

    patterns = []

    def walk(resolver) -> None:
        for entry in resolver.url_patterns:
            patterns.append(str(getattr(entry, "pattern", entry)))
            nested = getattr(entry, "url_patterns", None)
            if nested:
                walk(entry)

    walk(get_resolver())
    joined = " ".join(patterns)
    assert "media" not in joined.lower(), patterns
    assert root not in joined, "URL 路由不得引用私有存储路径"
    assert "files/<str:file_id>/download" in joined, "下载必须经受控端点"

    # The storage root is a real, private directory used by the file service.
    from domain.file_service import object_directory, staging_directory

    assert str(object_directory()).startswith(root)
    assert str(staging_directory()).startswith(root)


# --------------------------------------------------------------------------------------
# mysql and concurrency and jobs — T26 two dispatchers, one publish
# --------------------------------------------------------------------------------------


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.jobs
@pytest.mark.django_db(transaction=True)
def test_two_dispatchers_publish_an_event_only_once(world):
    """A dispatch lease means concurrent dispatchers cannot double-publish one event."""
    task = factories.create_task(world.project_a, difficulty=1, title="派发竞争")
    event = run_request_service(
        lambda: enqueue_outbox("task.claimed", task.pk, {"task_id": str(task.pk)}),
        require_write_lock=False,
    )
    barrier = threading.Barrier(2)
    published: list[int] = []
    guard = threading.Lock()

    def dispatcher() -> None:
        try:
            barrier.wait(timeout=20)
            monkeypatch_target = job_tasks._publish_to_broker  # keep the real function reference
            assert monkeypatch_target is not None
            result = job_tasks.outbox_dispatch(batch=5)
            with guard:
                published.append(int(result.get("published", 0)))
        except BaseException:  # noqa: BLE001 - recorded as zero published
            with guard:
                published.append(-1)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=dispatcher) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(published) == 2, published
    assert sum(count for count in published if count > 0) == 1, f"事件被重复派发：{published}"

    event.refresh_from_db()
    assert event.processed_at is None
    assert event.attempts == 1, f"只应记录一次派发尝试（实际 {event.attempts}）"
    assert event.delivery_state == DeliveryState.PUBLISHED.value


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.jobs
def test_publishing_an_already_processed_event_is_a_no_op(db):
    """A redelivery after consumption must not resurrect the event or repeat its effect."""
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)
    event = run_request_service(
        lambda: enqueue_outbox("task.claimed", task.pk, {}), require_write_lock=False
    )
    OutboxEvent.objects.filter(pk=event.pk).update(
        processed_at=db_now(), delivery_state=DeliveryState.PROCESSED.value
    )

    job_tasks._record_failure(event.pk, "迟到的失败")

    event.refresh_from_db()
    assert event.processed_at is not None
    assert event.delivery_state == DeliveryState.PROCESSED.value
    assert event.last_error == ""


# --------------------------------------------------------------------------------------
# mysql and concurrency and fault — T19/A29 a held lock refuses, never degrades
# --------------------------------------------------------------------------------------


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.fault
@pytest.mark.claim
@pytest.mark.django_db(transaction=True)
def test_a_held_claim_lock_returns_a_retryable_error_and_claims_nothing(world):
    """While another connection holds the singleton row, a claim must fail with 503."""
    import pymysql

    task = factories.create_task(world.project_a, difficulty=1, title="持锁期间的领取")
    # The tests run against the test database, so the holder must lock the row there.
    database_name = connections["default"].settings_dict["NAME"]
    holder = pymysql.connect(
        host="127.0.0.1", port=3306, user="root", password="", database=database_name, autocommit=False
    )
    try:
        with holder.cursor() as cursor:
            cursor.execute("UPDATE tasks_claim_lock SET revision = revision + 1 WHERE id = 1")
            assert cursor.rowcount == 1

        actor = agent_actor(world.w1)
        with pytest.raises(ClaimLockTimeoutError) as excinfo:
            run_request_service(
                lambda: claim_task(actor, ClaimRequest(levels=[1]), str(uuid.uuid4())),
                global_claim_lock=True,
            )
        assert getattr(excinfo.value, "http_status", None) == 503

        task.refresh_from_db()
        assert task.status == TaskStatus.TODO.value
        assert TaskExecution.objects.filter(task=task).count() == 0
        assert (
            __import__("apps.identity.models", fromlist=["Agent"]).Agent.objects.get(pk=world.w1.pk)
            .reserved_execution_count
            == 0
        )
    finally:
        holder.rollback()
        holder.close()
        connections.close_all()

    # Once the holder releases, the same instance claims normally.
    claimed = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[1]), str(uuid.uuid4())),
        global_claim_lock=True,
    )
    assert claimed["data"]["task_id"] == str(task.pk)
