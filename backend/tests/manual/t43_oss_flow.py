"""T43 real-OSS end-to-end acceptance (upload -> prompt -> claim -> artifact -> submit).

Everything runs against the two real servers started by ``scripts/start.sh`` and the
real Aliyun OSS bucket configured in ``backend/.env`` — no mocks, no test doubles:

    Web 暂存上传 POST /files -> 匿名下载 oss_url 校验字节一致
    -> 创建任务 attachment_file_ids（OSS URL 写入 description 提示词）
    -> 发布 -> Agent read_task/get_task 领取 -> requirements_snapshot.description 携带 URL
    -> Agent 上传产物 -> data.oss_url 可匿名下载
    -> 提交 content 文本包含产物 OSS URL -> 主管审核通过闭环

Run with the services up:

    cd backend
    .venv/bin/python tests/manual/t43_oss_flow.py

Exit code 0 means every step and every database/OSS assertion held.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import httpx  # noqa: E402

from apps.files.models import AttachmentLink, FileAsset  # noqa: E402
from apps.tasks.models import Task  # noqa: E402
from domain import oss_storage  # noqa: E402
from domain.errors import DomainError  # noqa: E402

WEB = os.environ.get("T43_WEB_BASE", "http://127.0.0.1:7101/api/web/v1")
AGENT = os.environ.get("T43_AGENT_BASE", "http://127.0.0.1:7102/api/agent/v1")
FAILURES: list[str] = []
STEP = 0

client = httpx.Client(timeout=60.0, trust_env=False)


def step(title: str) -> None:
    global STEP
    STEP += 1
    print(f"\n[{STEP:02d}] {title}")


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def web_headers() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def agent_headers(key: str) -> dict:
    return {"AGENT_TOKEN": key, "Idempotency-Key": str(uuid.uuid4())}


def web_json(path: str, payload: dict, expected: int = 200) -> dict:
    response = client.post(f"{WEB}{path}", json=payload, headers=web_headers())
    if response.status_code != expected:
        raise AssertionError(f"期望 {expected}，实际 {response.status_code}：{response.text[:400]}")
    return response.json()["data"]


def agent_json(path: str, key: str, payload: dict, expected: int = 200) -> dict:
    response = client.post(f"{AGENT}{path}", json=payload, headers=agent_headers(key))
    if response.status_code != expected:
        raise AssertionError(f"期望 {expected}，实际 {response.status_code}：{response.text[:400]}")
    return response.json()["data"]


def build_fixture() -> dict:
    from tests import factories

    token = uuid.uuid4().hex[:6]
    factories.ensure_claim_lock()
    s2, s2_key = factories.create_agent(f"T43-S2-{token}", "SUPERVISOR", max_concurrency=3)
    w1, w1_key = factories.create_agent(f"T43-W1-{token}", "WORKER", max_concurrency=1)

    project = web_json(
        "/projects", {"title": f"T43 OSS 项目 {token}", "background": "OSS 主线"}, 201
    )
    for agent in (s2, w1):
        response = client.put(
            f"{WEB}/projects/{project['id']}/members/{agent.pk}",
            json={},
            headers=web_headers(),
        )
        assert response.status_code == 201, response.text[:300]
    web_json(f"/projects/{project['id']}/activate", {"expected_version": project["version"]})
    return {"s2": s2, "s2_key": s2_key, "w1": w1, "w1_key": w1_key, "project": project}


def main() -> int:
    if not oss_storage.is_enabled():
        print("FAIL: OSS 未启用（backend/.env 需 OSS_ENABLED=true），本脚本验证真实 OSS 链路")
        return 1

    for label, url in (("WebAPI", f"{WEB}/dashboard"), ("AgentAPI", f"{AGENT}/openapi.json")):
        probe = client.get(url)
        if probe.status_code != 200:
            print(f"FAIL: {label} 未就绪（{url} -> {probe.status_code}）。先执行 ./scripts/start.sh")
            return 1

    step("准备：主管/员工/项目（走真实 WebAPI）")
    fixture = build_fixture()
    project = fixture["project"]
    check(True, f"项目 {project['title']} 已激活")

    step("Web 暂存上传 POST /files（multipart）→ 返回 file_id 与 oss_url")
    spec_content = f"# T43 规格文档 {uuid.uuid4().hex}\n\n这是写入任务提示词的参考附件。\n".encode()
    upload = client.post(
        f"{WEB}/files",
        files={"file": ("t43-规格.md", spec_content, "text/markdown")},
        data={"project_id": project["id"]},
        headers=web_headers(),
    )
    if upload.status_code != 201:
        raise AssertionError(f"暂存上传失败 {upload.status_code}：{upload.text[:400]}")
    staged = upload.json()["data"]
    file_id = staged["file_id"]
    oss_url = staged["oss_url"]
    check(bool(oss_url and oss_url.startswith("http")), f"oss_url 已返回：{str(oss_url)[:80]}…")
    asset = FileAsset.objects.get(pk=file_id)
    check(asset.oss_uploaded_at is not None, "FileAsset.oss_uploaded_at 已落库")
    check(asset.bound_at is None, "暂存文件未绑定")

    step("匿名下载 oss_url，字节与上传内容一致（真实 OSS）")
    fetched = client.get(oss_url)
    check(fetched.status_code == 200, f"匿名 GET oss_url 成功（{fetched.status_code}）")
    check(fetched.content == spec_content, "下载字节与上传内容一致")

    step("创建任务携带 attachment_file_ids → OSS URL 写入 description 提示词")
    task = web_json(
        "/tasks",
        {
            "project_id": project["id"],
            "title": "T43 OSS 提示词任务",
            "description": "请阅读参考附件后完成工作。",
            "acceptance_criteria": "提交说明包含产物下载地址",
            "difficulty": 2,
            "attachment_file_ids": [file_id],
        },
    )
    check("## 参考附件（OSS 下载链接，可直接下载）" in task["description"], "提示词含参考附件小节")
    # oss_url 按次现签（Expires/Signature 每次不同），比对去掉 query 串的对象地址。
    check(
        oss_url.split("?")[0] in task["description"],
        "提示词包含暂存文件的 OSS 对象地址",
    )
    asset.refresh_from_db()
    check(asset.bound_at is not None, "暂存文件已绑定为 TASK_REFERENCE")
    check(
        AttachmentLink.objects.filter(
            task_id=task["id"], file_asset=asset, purpose="TASK_REFERENCE"
        ).exists(),
        "TASK_REFERENCE 链接已创建",
    )

    step("发布任务，Agent 领取 → requirements_snapshot.description 携带 oss_url")
    web_json(f"/tasks/{task['id']}/publish", {"expected_version": task["version"]})
    read = client.get(
        f"{AGENT}/read_task/?l1=1&l2=1", headers={"AGENT_TOKEN": fixture["w1_key"]}
    ).json()["data"]
    check(read["total"] >= 1, "read_task 可见任务")
    claimed = agent_json("/get_task/?l1=1&l2=1", fixture["w1_key"], {})
    check(claimed["task_id"] == task["id"], "领取到目标任务")
    execution_id = claimed["execution_id"]
    snapshot = claimed["requirements_snapshot"]
    check(
        oss_url.split("?")[0] in snapshot["description"],
        "执行快照的提示词包含 OSS 对象地址",
    )

    execution = Task.objects.get(pk=task["id"]).active_execution
    check(execution is not None and str(execution.pk) == execution_id, "当前执行已落库")

    step("Agent 上传产物 EXECUTION_ARTIFACT → 响应携带 oss_url 且可匿名下载")
    artifact_content = f"# T43 交付物 {uuid.uuid4().hex}\n".encode()
    artifact = client.post(
        f"{AGENT}/tasks/{task['id']}/attachments",
        params={
            "purpose": "EXECUTION_ARTIFACT",
            "execution_id": execution_id,
            "filename": "deliverable.md",
        },
        content=artifact_content,
        headers={
            "AGENT_TOKEN": fixture["w1_key"],
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "text/markdown",
        },
    )
    if artifact.status_code != 201:
        raise AssertionError(f"产物上传失败 {artifact.status_code}：{artifact.text[:400]}")
    artifact_data = artifact.json()["data"]
    artifact_oss_url = artifact_data["oss_url"]
    check(bool(artifact_oss_url), f"产物 oss_url 已返回：{str(artifact_oss_url)[:80]}…")
    artifact_fetch = client.get(artifact_oss_url)
    check(
        artifact_fetch.status_code == 200 and artifact_fetch.content == artifact_content,
        "产物 oss_url 匿名下载字节一致",
    )

    step("提交结果：content 文字包含产物 OSS URL（README 提示词新约定）")
    submission = agent_json(
        f"/tasks/{task['id']}/submit",
        fixture["w1_key"],
        {
            "execution_id": execution_id,
            "expected_version": claimed["version"],
            "summary": "已完成 T43 OSS 提示词任务",
            "content": f"变更说明：按参考附件完成工作。\n产物下载：{artifact_oss_url}",
            "self_check": [
                {"criterion": "提交说明包含产物下载地址", "passed": True, "evidence": artifact_oss_url}
            ],
            "artifacts": [{"file_id": artifact_data["file_id"]}],
            "links": [],
        },
    )
    check(submission.get("submission_id"), f"提交成功 submission_id={submission.get('submission_id')}")
    task_row = Task.objects.get(pk=task["id"])
    check(task_row.status == "IN_REVIEW", "任务转入待审核")
    check(
        artifact_oss_url in task_row.current_submission.content,
        "提交正文中包含产物 OSS URL 文本",
    )

    step("主管审核通过，闭环到 DONE")
    task_row.refresh_from_db()
    review = agent_json(
        f"/tasks/{task['id']}/review",
        fixture["s2_key"],
        {
            "submission_id": submission["submission_id"],
            "expected_version": task_row.version,
            "decision": "APPROVE",
            "comment": "通过",
        },
    )
    task_row.refresh_from_db()
    check(task_row.status == "DONE", f"审核后任务 DONE（当前 {task_row.status}）")

    step("OSS 侧复核：对象真实存在于 bucket")
    bucket = oss_storage._bucket()
    check(bool(bucket.head_object(asset.storage_key)), "参考附件对象存在于 OSS")
    check(bool(bucket.head_object(FileAsset.objects.get(pk=artifact_data['file_id']).storage_key)), "产物对象存在于 OSS")

    print()
    if FAILURES:
        print(f"T43 失败 {len(FAILURES)} 项：")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("T43 全部通过：上传→提示词→领取→产物→提交(含 URL)→审核 全链路真实 OSS 验证 OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, DomainError) as exc:
        print(f"T43 中断：{exc}")
        sys.exit(1)
