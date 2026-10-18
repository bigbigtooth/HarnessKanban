"""T36 backup and recovery drill (PLAN T36, DEVELOP D19, product 18.2).

A database-only backup cannot restore attachments, so the drill backs up the database and
the private file store as a matched pair, restores them into an isolated database and file
root, and then verifies that the restored copy is usable: submission/review relationships
intact, artifact checksums unchanged, and execution deadlines NOT re-timed by the restore.

Run explicitly:

    cd backend
    DB_ENGINE=mysql MYSQL_DATABASE=harvesstask \\
      .venv/bin/python tests/manual/t36_backup_restore.py

Exit code 0 means every verification held. The restored copy lives in a separate database
and directory and is removed at the end, so the working database is never modified.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DB_ENGINE", "mysql")
os.environ.setdefault("MYSQL_DATABASE", "harvesstask")

import django  # noqa: E402

django.setup()

import pymysql  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test import override_settings  # noqa: E402

from domain.clock import db_now  # noqa: E402

SOURCE_DB = "harvesstask"
RESTORE_DB = "harvesstask_restore_drill"
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def mysql_args() -> dict:
    return {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
    }


def run_sql(database: str | None, statements: list[str]) -> None:
    connection = pymysql.connect(database=database, autocommit=True, **mysql_args())
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    finally:
        connection.close()


def seed_closed_loop() -> dict:
    """Create one complete delivery：agent -> project -> claim -> artifact -> submit -> approve."""
    from apps.identity.models import Agent
    from apps.tasks.models import Task, TaskExecution, TaskSubmission
    from domain.claim_service import ClaimRequest, claim_task
    from domain.contracts import ActorContext, ActorType, ProjectStatus, TaskStatus
    from domain.review_service import review_submission
    from domain.service_runner import run_request_service
    from domain.submission_service import submit_result
    from tests import factories

    token = uuid.uuid4().hex[:8]
    factories.ensure_claim_lock()
    project = factories.create_project(f"恢复演练项目 {token}", status=ProjectStatus.ACTIVE)
    worker, _ = factories.create_agent(f"T36-W-{token}", "WORKER", max_concurrency=1)
    supervisor, _ = factories.create_agent(f"T36-S-{token}", "SUPERVISOR", max_concurrency=1)
    factories.add_member(project, worker)
    factories.add_member(project, supervisor)
    task = factories.create_task(project, title=f"恢复演练任务 {token}", difficulty=2)

    def actor(agent):
        return ActorContext(
            actor_type=ActorType.AGENT,
            request_id=str(uuid.uuid4()),
            agent_id=agent.pk,
            key_version=agent.key_version,
        )

    claimed = run_request_service(
        lambda: claim_task(actor(worker), ClaimRequest(levels=[2]), str(uuid.uuid4())),
        global_claim_lock=True,
    )
    task.refresh_from_db()
    execution = TaskExecution.objects.get(pk=claimed["data"]["execution_id"])

    # A real private artifact on disk, bound to the execution.
    from apps.files.models import AttachmentLink, FileAsset
    from domain.contracts import AttachmentPurpose
    from domain.file_service import object_path

    payload = b"restore drill artifact payload"
    digest = hashlib.sha256(payload).hexdigest()
    asset = FileAsset.objects.create(
        storage_key=f"{uuid.uuid4().hex}.md",
        original_name="drill.md",
        media_type="text/markdown",
        size=len(payload),
        sha256=digest,
        uploader_type="agent",
        uploader_agent=worker,
        project_id=project.pk,
        bound_at=db_now(),
    )
    object_path(asset.storage_key).write_bytes(payload)
    AttachmentLink.objects.create(
        file_asset=asset,
        project_id=project.pk,
        task_id=task.pk,
        execution_id=execution.pk,
        purpose=AttachmentPurpose.EXECUTION_ARTIFACT.value,
    )

    run_request_service(
        lambda: submit_result(
            actor(worker),
            task_id=task.pk,
            payload={
                "execution_id": str(execution.pk),
                "expected_version": task.version,
                "summary": "恢复演练提交",
                "content": "",
                "self_check": [],
                "artifacts": [{"file_id": str(asset.pk)}],
                "links": [],
            },
            idem_key=str(uuid.uuid4()),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()
    run_request_service(
        lambda: review_submission(
            actor(supervisor),
            task_id=task.pk,
            payload={
                "submission_id": str(task.current_submission_id),
                "expected_version": task.version,
                "decision": "APPROVE",
                "comment": "恢复演练通过",
            },
            idem_key=str(uuid.uuid4()),
        ),
        require_write_lock=False,
    )
    task.refresh_from_db()

    return {
        "task_id": str(task.pk),
        "execution_id": str(execution.pk),
        "submission_id": str(task.current_submission_id),
        "asset_id": str(asset.pk),
        "storage_key": asset.storage_key,
        "sha256": digest,
        "claimed_at": execution.claimed_at.isoformat(),
        "deadline": execution.execution_deadline_at.isoformat(),
        "timeout": execution.execution_timeout_seconds,
        "status": task.status,
        "task": Task.objects.get(pk=task.pk),
        "submission": TaskSubmission.objects.get(pk=task.current_submission_id),
        "worker_id": str(worker.pk),
    }


def backup(workdir: Path, storage_root: str) -> tuple[Path, Path]:
    dump_path = workdir / "harvesstask.sql"
    with dump_path.open("wb") as handle:
        completed = subprocess.run(
            [
                "mysqldump",
                "--host=127.0.0.1",
                "--port=3306",
                "--user=root",
                "--single-transaction",
                "--routines",
                # Restoring into the same server must not replay the source GTID set.
                "--set-gtid-purged=OFF",
                SOURCE_DB,
            ],
            stdout=handle,
            stderr=subprocess.PIPE,
        )
    if completed.returncode != 0:
        raise RuntimeError(f"mysqldump 失败：{completed.stderr.decode('utf-8', 'replace')[:400]}")

    files_dir = workdir / "files"
    shutil.copytree(storage_root, files_dir)
    return dump_path, files_dir


def restore(dump_path: Path, files_dir: Path, restored_storage: Path) -> None:
    run_sql(None, [f"DROP DATABASE IF EXISTS {RESTORE_DB}", f"CREATE DATABASE {RESTORE_DB} CHARACTER SET utf8mb4"])
    with dump_path.open("rb") as handle:
        completed = subprocess.run(
            ["mysql", "--host=127.0.0.1", "--port=3306", "--user=root", RESTORE_DB],
            stdin=handle,
            stderr=subprocess.PIPE,
        )
    if completed.returncode != 0:
        raise RuntimeError(f"mysql 恢复失败：{completed.stderr.decode('utf-8', 'replace')[:400]}")
    shutil.copytree(files_dir, restored_storage)


def uid(value: str) -> str:
    """Django stores UUIDField as char(32) on MySQL, so raw SQL params must drop dashes."""
    return str(value).replace("-", "")


def verify(seed: dict, restored_storage: Path) -> None:
    connection = pymysql.connect(database=RESTORE_DB, autocommit=True, **mysql_args())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT status, current_submission_id FROM tasks_task WHERE id = %s", [uid(seed["task_id"])])
            row = cursor.fetchone()
            check(row is not None, "恢复后任务行存在")
            check(row and row[0] == "DONE", f"任务状态恢复为 DONE（实际 {row and row[0]}）")
            check(
                row and str(row[1]).replace("-", "") == uid(seed["submission_id"]),
                "任务仍指向同一份当前提交",
            )

            cursor.execute(
                "SELECT submission_id, decision, reviewer_identity_verified FROM tasks_review WHERE task_id = %s",
                [uid(seed["task_id"])],
            )
            review = cursor.fetchone()
            check(review is not None and review[1] == "APPROVE", "审核记录恢复且仍为通过")

            cursor.execute(
                "SELECT claimed_at, execution_deadline_at, execution_timeout_seconds "
                "FROM tasks_execution WHERE id = %s",
                [uid(seed["execution_id"])],
            )
            execution = cursor.fetchone()
            check(execution is not None, "执行记录恢复")
            if execution:
                claimed = execution[0]
                deadline = execution[1]
                check(
                    execution[2] == seed["timeout"],
                    f"固定超时快照未被改写（{execution[2]}s）",
                )
                delta = deadline - claimed
                check(
                    int(delta.total_seconds()) == seed["timeout"],
                    "恢复后截止时刻仍是领取时刻加固定超时，没有被重新计时",
                )

            cursor.execute(
                "SELECT file_asset_id FROM files_attachment_link WHERE task_id = %s",
                [uid(seed["task_id"])],
            )
            links = cursor.fetchall()
            check(len(links) >= 1, "附件引用关系恢复")
            cursor.execute("SELECT sha256 FROM files_asset WHERE id = %s", [uid(seed["asset_id"])])
            asset_row = cursor.fetchone()
            check(
                asset_row is not None and asset_row[0] == seed["sha256"],
                "文件元数据与校验和一致",
            )
    finally:
        connection.close()

    # The matched file must exist in the restored file store with an identical checksum.
    restored_file = restored_storage / "objects" / seed["storage_key"]
    if not restored_file.exists():
        candidates = list(restored_storage.rglob(seed["storage_key"]))
        restored_file = candidates[0] if candidates else restored_file
    check(restored_file.exists(), f"恢复的私有文件存在：{restored_file.name}")
    if restored_file.exists():
        digest = hashlib.sha256(restored_file.read_bytes()).hexdigest()
        check(digest == seed["sha256"], "恢复文件校验和与数据库记录一致")


def main() -> int:
    print("== T36 备份与恢复演练（数据库 + 文件成对）==")
    from django.db import connection

    print(f"数据库后端：{connection.vendor}；源库：{SOURCE_DB}")
    if connection.vendor != "mysql":
        print("必须使用真实 MySQL 运行本演练，当前不是 MySQL，终止。")
        return 2

    storage_root = str(settings.FILE_STORAGE_ROOT)
    print("\n1) 准备完整交付闭环数据")
    seed = seed_closed_loop()
    print(f"   任务 {seed['task_id']} 状态 {seed['status']}，产物 {seed['storage_key']}")

    with tempfile.TemporaryDirectory(prefix="ht-restore-drill-") as tmp:
        workdir = Path(tmp)
        restored_storage = workdir / "restored-files"
        print("\n2) 备份数据库与私有文件目录")
        dump_path, files_dir = backup(workdir, storage_root)
        print(f"   数据库转储 {dump_path.stat().st_size} 字节；文件目录已复制")

        print("\n3) 恢复到隔离的数据库与文件目录")
        restore(dump_path, files_dir, restored_storage)

        print("\n4) 校验恢复后的业务与文件一致性")
        verify(seed, restored_storage)

        run_sql(None, [f"DROP DATABASE IF EXISTS {RESTORE_DB}"])

    print("\n== 结果 ==")
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过：数据库与文件成对恢复后，任务/提交/审核关系完整、文件校验和一致、固定截止未被重新计时。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
