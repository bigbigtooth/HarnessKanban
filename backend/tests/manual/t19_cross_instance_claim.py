"""T19 cross-instance claim verification (PLAN T19, product A09/A29, DEVELOP D12/D20).

This is a real-process verification, not an in-process one: it starts TWO uvicorn AgentAPI
instances against the same MySQL write database and races them for one task. It also
verifies the lock-holder-exit case by holding the singleton lock row on a separate raw
connection and confirming that a waiting claim fails with a clear 503 instead of degrading
to lock-free execution.

Run it explicitly (it spawns servers and writes to the configured MySQL database):

    cd backend
    DB_ENGINE=mysql MYSQL_DATABASE=harvesstask \\
      .venv/bin/python tests/manual/t19_cross_instance_claim.py

Exit code 0 means every assertion held; any failure prints evidence and exits non-zero.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DB_ENGINE", "mysql")
os.environ.setdefault("MYSQL_DATABASE", "harvesstask")

import django  # noqa: E402

django.setup()

import httpx  # noqa: E402

from apps.identity.models import Agent  # noqa: E402
from apps.tasks.models import Task, TaskExecution  # noqa: E402
from django.db import connections  # noqa: E402
from domain.contracts import AgentRole, ProjectStatus  # noqa: E402
from tests import factories  # noqa: E402

PORTS = tuple(
    int(part) for part in os.environ.get("T19_PORTS", "19121,19122").split(",") if part.strip()
)
REQUEST_COUNT = 20
STARTUP_TIMEOUT = 40
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        FAILURES.append(message)


def assert_ports_free() -> None:
    """Refuse to run if a previous AgentAPI is still serving on our ports.

    Uses an HTTP probe rather than a raw TCP connect: a local interception layer can accept
    TCP connections on any port, which would make a socket-level check report a false
    positive and hide the real reason a run failed.
    """
    client = httpx.Client(timeout=2.0, trust_env=False)
    try:
        for port in PORTS:
            try:
                response = client.get(f"http://127.0.0.1:{port}/health/live")
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                raise RuntimeError(
                    f"端口 {port} 上仍有 AgentAPI 在运行，请先停止残留进程再运行本验收"
                )
    finally:
        client.close()


def start_instance(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    log_path = BACKEND_DIR / "var" / f"t19-agentapi-{port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "wb")  # noqa: SIM115 - kept open for the child's lifetime
    process = subprocess.Popen(
        [
            str(BACKEND_DIR / ".venv" / "bin" / "python"),
            "-m",
            "uvicorn",
            "agentapi.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    client = httpx.Client(timeout=2.0, trust_env=False)
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            response = client.get(f"http://127.0.0.1:{port}/health/live")
            if response.status_code == 200:
                client.close()
                return process
        except httpx.HTTPError:
            time.sleep(0.3)
    client.close()
    process.kill()
    output = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    raise RuntimeError(f"AgentAPI 实例 {port} 未在 {STARTUP_TIMEOUT}s 内就绪。服务日志：\n{output}")


def seed() -> dict:
    """Create isolated data for this run: four workers on one project with one TODO task."""
    factories.ensure_claim_lock()
    token = uuid.uuid4().hex[:8]
    project = factories.create_project(f"T19 项目 {token}", status=ProjectStatus.ACTIVE)
    agents = []
    for index in range(4):
        agent, key = factories.create_agent(
            f"T19-W{index}-{token}", AgentRole.WORKER, max_concurrency=1
        )
        factories.add_member(project, agent)
        agents.append((agent, key))
    task = factories.create_task(project, title=f"T19 竞争任务 {token}", difficulty=2)
    return {"project": project, "agents": agents, "task": task}


def race_one_task(seed_data: dict) -> tuple[list[int], list[str]]:
    """20 concurrent claims, alternating between the two real API instances."""
    task = seed_data["task"]
    agents = seed_data["agents"]
    barrier = threading.Barrier(REQUEST_COUNT)
    statuses: list[int] = []
    bodies: list[str] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        agent, key = agents[index % len(agents)]
        port = PORTS[index % len(PORTS)]
        payload = {"task_id": str(task.pk), "expected_version": task.version}
        headers = {"AGENT_TOKEN": key, "Idempotency-Key": str(uuid.uuid4())}
        try:
            barrier.wait(timeout=30)
            response = httpx.Client(trust_env=False, timeout=120.0).post(
                f"http://127.0.0.1:{port}/api/agent/v1/get_task/?l1=1&l2=1",
                json=payload,
                headers=headers,
                timeout=30.0,
            )
            with guard:
                statuses.append(response.status_code)
                bodies.append(response.text[:160])
        except Exception as exc:  # noqa: BLE001 - recorded as evidence
            with guard:
                statuses.append(-1)
                bodies.append(f"{type(exc).__name__}: {exc}")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(REQUEST_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    return statuses, bodies


def hold_claim_lock_with_raw_connection():
    """Take the singleton row lock on a separate connection and keep it open."""
    import pymysql

    connection = pymysql.connect(
        host="127.0.0.1", port=3306, user="root", password="", database="harvesstask", autocommit=False
    )
    cursor = connection.cursor()
    cursor.execute("UPDATE tasks_claim_lock SET revision = revision + 1 WHERE id = 1")
    if cursor.rowcount != 1:  # pragma: no cover - seeded by migration
        raise RuntimeError("无法取得全局领取锁行，数据库状态异常")
    return connection


def main() -> int:
    print("== T19 跨实例全局领取验收（真实进程 + 真实 MySQL）==")
    from django.db import connection as django_connection

    print(f"数据库后端：{django_connection.vendor}")
    if django_connection.vendor != "mysql":
        print("必须使用真实 MySQL 运行本验收，当前不是 MySQL，终止。")
        return 2

    seed_data = seed()
    task: Task = seed_data["task"]
    processes = []
    try:
        assert_ports_free()
        for port in PORTS:
            processes.append(start_instance(port))
        print(f"已启动 {len(processes)} 个 AgentAPI 实例：{PORTS}")

        print(f"\n[场景 1] {REQUEST_COUNT} 个并发请求跨两个实例争抢同一任务")
        statuses, bodies = race_one_task(seed_data)
        successes = statuses.count(200)
        conflicts = sum(1 for status in statuses if status == 409)
        others = [status for status in statuses if status not in (200, 409)]
        print(f"  状态分布：200={successes} 409={conflicts} 其他={others}")
        check(successes == 1, f"恰好一次领取成功（实际 {successes}）")
        check(conflicts == REQUEST_COUNT - 1, f"其余请求均为明确冲突（实际 {conflicts}）")
        check(not others, f"没有异常状态码（实际 {others}）")

        task.refresh_from_db()
        executions = TaskExecution.objects.filter(task=task)
        agent = Agent.objects.get(pk=task.assignee_agent_id)
        check(executions.count() == 1, f"只产生一个执行记录（实际 {executions.count()}）")
        check(task.status == "IN_PROGRESS", f"任务为进行中（实际 {task.status}）")
        check(agent.reserved_execution_count == 1, f"只占用一个容量槽（实际 {agent.reserved_execution_count}）")
        check(
            executions.first().execution_deadline_at > executions.first().claimed_at,
            "执行记录带固定截止时刻",
        )

        print("\n[场景 2] 持锁连接未提交时，其他实例的领取必须明确失败而不是无锁降级")
        holder = hold_claim_lock_with_raw_connection()
        try:
            second_task = factories.create_task(seed_data["project"], title="持锁等待任务", difficulty=1)
            # A dedicated agent: the race above may have consumed another agent's only slot.
            waiting_agent, waiting_key = factories.create_agent(
                f"T19-WAIT-{uuid.uuid4().hex[:8]}", AgentRole.WORKER, max_concurrency=1
            )
            factories.add_member(seed_data["project"], waiting_agent)
            started = time.time()
            response = httpx.Client(trust_env=False, timeout=120.0).post(
                f"http://127.0.0.1:{PORTS[0]}/api/agent/v1/get_task/?l1=1",
                json={"task_id": str(second_task.pk), "expected_version": second_task.version},
                headers={"AGENT_TOKEN": waiting_key, "Idempotency-Key": str(uuid.uuid4())},
                timeout=120.0,
            )
            elapsed = time.time() - started
            print(f"  等待 {elapsed:.1f}s 后返回 {response.status_code}：{response.text[:200]}")
            check(
                response.status_code == 503,
                f"等待锁超时返回 503（实际 {response.status_code}）",
            )
            if response.status_code == 503:
                code = response.json().get("error", {}).get("code")
                check(code in ("CLAIM_LOCK_TIMEOUT", "DATABASE_BUSY"), f"错误码可重试（实际 {code}）")
            check(elapsed < 30, f"等待被限制在配置的锁等待上限附近（实际 {elapsed:.1f}s）")
            second_task.refresh_from_db()
            check(second_task.status == "TODO", "持锁期间任务未被无锁领取")
        finally:
            holder.rollback()
            holder.close()

        print("\n[场景 3] 持锁连接退出后，另一实例必须可以正常领取")
        third_task = factories.create_task(seed_data["project"], title="恢复领取任务", difficulty=1)
        waiting_agent, waiting_key = factories.create_agent(
            f"T19-RECOVER-{uuid.uuid4().hex[:8]}", AgentRole.WORKER, max_concurrency=1
        )
        factories.add_member(seed_data["project"], waiting_agent)
        response = httpx.Client(trust_env=False, timeout=120.0).post(
            f"http://127.0.0.1:{PORTS[1]}/api/agent/v1/get_task/?l1=1",
            json={"task_id": str(third_task.pk), "expected_version": third_task.version},
            headers={"AGENT_TOKEN": waiting_key, "Idempotency-Key": str(uuid.uuid4())},
            timeout=30.0,
        )
        print(f"  返回 {response.status_code}：{response.text[:200]}")
        check(response.status_code == 200, f"锁释放后领取成功（实际 {response.status_code}）")
        third_task.refresh_from_db()
        check(third_task.status == "IN_PROGRESS", "任务已转为进行中")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
        connections.close_all()

    print("\n== 结果 ==")
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过：跨实例仅一次领取、容量与执行唯一、持锁退出可恢复且拒绝无锁降级。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
