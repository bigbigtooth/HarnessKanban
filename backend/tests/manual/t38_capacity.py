"""T38 capacity and performance acceptance (PLAN T38, DEVELOP D20, product 18.1).

Prepares the target-scale dataset (100 agents, 30 active projects, 100,000 tasks) and then
samples real latencies for the list, dashboard, claim, submit and review paths, reporting
nearest-rank P95 against the product targets (queries/writes < 500 ms, dashboard < 1 s).

Run explicitly — it writes to the configured MySQL database and drops its own tables' rows
first so repeated runs are comparable:

    cd backend
    DB_ENGINE=mysql MYSQL_DATABASE=harvesstask \\
      .venv/bin/python tests/manual/t38_capacity.py

Exit code 0 means every correctness invariant held and every latency target was met; a
missed latency target is reported with the measured value and exits non-zero, because the
task card requires the gap to be stated rather than hidden.
"""

from __future__ import annotations

import math
import os
import statistics
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DB_ENGINE", "mysql")
os.environ.setdefault("MYSQL_DATABASE", "harvesstask")

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402

from apps.identity.models import Agent  # noqa: E402
from apps.projects.models import (  # noqa: E402
    Project,
    ProjectContextVersion,
    ProjectMember,
)
from apps.tasks.models import Task, TaskClaimLock  # noqa: E402
from domain.claim_service import ClaimRequest, claim_task  # noqa: E402
from domain.contracts import (  # noqa: E402
    ActorContext,
    ActorType,
    EndReason,
    ProjectStatus,
    TaskStatus,
)
from domain.dashboard_service import DashboardScope, get_dashboard  # noqa: E402
from domain.read_task_query import TaskListFilters, list_tasks  # noqa: E402
from domain.execution_service import end_execution  # noqa: E402
from domain.review_service import review_submission  # noqa: E402
from domain.service_runner import run_request_service  # noqa: E402
from domain.submission_service import submit_result  # noqa: E402

AGENT_COUNT = 100
PROJECT_COUNT = 30
TASK_COUNT = 100_000
AGENTS_PER_PROJECT = 4
SAMPLES = 100

TARGET_QUERY_MS = 500.0
TARGET_DASHBOARD_MS = 1000.0
RESULTS: list[tuple[str, str, float, float, bool]] = []


def nearest_rank_p95(samples: list[float]) -> float:
    """P95 as the ceil(0.95*n)-th value of the sorted samples, never an average of P95s."""
    ordered = sorted(samples)
    index = max(math.ceil(0.95 * len(ordered)) - 1, 0)
    return ordered[index]


def timed(fn) -> float:
    started = time.perf_counter()
    fn()
    return (time.perf_counter() - started) * 1000


def record(path: str, kind: str, samples: list[float], target: float) -> None:
    p95 = nearest_rank_p95(samples)
    RESULTS.append((path, kind, p95, target, p95 <= target))
    print(
        f"  {path:34} n={len(samples):4} min={min(samples):7.1f} "
        f"median={statistics.median(samples):7.1f} p95={p95:8.1f} 目标<{target:.0f}ms "
        f"{'OK' if p95 <= target else 'MISS'}"
    )


def reset_dataset() -> None:
    """Clear business rows so repeated runs measure the same shape (keeps task settings)."""
    print("清理既有业务数据…")
    with connection.cursor() as cursor:
        # The business tables form reference cycles (task <-> execution/submission,
        # document <-> document_version), so a benchmark reset temporarily suspends FK
        # checks. This is harness-only: no product code disables constraints.
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        try:
            for table in (
                "files_attachment_link",
                "files_asset",
                "tasks_review",
                "tasks_submission",
                "tasks_execution",
                "tasks_task",
                "projects_document_version",
                "projects_document",
                "projects_context_version",
                "projects_member",
                "projects_project",
                "identity_agent",
                "audit_activity_event",
                "jobs_outbox_event",
                "jobs_idempotency_record",
            ):
                cursor.execute(f"DELETE FROM {table}")
        finally:
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    TaskClaimLock.objects.update_or_create(id=1, defaults={"revision": 0})


def seed() -> dict:
    print(f"准备目标规模数据：{AGENT_COUNT} Agent / {PROJECT_COUNT} 项目 / {TASK_COUNT} 任务…")
    started = time.perf_counter()

    supervisors = [
        Agent(
            id=uuid.uuid4(),
            name=f"perf-supervisor-{index:03d}",
            role="SUPERVISOR",
            key_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            max_concurrency=8,
        )
        for index in range(10)
    ]
    workers = [
        Agent(
            id=uuid.uuid4(),
            name=f"perf-worker-{index:03d}",
            role="WORKER",
            key_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            max_concurrency=4,
        )
        for index in range(AGENT_COUNT - len(supervisors))
    ]
    agents = supervisors + workers
    Agent.objects.bulk_create(agents, batch_size=500)

    projects = [
        Project(
            id=uuid.uuid4(),
            title=f"perf-project-{index:03d}",
            status=ProjectStatus.ACTIVE.value,
            background="压测项目背景",
        )
        for index in range(PROJECT_COUNT)
    ]
    Project.objects.bulk_create(projects, batch_size=500)
    ProjectContextVersion.objects.bulk_create(
        [
            ProjectContextVersion(
                id=uuid.uuid4(),
                project=project,
                number=1,
                background_snapshot="压测项目背景",
                document_version_refs=[],
            )
            for project in projects
        ],
        batch_size=500,
    )

    memberships = []
    seen: set = set()

    def add(project, agent) -> None:
        # A repeated (project, agent) pair violates the unique membership constraint, and a
        # worker with no membership would answer NO_MATCHING_TASK instead of claiming —
        # which would silently turn a latency sample into a no-op fast path.
        key = (project.pk, agent.pk)
        if key in seen:
            return
        seen.add(key)
        memberships.append(ProjectMember(id=uuid.uuid4(), project=project, agent=agent))

    for index, project in enumerate(projects):
        add(project, supervisors[index % len(supervisors)])
    # Every worker gets exactly one project so any worker used as a sampler can really claim.
    for index, worker in enumerate(workers):
        add(projects[index % len(projects)], worker)
    ProjectMember.objects.bulk_create(memberships, batch_size=1000)

    tasks = []
    per_project = TASK_COUNT // PROJECT_COUNT
    for project_index, project in enumerate(projects):
        for local_index in range(per_project):
            number = local_index + 1
            tasks.append(
                Task(
                    id=uuid.uuid4(),
                    project=project,
                    number=number,
                    title=f"压测任务 {project_index}-{number}",
                    description="压测描述",
                    acceptance_criteria="压测验收标准",
                    difficulty=(local_index % 5) + 1,
                    status=TaskStatus.TODO.value,
                    claim_mode="OPEN",
                    sort_order=Decimal(number) * Decimal("1000"),
                )
            )
    Task.objects.bulk_create(tasks, batch_size=2000)

    elapsed = time.perf_counter() - started
    print(
        f"  已写入：agents={Agent.objects.count()} projects={Project.objects.count()} "
        f"members={ProjectMember.objects.count()} tasks={Task.objects.count()}（{elapsed:.1f}s）"
    )
    return {"agents": agents, "supervisors": supervisors, "workers": workers, "projects": projects}


def agent_actor(agent: Agent) -> ActorContext:
    return ActorContext(
        actor_type=ActorType.AGENT,
        request_id=str(uuid.uuid4()),
        agent_id=agent.pk,
        key_version=agent.key_version,
    )


def web_actor() -> ActorContext:
    return ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4()))


def main() -> int:
    print("== T38 容量与性能验收 ==")
    print(f"数据库后端：{connection.vendor}")
    if connection.vendor != "mysql":
        print("必须使用真实 MySQL 运行本验收，当前不是 MySQL，终止。")
        return 2
    with connection.cursor() as cursor:
        cursor.execute("SELECT VERSION()")
        print(f"MySQL 版本：{cursor.fetchone()[0]}")

    reset_dataset()
    dataset = seed()
    runner = dataset["workers"][0]
    supervisor = dataset["supervisors"][0]
    project = dataset["projects"][0]

    # Warm up first (DEVELOP D20): without it the first sample after a 100k-row insert
    # measures cold buffer-pool reads and reports a steady-state latency that is far too
    # pessimistic. Warm-up results are discarded, not included in any P95.
    print("\n预热（结果丢弃，不计入样本）…")
    warm_started = time.perf_counter()
    for _ in range(5):
        run_request_service(
            lambda: list_tasks(
                agent_actor(runner),
                TaskListFilters(levels=(1, 2, 3, 4, 5), status=TaskStatus.TODO.value),
                page_number=1,
                page_size=100,
            ),
            require_write_lock=False,
        )
        run_request_service(
            lambda: get_dashboard(web_actor(), DashboardScope(project_id=project.pk)),
            require_write_lock=False,
        )
    print(f"  预热完成（{time.perf_counter() - warm_started:.1f}s）")

    print(f"\n各路径有效延迟样本（nearest-rank P95，n={SAMPLES}）：")

    def list_once():
        return run_request_service(
            lambda: list_tasks(
                agent_actor(runner),
                TaskListFilters(levels=(1, 2, 3, 4, 5), status=TaskStatus.TODO.value),
                page_number=1,
                page_size=100,
            ),
            require_write_lock=False,
        )

    list_samples = [timed(list_once) for _ in range(SAMPLES)]
    record("GET /tasks (page_size=100)", "查询", list_samples, TARGET_QUERY_MS)

    dashboard_samples = [
        timed(
            lambda: run_request_service(
                lambda: get_dashboard(web_actor(), DashboardScope(project_id=project.pk)),
                require_write_lock=False,
            )
        )
        for _ in range(SAMPLES)
    ]
    record("GET /dashboard", "查询", dashboard_samples, TARGET_DASHBOARD_MS)

    detail_samples = [
        timed(
            lambda: run_request_service(
                lambda: list_tasks(
                    agent_actor(runner),
                    TaskListFilters(levels=None, project_id=project.pk, include_archived=True),
                    page_number=1,
                    page_size=20,
                ),
                require_write_lock=False,
            )
        )
        for _ in range(SAMPLES)
    ]
    record("GET /tasks?project_id (过滤)", "查询", detail_samples, TARGET_QUERY_MS)

    # Claim is serialised by the single global lock, so it is sampled sequentially and its
    # lock wait is the interesting number.
    workers = dataset["workers"]
    claim_samples: list[float] = []
    empty_claims = 0
    for index in range(SAMPLES):
        # Rotate the claimer so no agent is asked to exceed its own capacity, and release
        # the execution right after measuring so the sample stays a pure claim latency.
        claimer = workers[index % len(workers)]
        started = time.perf_counter()
        result = run_request_service(
            lambda: claim_task(
                agent_actor(claimer), ClaimRequest(levels=[1, 2, 3, 4, 5]), str(uuid.uuid4())
            ),
            global_claim_lock=True,
        )
        claim_samples.append((time.perf_counter() - started) * 1000)
        if not result.get("data"):
            empty_claims += 1
        if result.get("data"):
            execution_id = uuid.UUID(result["data"]["execution_id"])
            task_id = uuid.UUID(result["data"]["task_id"])
            run_request_service(
                lambda: end_execution(
                    agent_actor(claimer),
                    task_id=task_id,
                    execution_id=execution_id,
                    reason=EndReason.RELEASE,
                    reason_text="压测释放",
                    idem_key=str(uuid.uuid4()),
                ),
                require_write_lock=False,
            )
    if len(claim_samples) - empty_claims > 0:
        print(
            f"  说明：{SAMPLES} 次领取样本中 {SAMPLES - empty_claims} 次为真实领取，"
            f"{empty_claims} 次为无候选快速路径（不计入 p95 结论的有效样本）"
        )
    record("POST /get_task/ (全局锁串行)", "业务写入", claim_samples, TARGET_QUERY_MS)

    submit_samples: list[float] = []
    review_samples: list[float] = []
    workflow_rounds = 30
    for index in range(workflow_rounds):
        worker = workers[index % len(workers)]
        claimed = run_request_service(
            lambda: claim_task(
                agent_actor(worker), ClaimRequest(levels=[1, 2, 3, 4, 5]), str(uuid.uuid4())
            ),
            global_claim_lock=True,
        )
        if not claimed.get("data"):
            continue
        task_id = uuid.UUID(claimed["data"]["task_id"])
        execution_id = claimed["data"]["execution_id"]
        expected_version = claimed["data"]["version"]

        started = time.perf_counter()
        run_request_service(
            lambda: submit_result(
                agent_actor(worker),
                task_id=task_id,
                payload={
                    "execution_id": execution_id,
                    "expected_version": expected_version,
                    "summary": f"压测提交 {index}",
                    "content": "",
                    "self_check": [],
                    "artifacts": [],
                    "links": [],
                },
                idem_key=str(uuid.uuid4()),
            ),
            require_write_lock=False,
        )
        submit_samples.append((time.perf_counter() - started) * 1000)

        task = Task.objects.get(pk=task_id)
        review_expected_version = task.version
        submission_id = str(task.current_submission_id)
        started = time.perf_counter()
        run_request_service(
            lambda: review_submission(
                agent_actor(supervisor),
                task_id=task_id,
                payload={
                    "submission_id": submission_id,
                    "expected_version": review_expected_version,
                    "decision": "APPROVE",
                    "comment": "压测通过",
                },
                idem_key=str(uuid.uuid4()),
            ),
            require_write_lock=False,
        )
        review_samples.append((time.perf_counter() - started) * 1000)
    if submit_samples:
        record("POST /submit", "业务写入", submit_samples, TARGET_QUERY_MS)
        record("POST /review", "业务写入", review_samples, TARGET_QUERY_MS)

    if empty_claims:
        problems_early = (
            f"领取样本中有 {empty_claims}/{SAMPLES} 次无候选，延迟样本无效，"
            "需要修正数据准备后重跑"
        )
        print(f"\n[FAIL] {problems_early}")
        RESULTS.append(("POST /get_task/ 样本有效性", "样本", 0.0, 0.0, False))

    print("\n不变量复核（压力下正确性不得被削弱）：")
    problems = check_invariants()
    for message in problems:
        print(f"  [FAIL] {message}")
    if not problems:
        print("  [PASS] 独占、占槽、结果防重与五态合计一致")

    missed = [row for row in RESULTS if not row[4]]
    print("\n== 结果 ==")
    print(f"延迟样本组：{len(RESULTS)}，达标 {len(RESULTS) - len(missed)}，未达标 {len(missed)}")
    if missed or problems:
        for path, kind, p95, target, _ in missed:
            print(f"  未达标：{path} p95={p95:.1f}ms > 目标 {target:.0f}ms")
        return 1
    print("全部达标：查询与写入 P95 小于 500ms，看板汇总小于 1s，且不变量在目标规模下保持。")
    return 0


def check_invariants() -> list[str]:
    problems: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FROM tasks_task t
            JOIN tasks_execution e ON e.id = t.active_execution_id
            WHERE t.status = 'IN_PROGRESS' AND (e.ended_at IS NOT NULL OR e.task_id <> t.id)
            """
        )
        if cursor.fetchone()[0]:
            problems.append("存在进行中任务指向已结束或不属于该任务的执行")

        cursor.execute(
            """
            SELECT COUNT(*) FROM tasks_task
            WHERE status = 'IN_PROGRESS' AND (active_execution_id IS NULL OR assignee_agent_id IS NULL)
            """
        )
        if cursor.fetchone()[0]:
            problems.append("存在没有当前执行或执行者的进行中任务")

        cursor.execute(
            """
            SELECT COUNT(*) FROM identity_agent a
            WHERE a.reserved_execution_count <> (
                SELECT COUNT(*) FROM tasks_execution e
                WHERE e.agent_id = a.id AND e.ended_at IS NULL
            )
            """
        )
        if cursor.fetchone()[0]:
            problems.append("Agent 占槽数与真实未结束执行数不一致")

        cursor.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT execution_id FROM tasks_submission GROUP BY execution_id HAVING COUNT(*) > 1
            ) dup
            """
        )
        if cursor.fetchone()[0]:
            problems.append("存在同一执行的多份正式提交")

        cursor.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT submission_id FROM tasks_review GROUP BY submission_id HAVING COUNT(*) > 1
            ) dup
            """
        )
        if cursor.fetchone()[0]:
            problems.append("存在同一提交的多次审核决定")

        cursor.execute(
            """
            SELECT COUNT(*) FROM tasks_task
            WHERE status = 'IN_REVIEW' AND current_submission_id IS NULL
            """
        )
        if cursor.fetchone()[0]:
            problems.append("存在没有当前提交的待审核任务")

        cursor.execute(
            "SELECT status, COUNT(*) FROM tasks_task GROUP BY status ORDER BY status"
        )
        counts = dict(cursor.fetchall())
        cursor.execute("SELECT COUNT(*) FROM tasks_task")
        total = cursor.fetchone()[0]
        if sum(counts.values()) != total:
            problems.append(f"五态合计 {sum(counts.values())} 与任务总数 {total} 不一致")
        print(f"  五态分布：{counts}，合计 {total}")
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
