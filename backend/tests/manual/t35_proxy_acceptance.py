"""T35 / A24 real HTTPS proxy acceptance (PLAN T35, DEVELOP D19, product 17.2).

Verifies the deployment boundary through a real nginx reverse proxy rather than by
assertion: the remote entry point forwards only the AgentAPI, `AGENT_TOKEN` survives the
hop as an underscore header, and the local management API still answers without a login.

The test also runs an A/B control. Two HTTPS servers are started from the same config, one
with ``underscores_in_headers on`` and one with ``off``: with the directive the same request
authenticates, without it nginx drops the header and the request is rejected. That control
is what proves the directive — not merely the proxy — is carrying the header.

Run explicitly (it needs nginx on PATH, plus both APIs up):

    cd backend
    DB_ENGINE=mysql MYSQL_DATABASE=harvesstask \\
      .venv/bin/python tests/manual/t35_proxy_acceptance.py

Exit code 0 means every check held.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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

from tests import factories  # noqa: E402

WORKDIR = Path("/tmp/ht-proxy-acceptance")
PROXY_PORT = 8443          # upstream entry point: underscore header allowed
CONTROL_PORT = 8444        # control: underscore header dropped by nginx
AGENT_API = "http://127.0.0.1:8001"
WEB_API = "http://127.0.0.1:8000"

FAILURES: list[str] = []

CONFIG = """\
worker_processes 1;
error_log {workdir}/logs/error.log info;
pid {workdir}/logs/nginx.pid;
events {{ worker_connections 64; }}
http {{
  access_log {workdir}/logs/access.log;
  client_max_body_size 21m;
  underscores_in_headers on;

  server {{
    listen 127.0.0.1:{proxy_port} ssl;
    server_name agent.local;
    ssl_certificate {workdir}/certs/cert.pem;
    ssl_certificate_key {workdir}/certs/key.pem;
    underscores_in_headers on;
    location /api/agent/v1/ {{
      proxy_pass {agent_api};
      proxy_set_header Host $host;
      proxy_pass_request_headers on;
    }}
    location = /health/live {{ proxy_pass {agent_api}/health/live; }}
    location / {{ return 404; }}
  }}

  server {{
    listen 127.0.0.1:{control_port} ssl;
    server_name agent-control.local;
    ssl_certificate {workdir}/certs/cert.pem;
    ssl_certificate_key {workdir}/certs/key.pem;
    underscores_in_headers off;
    location /api/agent/v1/ {{
      proxy_pass {agent_api};
      proxy_set_header Host $host;
      proxy_pass_request_headers on;
    }}
    location / {{ return 404; }}
  }}
}}
"""


def check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def prepare_config() -> Path:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    (WORKDIR / "logs").mkdir(parents=True)
    (WORKDIR / "certs").mkdir(parents=True)

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(WORKDIR / "certs" / "key.pem"),
            "-out", str(WORKDIR / "certs" / "cert.pem"),
            "-subj", "/CN=agent.local",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    config_path = WORKDIR / "nginx.conf"
    config_path.write_text(
        CONFIG.format(workdir=WORKDIR, proxy_port=PROXY_PORT, control_port=CONTROL_PORT, agent_api=AGENT_API),
        encoding="utf-8",
    )
    return config_path


def start_nginx(config_path: Path) -> subprocess.Popen:
    process = subprocess.Popen(
        ["nginx", "-c", str(config_path), "-p", str(WORKDIR), "-g", "daemon off;"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    client = httpx.Client(timeout=3.0, trust_env=False, verify=False)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            client.get(f"https://127.0.0.1:{PROXY_PORT}/health/live")
            return process
        except httpx.HTTPError:
            time.sleep(0.3)
    process.kill()
    log = (WORKDIR / "logs" / "error.log")
    detail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
    raise RuntimeError(f"nginx 未在 20s 内就绪。错误日志：\n{detail}")


def main() -> int:
    print("== T35 / A24 真实 HTTPS 代理验收 ==")
    if shutil.which("nginx") is None:
        print("未找到 nginx，无法执行本验收。")
        return 2

    for label, url in (("Django WebAPI", f"{WEB_API}/health/live"), ("AgentAPI", f"{AGENT_API}/health/live")):
        try:
            if httpx.Client(timeout=3.0, trust_env=False).get(url).status_code != 200:
                print(f"{label} 未就绪，请先启动两个服务。")
                return 2
        except httpx.HTTPError:
            print(f"{label} 不可达，请先启动两个服务。")
            return 2

    token = uuid.uuid4().hex[:6]
    agent, agent_key = factories.create_agent(f"T35-Agent-{token}", "WORKER", max_concurrency=1)
    print(f"已准备验收 Agent：{agent.name}")

    config_path = prepare_config()
    process = start_nginx(config_path)
    print(f"nginx 已启动：{PROXY_PORT}（underscores_in_headers on）、{CONTROL_PORT}（off）\n")

    client = httpx.Client(timeout=15.0, trust_env=False, verify=False)
    try:
        print("[1] 远端经 HTTPS 携带 AGENT_TOKEN 访问 AgentAPI")
        response = client.get(
            f"https://127.0.0.1:{PROXY_PORT}/api/agent/v1/me",
            headers={"AGENT_TOKEN": agent_key},
        )
        check(response.status_code == 200, f"经代理鉴权成功（实际 {response.status_code}）")
        if response.status_code == 200:
            payload = response.json()["data"]
            check(payload["name"] == agent.name, "返回的身份正是该 Agent，未被代理改写")
            check("key" not in response.text.lower() or "key_hash" not in response.text, "响应不含 Key 或摘要")

        print("\n[2] A/B 对照：underscores_in_headers 决定下划线头是否存活")
        control = client.get(
            f"https://127.0.0.1:{CONTROL_PORT}/api/agent/v1/me",
            headers={"AGENT_TOKEN": agent_key},
        )
        check(
            control.status_code == 401,
            f"关闭该指令时同一请求被拒（实际 {control.status_code}），证明头确实被丢弃",
        )
        if control.status_code == 401:
            check(
                control.json()["error"]["code"] == "AGENT_TOKEN_MISSING",
                f"拒绝原因为缺少 AGENT_TOKEN（实际 {control.json()['error']['code']}）",
            )

        print("\n[3] 代理不暴露 WebAPI")
        for path in ("/api/web/v1/dashboard", "/api/web/v1/agents", "/api/web/v1/ops/failures"):
            proxied = client.get(f"https://127.0.0.1:{PROXY_PORT}{path}")
            check(
                proxied.status_code == 404,
                f"远端访问 {path} 不可达（实际 {proxied.status_code}）",
            )

        print("\n[4] 本机 Web 管理入口无需登录")
        local = httpx.Client(timeout=10.0, trust_env=False)
        dashboard = local.get(f"{WEB_API}/api/web/v1/dashboard")
        check(dashboard.status_code == 200, f"本机 WebAPI 无凭据可访问（实际 {dashboard.status_code}）")
        health = local.get(f"{WEB_API}/health/live")
        check(health.status_code == 200 and health.json()["data"] == {"status": "live"}, "本机存活探针最小化")

        print("\n[5] 反向对照：不经过代理时同样的错误头名也不会通过")
        direct = httpx.Client(timeout=10.0, trust_env=False)
        misspelled = direct.get(
            f"{AGENT_API}/api/agent/v1/me", headers={"AGENT-TOKEN": agent_key}
        )
        check(
            misspelled.status_code == 401,
            f"把 AGENT_TOKEN 写成 AGENT-TOKEN 直接访问也被拒（实际 {misspelled.status_code}）",
        )

        print("\n[6] 代理链路仍保留可追踪的 request_id")
        proxied_me = client.get(
            f"https://127.0.0.1:{PROXY_PORT}/api/agent/v1/me", headers={"AGENT_TOKEN": agent_key}
        )
        check("X-Request-Id" in proxied_me.headers, "代理响应带回 X-Request-Id")
        check(bool(proxied_me.json().get("request_id")), "响应体包含 request_id")
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()

    print("\n== 结果 ==")
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过：AGENT_TOKEN 经真实 nginx 透传并成功鉴权，关闭 underscores_in_headers 即失败，")
    print("远端无法访问 WebAPI，本机管理入口无需登录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
