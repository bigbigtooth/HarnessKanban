"""T39 用真实 pi Agent 跑通「查任务 → 领取 → 提交」全流程（非 mock）。

与其它验收脚本的区别：这里的执行者不是测试代码，而是一个真实运行的 Agent 进程
（``pi -p "<一段提示词>"``）。测试只负责：

1. 准备真实环境（真实 MySQL + 真实 Django WebAPI + 真实 FastAPI AgentAPI）；
2. 准备真实数据（项目、主管/员工 Agent、真实 Key、已发布的 L2 待办任务）；
3. 把一段提示词交给 ``pi``，由它自己用 curl 调 AgentAPI 完成「看有没有任务 → 领取一项 →
   产出交付物 → 正式提交」；
4. 回到数据库核对全流程事实（执行、产物、提交各恰好一条，且内容是 pi 真实产出的）。

提示词里的每个 ID 都由 pi 从真实响应里读取，测试不替它填任何业务字段；如果 pi 没跑完，
脚本会以非零退出并打印它的原始输出。

运行：

    cd backend
    DB_ENGINE=mysql MYSQL_DATABASE=harvesstask \\
      .venv/bin/python tests/manual/t39_pi_agent_flow.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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

from apps.files.models import AttachmentLink, FileAsset  # noqa: E402
from apps.tasks.models import Task, TaskExecution, TaskReview, TaskSubmission  # noqa: E402
from domain.contracts import ProjectStatus, TaskStatus  # noqa: E402

WEB = os.environ.get("T39_WEB_BASE", "http://127.0.0.1:8000/api/web/v1")
AGENT = os.environ.get("T39_AGENT_BASE", "http://127.0.0.1:8001/api/agent/v1")
WEB_ROOT = WEB.split("/api/", 1)[0]
AGENT_ROOT = AGENT.split("/api/", 1)[0]
WORKSPACE = Path("/tmp/ht-pi-agent-flow")
PI_TIMEOUT_SECONDS = 900

FAILURES: list[str] = []
client = httpx.Client(timeout=30.0, trust_env=False)


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def web(method: str, path: str, payload: dict | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return client.request(
        method,
        f"{WEB}{path}",
        content=None if payload is None else json.dumps(payload),
        headers=headers,
    )


def agent(method: str, path: str, token: str, payload: dict | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json", "AGENT_TOKEN": token}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return client.request(
        method,
        f"{AGENT}{path}",
        content=None if payload is None else json.dumps(payload),
        headers=headers,
    )


def prepare_environment() -> dict:
    """Real project, real agents with real keys, one published L2 task waiting to be claimed."""
    from tests import factories

    token = uuid.uuid4().hex[:6]
    factories.ensure_claim_lock()

    supervisor, supervisor_key = factories.create_agent(f"PI-S-{token}", "SUPERVISOR", max_concurrency=2)
    worker, worker_key = factories.create_agent(f"PI-W-{token}", "WORKER", max_concurrency=1)

    project_response = web("POST", "/projects", {"title": f"pi Agent 全流程 {token}", "background": "真实环境验收"})
    project = project_response.json()["data"]
    for agent_row in (supervisor, worker):
        web("PUT", f"/projects/{project['id']}/members/{agent_row.pk}", {})
    web("POST", f"/projects/{project['id']}/activate", {"expected_version": project["version"]})

    created = agent(
        "POST",
        "/tasks",
        supervisor_key,
        {
            "project_id": project["id"],
            "title": f"pi 领取的任务 {token}",
            "description": "写一份交付说明文件，说明该任务已完成",
            "acceptance_criteria": "存在交付文件，且提交摘要包含 PI-AGENT-DONE 标记",
            "difficulty": 2,
            "claim_mode": "OPEN",
        },
    ).json()["data"]
    agent(
        "POST",
        f"/tasks/{created['id']}/publish",
        supervisor_key,
        {"expected_version": created["version"]},
    )

    return {
        "project": project,
        "task_id": created["id"],
        "display_number": created["display_number"],
        "supervisor_key": supervisor_key,
        "worker_key": worker_key,
        "worker_name": worker.name,
    }


README_PATH = BACKEND_DIR.parent / "README.md"
PROMPT_START = "<!-- AGENT-PROMPT:START -->"
PROMPT_END = "<!-- AGENT-PROMPT:END -->"


def load_prompt() -> str:
    """Read the agent prompt from README.md.

    The documented prompt is the single source of truth: this acceptance reads it instead of
    keeping a private copy, so what the README tells other agents to do is exactly what gets
    executed and verified here.
    """
    if not README_PATH.exists():
        raise SystemExit(f"缺少 {README_PATH}，无法读取 Agent 提示词")
    document = README_PATH.read_text(encoding="utf-8")
    if PROMPT_START not in document or PROMPT_END not in document:
        raise SystemExit("README.md 缺少 AGENT-PROMPT 标记，提示词无法加载")
    block = document.split(PROMPT_START, 1)[1].split(PROMPT_END, 1)[0].strip()
    lines = block.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    prompt = "\n".join(lines).strip()
    if "get_task" not in prompt or "submit" not in prompt:
        raise SystemExit("README 中的提示词缺少领取或提交步骤，拒绝执行")
    return prompt + "\n"


def run_pi(fixture: dict) -> subprocess.CompletedProcess:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)

    environment = dict(os.environ)
    environment["AGENT_TOKEN"] = fixture["worker_key"]
    environment["BASE"] = AGENT

    return subprocess.run(
        [
            "pi",
            "-p",
            load_prompt(),
            "--system-prompt",
            "你是执行任务的工作 Agent。只用 bash 工具与 curl，不要修改正在运行的服务代码。",
        ],
        cwd=str(WORKSPACE),
        env=environment,
        capture_output=True,
        text=True,
        timeout=PI_TIMEOUT_SECONDS,
    )


def main() -> int:
    print("== T39 真实 pi Agent 全流程验收（查任务 → 领取 → 提交）==")
    if shutil.which("pi") is None:
        print("未找到 pi 命令，无法执行本验收。")
        return 2

    for label, url in (("Django WebAPI", f"{WEB_ROOT}/health/live"), ("AgentAPI", f"{AGENT_ROOT}/health/live")):
        try:
            if client.get(url).status_code != 200:
                print(f"{label} 未就绪，请先启动两个服务。")
                return 2
        except httpx.HTTPError:
            print(f"{label} 不可达，请先启动两个服务。")
            return 2

    fixture = prepare_environment()
    print(f"环境已就绪：项目 {fixture['project']['title']}，任务 {fixture['display_number']}")

    print("\n[1] 把提示词交给真实 pi Agent 执行（非 mock）")
    try:
        completed = run_pi(fixture)
    except subprocess.TimeoutExpired:
        print(f"  [FAIL] pi 在 {PI_TIMEOUT_SECONDS}s 内未结束")
        return 1
    output = completed.stdout or ""
    print("  --- pi 输出末尾 ---")
    for line in output.strip().splitlines()[-12:]:
        print(f"  {line}")

    result: dict = {}
    for line in output.splitlines():
        if line.strip().startswith("PI_RESULT:"):
            try:
                result = json.loads(line.split("PI_RESULT:", 1)[1].strip())
            except json.JSONDecodeError:
                pass
    check(result.get("status") == "submitted", f"pi 自报状态为 submitted（实际 {result.get('status')}）")
    check(result.get("task_id") == fixture["task_id"], "pi 领取的正是准备的那项任务")

    print("\n[2] 回到数据库核对全流程事实")
    task = Task.objects.get(pk=fixture["task_id"])
    executions = TaskExecution.objects.filter(task=task)
    submissions = TaskSubmission.objects.filter(task=task)
    check(executions.count() == 1, f"真实执行恰好一条（实际 {executions.count()}）")
    check(submissions.count() == 1, f"真实提交恰好一条（实际 {submissions.count()}）")
    check(task.status == TaskStatus.IN_REVIEW.value, f"任务已进入待审核（实际 {task.status}）")

    execution = executions.first()
    submission = submissions.first()
    if execution and submission:
        check(execution.ended_at is not None and execution.end_reason == "SUBMIT", "执行以 SUBMIT 结束")
        check(submission.execution_id == execution.pk, "提交绑定在本次执行上")
        check("PI-AGENT-DONE" in submission.summary, f"提交摘要来自 pi 的真实产出（{submission.summary}）")
        check(bool(submission.self_check), "提交带自检项")
        check(submission.artifact_refs, "提交引用了产物")

        deliverable = WORKSPACE / "deliverable.md"
        check(deliverable.exists(), "pi 在工作目录真实创建了交付文件")
        if deliverable.exists():
            content = deliverable.read_text(encoding="utf-8", errors="replace")
            check(fixture["display_number"] in content, "交付文件内容包含被领取任务的编号")
            # README 允许一次执行上传多个产物（上限 10），因此这里只要求「至少一个、
            # 且交付文件本身确实在库中、全部冻结绑定」；磁盘摘要比对锚定 deliverable.md。
            assets = list(FileAsset.objects.filter(pk__in=submission.artifact_refs))
            check(len(assets) >= 1, f"产物在库中至少一条（实际 {len(assets)}）")
            check(len(assets) <= 10, "产物数量不超过服务端上限 10")
            if assets:
                import hashlib

                digest = hashlib.sha256(deliverable.read_bytes()).hexdigest()
                matching = [asset for asset in assets if asset.sha256 == digest]
                check(bool(matching), "库中存在与磁盘交付文件摘要一致的产物")
                check(
                    all(
                        AttachmentLink.objects.filter(
                            file_asset=asset, submission=submission
                        ).exists()
                        for asset in assets
                    ),
                    "全部产物已冻结绑定到该提交",
                )
                check(
                    all(asset.bound_at is not None for asset in assets),
                    "全部产物已标记为已绑定，不会被临时文件清理",
                )
                # README 第 4 步强制格式：有产物时必须把 OSS 下载地址写进提交结果文本。
                oss_assets = [asset for asset in assets if asset.oss_uploaded_at is not None]
                if oss_assets:
                    check(
                        all(asset.storage_key in submission.content for asset in oss_assets),
                        "提交正文包含全部产物的 OSS 链接（README 第 4 步强制格式）",
                    )
                else:
                    print("  [SKIP] 服务端未启用 OSS，跳过「提交正文含 OSS 链接」断言")

    print("\n[3] 主管在真实 AgentAPI 上审核通过，确认闭环可达 DONE")
    approval = agent(
        "POST",
        f"/tasks/{fixture['task_id']}/review",
        fixture["supervisor_key"],
        {
            "submission_id": str(task.current_submission_id),
            "expected_version": task.version,
            "decision": "APPROVE",
            "comment": "pi 交付已核对",
        },
    )
    check(approval.status_code == 200, f"审核通过（实际 {approval.status_code}）")
    task.refresh_from_db()
    check(task.status == TaskStatus.DONE.value, f"任务已完成（实际 {task.status}）")
    check(TaskReview.objects.filter(task=task, decision="APPROVE").count() == 1, "审核记录唯一")

    print("\n== 结果 ==")
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        print("pi 完整输出：")
        print(output)
        return 1
    print("全部通过：真实 pi Agent 在真实 MySQL + 两套真实 API 上完成了「查任务 → 领取 → 产出 → 提交」，")
    print("数据库中的执行、产物与提交各恰好一条，产物摘要与磁盘文件一致，主管审核后闭环到 DONE。")
    return 0


def print_prompt() -> int:
    """Emit the README prompt verbatim so callers never re-derive it with fragile text tools."""
    print(load_prompt(), end="")
    return 0


if __name__ == "__main__":
    if "--print-prompt" in sys.argv:
        raise SystemExit(print_prompt())
    raise SystemExit(main())
