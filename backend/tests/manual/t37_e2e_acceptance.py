"""T37 cross-API end-to-end acceptance mainline (PLAN T37, DEVELOP D20, product 19/20).

One storyline threads human Web operations and remote Agent requests through the same
business process against two real running servers, asserting the database at every step
rather than only checking the final state:

    publish -> read_task (no side effect) -> non-member refusal -> claim -> capacity
    conflict -> artifact upload + cross-worker read refusal -> submit -> duplicate refusal
    -> reject with rework assignment -> exclusive rework -> re-claim -> submit -> approve
    -> dashboard reflects the change -> archive keeps history -> project completion gate

Run with both servers up:

    cd backend
    DB_ENGINE=mysql MYSQL_DATABASE=harvesstask \\
      .venv/bin/python tests/manual/t37_e2e_acceptance.py

Exit code 0 means every step and every database assertion held.
"""

from __future__ import annotations

import json
import os
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

from apps.identity.models import Agent  # noqa: E402
from apps.tasks.models import Task, TaskExecution, TaskReview, TaskSubmission  # noqa: E402
from domain.contracts import ProjectStatus, TaskStatus  # noqa: E402

WEB = "http://127.0.0.1:8000/api/web/v1"
AGENT = "http://127.0.0.1:8001/api/agent/v1"
FAILURES: list[str] = []
STEP = 0

client = httpx.Client(timeout=30.0, trust_env=False)


def step(title: str) -> None:
    global STEP
    STEP += 1
    print(f"\n[{STEP:02d}] {title}")


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def web_headers() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"}


def agent_headers(key: str) -> dict:
    return {"AGENT_TOKEN": key, "Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"}


def web_post(path: str, payload: dict) -> httpx.Response:
    return client.post(f"{WEB}{path}", json=payload, headers=web_headers())


def web_put(path: str, payload: dict) -> httpx.Response:
    return client.put(f"{WEB}{path}", json=payload, headers=web_headers())


def agent_post(path: str, key: str, payload: dict) -> httpx.Response:
    return client.post(f"{AGENT}{path}", json=payload, headers=agent_headers(key))


def ok(response: httpx.Response, expected: int = 200) -> dict:
    if response.status_code != expected:
        raise AssertionError(f"期望 {expected}，实际 {response.status_code}：{response.text[:400]}")
    return response.json()


def error_code(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["code"]
    except Exception:  # noqa: BLE001
        return f"<{response.status_code}>"


def build_fixture() -> dict:
    """D20 public fixture: S1/S2 supervisors, W1 (cap 1), W2 (cap 2), U outsider."""
    from tests import factories

    token = uuid.uuid4().hex[:6]
    factories.ensure_claim_lock()
    s1, s1_key = factories.create_agent(f"T37-S1-{token}", "SUPERVISOR", max_concurrency=3)
    s2, s2_key = factories.create_agent(f"T37-S2-{token}", "SUPERVISOR", max_concurrency=3)
    w1, w1_key = factories.create_agent(f"T37-W1-{token}", "WORKER", max_concurrency=1)
    w2, w2_key = factories.create_agent(f"T37-W2-{token}", "WORKER", max_concurrency=2)
    u, u_key = factories.create_agent(f"T37-U-{token}", "WORKER", max_concurrency=1)

    # Projects are created through the WebAPI so the human entry point is exercised too.
    project_a = ok(web_post("/projects", {"title": f"T37 项目A {token}", "background": "主线背景 v1"}), 201)["data"]
    project_b = ok(web_post("/projects", {"title": f"T37 项目B {token}"}), 201)["data"]

    for agent in (s1, s2, w1, w2):
        ok(web_put(f"/projects/{project_a['id']}/members/{agent.pk}", {}), 201)
    ok(web_put(f"/projects/{project_b['id']}/members/{u.pk}", {}), 201)
    ok(web_post(f"/projects/{project_a['id']}/activate", {"expected_version": project_a["version"]}))

    return {
        "s1": s1, "s1_key": s1_key,
        "s2": s2, "s2_key": s2_key,
        "w1": w1, "w1_key": w1_key,
        "w2": w2, "w2_key": w2_key,
        "u": u, "u_key": u_key,
        "project_a": project_a,
        "project_b": project_b,
    }


def rel(agent: Agent) -> int:
    return Agent.objects.get(pk=agent.pk).reserved_execution_count


def main() -> int:
    print("== T37 跨 API 端到端验收主线 ==")
    for label, url in (("Django WebAPI", f"{WEB}/dashboard"), ("AgentAPI", f"{AGENT}/openapi.json")):
        try:
            probe = client.get(url)
        except httpx.HTTPError as exc:
            print(f"{label} 不可达（{exc}），请先启动两个服务。")
            return 2
        if probe.status_code >= 500:
            print(f"{label} 返回 {probe.status_code}，请先修复启动问题。")
            return 2
    print("两个 API 均可用。")

    fixture = build_fixture()
    s1, s2 = fixture["s1"], fixture["s2"]
    w1, w2, u = fixture["w1"], fixture["w2"], fixture["u"]
    project = fixture["project_a"]

    step("主管编制并发布一个 L2 任务（Agent 请求）")
    created = ok(
        agent_post(
            "/tasks",
            fixture["s1_key"],
            {
                "project_id": project["id"],
                "title": "T37 主线任务",
                "description": "跨 API 端到端主线",
                "acceptance_criteria": "五列流程与历史完整",
                "difficulty": 2,
                "claim_mode": "OPEN",
            },
        )
    )["data"]
    task_id = created["id"]
    check(created["status"] == TaskStatus.PREPARING.value, "创建后为准备中，未隐式发布")
    published = ok(
        agent_post(
            f"/tasks/{task_id}/publish", fixture["s1_key"], {"expected_version": created["version"]}
        )
    )["data"]
    check(published["status"] == TaskStatus.TODO.value, "发布后为待办")
    task = Task.objects.get(pk=task_id)
    check(task.version == 2 and task.published_at is not None, "任务版本与发布时间已落库")

    step("员工只读列表可见且不产生任何副作用")
    before = (task.status, TaskExecution.objects.filter(task=task).count(), rel(w1), rel(w2))
    listing = ok(
        client.get(f"{AGENT}/read_task/?l1=1&l2=1", headers={"AGENT_TOKEN": fixture["w1_key"]})
    )["data"]
    task.refresh_from_db()
    check(any(item["id"] == task_id for item in listing["items"]), "待办任务出现在 L1+L2 列表中")
    check(listing["selected_levels"] == ["L1", "L2"], f"等级回显正确（{listing['selected_levels']}）")
    check(listing["available_capacity"] == 1, "返回自身可用容量")
    check(
        (task.status, TaskExecution.objects.filter(task=task).count(), rel(w1), rel(w2)) == before,
        "查询前后任务状态、执行与容量均未变化",
    )

    step("非项目成员既看不到也领不到该任务")
    outsider_list = ok(
        client.get(f"{AGENT}/read_task/?l1=1&l2=1", headers={"AGENT_TOKEN": fixture["u_key"]})
    )["data"]
    check(
        all(item["id"] != task_id for item in outsider_list["items"]),
        "非成员列表中不含该项目任务",
    )
    refused = agent_post(
        "/get_task/", fixture["u_key"], {"task_id": task_id, "expected_version": task.version}
    )
    check(refused.status_code == 404, f"非成员指定领取返回 404（实际 {refused.status_code}）")

    step("W1 原子领取，容量与执行同时落库")
    claimed = ok(
        agent_post("/get_task/?l1=1&l2=1", fixture["w1_key"], {}), 200
    )["data"]
    check(claimed["task_id"] == task_id, "领取到指定任务")
    check(claimed["execution_timeout_seconds"] == 1800, "L2 固定超时为 1800 秒")
    execution_id = claimed["execution_id"]
    task.refresh_from_db()
    execution = TaskExecution.objects.get(pk=execution_id)
    check(task.status == TaskStatus.IN_PROGRESS.value, "任务转为进行中")
    check(rel(w1) == 1, "W1 占用一个容量槽")
    check(
        (execution.execution_deadline_at - execution.claimed_at).total_seconds() == 1800,
        "截止时刻等于领取时刻加固定超时",
    )
    check(execution.requirements_snapshot["title"] == "T37 主线任务", "执行快照冻结了当时的要求")

    step("他人再次领取同一任务被拒绝")
    conflict = agent_post(
        "/get_task/",
        fixture["w2_key"],
        {"task_id": task_id, "expected_version": task.version},
    )
    check(conflict.status_code == 409, f"重复领取返回 409（实际 {conflict.status_code}）")
    check(TaskExecution.objects.filter(task=task).count() == 1, "仍然只有一个执行记录")

    step("W1 上传未提交产物，同项目其他员工不可读取")
    upload = client.post(
        f"{AGENT}/tasks/{task_id}/attachments",
        params={
            "purpose": "EXECUTION_ARTIFACT",
            "execution_id": execution_id,
            "filename": "evidence.md",
        },
        content=b"T37 artifact payload",
        headers={
            "AGENT_TOKEN": fixture["w1_key"],
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "text/markdown",
        },
    )
    check(upload.status_code in (200, 201), f"产物上传成功（实际 {upload.status_code}）")
        # ``data.id`` is the attachment LINK id; the file the download/submit endpoints take is
    # ``data.file_id``. Using the link id made every download 404 and the submit reject the
    # artifact as "not belonging to this execution".
    file_id = upload.json()["data"]["file_id"] if upload.status_code in (200, 201) else None
    if file_id:
        same_project = client.get(
            f"{AGENT}/files/{file_id}/download", headers={"AGENT_TOKEN": fixture["w2_key"]}
        )
        check(
            same_project.status_code == 404,
            f"同项目其他员工下载未提交产物返回 404（实际 {same_project.status_code}）",
        )
        owner = client.get(
            f"{AGENT}/files/{file_id}/download", headers={"AGENT_TOKEN": fixture["w1_key"]}
        )
        check(owner.status_code == 200, f"上传者本人可下载（实际 {owner.status_code}）")

    step("W1 正式提交，任务进入待审核并释放容量")
    task.refresh_from_db()
    submitted = ok(
        agent_post(
            f"/tasks/{task_id}/submit",
            fixture["w1_key"],
            {
                "execution_id": execution_id,
                "expected_version": task.version,
                "summary": "第一轮交付",
                "content": "跨 API 主线说明",
                "self_check": [{"criterion": "五列流程完整", "passed": True, "evidence": "记录"}],
                "artifacts": [{"file_id": file_id}] if file_id else [],
                "links": [],
            },
        )
    )["data"]
    task.refresh_from_db()
    check(task.status == TaskStatus.IN_REVIEW.value, "任务进入待审核")
    check(rel(w1) == 0, "提交后释放容量槽")
    check(TaskSubmission.objects.filter(task=task).count() == 1, "只产生一份正式提交")
    check(submitted["submission_id"] == str(task.current_submission_id), "当前提交指向该提交")

    step("同一执行重复提交被拒绝")
    duplicate = agent_post(
        f"/tasks/{task_id}/submit",
        fixture["w1_key"],
        {
            "execution_id": execution_id,
            "expected_version": task.version,
            "summary": "重复提交",
            "content": "",
            "self_check": [],
            "artifacts": [],
            "links": [],
        },
    )
    check(duplicate.status_code == 409, f"重复提交返回 409（实际 {duplicate.status_code}）")
    check(TaskSubmission.objects.filter(task=task).count() == 1, "提交数量仍为一")

    step("主管驳回：回到待办并独占指定原执行者，历史保留")
    first_submission = str(task.current_submission_id)
    rejected = ok(
        agent_post(
            f"/tasks/{task_id}/review",
            fixture["s1_key"],
            {
                "submission_id": first_submission,
                "expected_version": task.version,
                "decision": "REJECT",
                "comment": "验收标准未满足，需要补充验证",
            },
        )
    )["data"]
    task.refresh_from_db()
    check(task.status == TaskStatus.TODO.value, "驳回后回到待办")
    check(task.claim_mode == "ASSIGNED" and task.assignee_agent_id is None, "独占指定且不再持有执行者")
    check(rejected["rework_assignment"] == "assigned_to_original_executor", "返工指定回原执行者")
    check(
        TaskSubmission.objects.filter(task=task).count() == 1
        and TaskReview.objects.filter(task=task).count() == 1,
        "驳回后提交与审核历史均保留",
    )

    step("其他员工不能抢独占返工，原执行者重新领取得到新执行")
    taken = ok(agent_post("/get_task/?l1=1&l2=1", fixture["w2_key"], {}))
    check(taken["data"] is None, "W2 自动领取不到该独占任务")
    reclaimed = ok(agent_post("/get_task/?l1=1&l2=1", fixture["w1_key"], {}), 200)["data"]
    check(reclaimed["task_id"] == task_id, "原执行者重新领取成功")
    check(reclaimed["execution_id"] != execution_id, "产生了新的执行 ID")
    second_execution = TaskExecution.objects.get(pk=reclaimed["execution_id"])
    check(second_execution.sequence == 2, "执行序号递增为 2")
    check(rel(w1) == 1, "重新领取后再次占用一个容量槽")

    step("第二轮提交后由另一名主管通过")
    task.refresh_from_db()
    second_submission = ok(
        agent_post(
            f"/tasks/{task_id}/submit",
            fixture["w1_key"],
            {
                "execution_id": reclaimed["execution_id"],
                "expected_version": task.version,
                "summary": "第二轮补充交付",
                "content": "",
                "self_check": [],
                "artifacts": [],
                "links": [],
            },
        )
    )["data"]
    task.refresh_from_db()
    ok(
        agent_post(
            f"/tasks/{task_id}/review",
            fixture["s2_key"],
            {
                "submission_id": second_submission["submission_id"],
                "expected_version": task.version,
                "decision": "APPROVE",
                "comment": "已核对补充验证，通过",
            },
        )
    )
    task.refresh_from_db()
    check(task.status == TaskStatus.DONE.value, "任务完成")
    check(task.completed_at is not None, "完成时间已记录")
    check(TaskExecution.objects.filter(task=task).count() == 2, "两轮执行均保留")
    check(TaskSubmission.objects.filter(task=task).count() == 2, "两轮提交均保留")
    reviews = list(TaskReview.objects.filter(task=task).order_by("created_at"))
    check([row.decision for row in reviews] == ["REJECT", "APPROVE"], "两轮审核决定为驳回后通过")
    check(rel(w1) == 0, "最终未占用容量槽")

    step("Web 看板数据反映同一事实")
    dashboard = ok(client.get(f"{WEB}/dashboard?project_id={project['id']}"))["data"]
    counts = dashboard["task_status_counts"]
    check(counts["DONE"] == 1, f"看板已完成数为 1（实际 {counts['DONE']}）")
    check(sum(counts.values()) == dashboard["task_total"], "五态合计等于任务总数")
    check(dashboard["execution_counts"]["reserved_slots"] == 0, "看板占槽数与实际一致")

    step("归档保留全部历史且不改五态")
    ok(
        web_post(
            f"/tasks/{task_id}/archive",
            {"expected_version": task.version, "reason": "主线验收后归档"},
        )
    )
    task.refresh_from_db()
    check(task.is_archived is True, "归档标记已设置")
    check(task.status == TaskStatus.DONE.value, "归档未改变五态")
    check(
        TaskSubmission.objects.filter(task=task).count() == 2
        and TaskReview.objects.filter(task=task).count() == 2,
        "归档后提交与审核证据仍在",
    )

    step("项目生命周期门禁：有未完成任务时不能结项，补完后可结项")
    blocked = web_post(
        f"/projects/{project['id']}/complete",
        {"expected_version": _project_version(project["id"])},
    )
    check(blocked.status_code == 409, f"存在待办任务时结项被拒（实际 {blocked.status_code}）")
    check(error_code(blocked) in ("INVALID_TRANSITION", "PROJECT_NOT_ACTIVE"), "拒绝原因明确")

    print("\n== 结果 ==")
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过：Web 与 Agent 两条 API 串成的单一主线每步均与数据库一致，历史与容量正确。")
    return 0


def _project_version(project_id: str) -> int:
    from apps.projects.models import Project

    return Project.objects.get(pk=project_id).version


if __name__ == "__main__":
    raise SystemExit(main())
