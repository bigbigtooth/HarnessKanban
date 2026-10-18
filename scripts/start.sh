#!/usr/bin/env bash
# 本地开发一键启动。五个进程各自在后台运行，本脚本在它们就绪后退出。
# 日志：logs/    pid 与 Celery Beat 调度文件：tmp/
# 不在这里执行 migrate；迁移按 README 与 deploy/RUNBOOK.md 单独做。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
LOG_DIR="$ROOT/logs"
TMP_DIR="$ROOT/tmp"
PY="$BACKEND/.venv/bin/python"
LOCK_DIR="$TMP_DIR/script.lock"

FRONTEND_PORT=7100
WEB_PORT=7101
AGENT_PORT=7102
WEB_URL="http://127.0.0.1:${WEB_PORT}/health/live"
AGENT_URL="http://127.0.0.1:${AGENT_PORT}/health/live"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}/"

LAUNCH_PID=""
LAUNCH_OFFSET=0
LAUNCH_LOG=""
LAUNCH_REUSED=0

usage() {
  cat <<EOF
用法: ./scripts/start.sh

后台启动本机开发进程（只监听 127.0.0.1）：
  WebAPI         $WEB_URL
  AgentAPI       $AGENT_URL
  Celery worker
  Celery beat
  前端           $FRONTEND_URL

日志目录: $LOG_DIR
运行时目录: $TMP_DIR
停止: ./scripts/stop.sh
EOF
}

die() {
  echo "$*" >&2
  exit 1
}

acquire_lock() {
  mkdir -p "$LOG_DIR" "$TMP_DIR"
  if [[ -d "$LOCK_DIR" ]]; then
    local old=""
    if [[ -f "$LOCK_DIR/pid" ]]; then
      old="$(tr -d '[:space:]' <"$LOCK_DIR/pid")"
    fi
    if [[ "$old" =~ ^[0-9]+$ ]] && kill -0 "$old" 2>/dev/null; then
      die "另一个 start/stop 正在运行 (pid $old)"
    fi
    rm -rf "$LOCK_DIR"
  fi
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" >"$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
}

require_tools() {
  if [[ ! -x "$PY" ]]; then
    die "未找到虚拟环境解释器: $PY
请先在 backend 执行: python -m venv .venv && .venv/bin/python -m pip install -e \".[dev]\""
  fi
  command -v pnpm >/dev/null 2>&1 || die "未找到 pnpm"
  command -v curl >/dev/null 2>&1 || die "未找到 curl"
  if [[ ! -d "$FRONTEND/node_modules" ]]; then
    die "未找到 frontend/node_modules。请先在 frontend 执行: pnpm install --frozen-lockfile"
  fi
  if [[ ! -f "$BACKEND/.env" ]]; then
    echo "提示: 未找到 backend/.env，将使用代码中的默认配置。样例见 backend/.env.example。"
  fi
}

check_redis() {
  "$PY" - "$BACKEND" <<'PY'
import os
import sys
from pathlib import Path

backend = Path(sys.argv[1])
try:
    import redis
    from dotenv import load_dotenv
except ImportError as exc:
    sys.stderr.write(f"虚拟环境缺少依赖: {exc}\n")
    sys.exit(1)

load_dotenv(backend / ".env", override=False)
url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
try:
    redis.Redis.from_url(url).ping()
except Exception as exc:
    sys.stderr.write(f"Redis 不可用: {exc}\n")
    sys.stderr.write("请先启动本机 Redis（默认 redis://127.0.0.1:6379/0）。Celery 用它做队列。\n")
    sys.exit(1)
PY
}

read_pid() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' <"$pidfile")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

listener_is_ours() {
  local own="$1"
  local pid="$2"
  local i
  [[ "$own" =~ ^[0-9]+$ ]] || return 1
  for ((i = 0; i < 8; i++)); do
    [[ "$pid" == "$own" ]] && return 0
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [[ "$pid" -le 1 ]] && return 1
  done
  return 1
}

assert_port_available() {
  local name="$1"
  local port="$2"
  command -v lsof >/dev/null 2>&1 || return 0
  local listeners own pid
  listeners="$(lsof -nP -a -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "${listeners}" ]] || return 0
  own="$(read_pid "$TMP_DIR/${name}.pid" || true)"
  for pid in $listeners; do
    if listener_is_ours "$own" "$pid"; then
      continue
    fi
    die "${name} 的端口 ${port} 已被 pid ${pid} 占用。请先结束该进程；若它是 ./scripts/start.sh 启动的，执行 ./scripts/stop.sh。"
  done
}

process_args() {
  ps -ww -o args= -p "$1" 2>/dev/null || true
}

pid_matches() {
  local name="$1"
  local pid="$2"
  local args
  args="$(process_args "$pid")"
  [[ -n "$args" ]] || return 1
  case "$name" in
    webapi) [[ "$args" == *"runserver"* && "$args" == *":${WEB_PORT}"* ]] ;;
    agentapi) [[ "$args" == *"uvicorn"* && "$args" == *"agentapi.main:app"* ]] ;;
    celery-worker) [[ "$args" == *"celery"* && "$args" == *"worker"* ]] ;;
    celery-beat) [[ "$args" == *"celery"* && "$args" == *"beat"* ]] ;;
    frontend) [[ "$args" == *"pnpm"* || "$args" == *"vite"* ]] ;;
    *) return 1 ;;
  esac
}

# 在新会话中启动，便于 stop.sh 按进程组结束子进程。pid 与会话组长相同。
launch_or_reuse() {
  local name="$1"
  local workdir="$2"
  shift 2
  local pidfile="$TMP_DIR/${name}.pid"
  local logfile="$LOG_DIR/${name}.log"
  LAUNCH_LOG="$logfile"
  LAUNCH_REUSED=0
  LAUNCH_OFFSET=0

  local existing=""
  if existing="$(read_pid "$pidfile")" && kill -0 "$existing" 2>/dev/null; then
    if pid_matches "$name" "$existing"; then
      LAUNCH_PID="$existing"
      LAUNCH_REUSED=1
      return 0
    fi
    echo "提示: $name 的 pid 文件已过期 (pid $existing 不是该服务)，将重新启动。"
  fi
  rm -f "$pidfile"

  mkdir -p "$LOG_DIR" "$TMP_DIR"
  {
    printf '===== %s 启动 %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$name"
    printf '工作目录: %s\n命令:' "$workdir"
    printf ' %q' "$@"
    printf '\n'
  } >>"$logfile"
  LAUNCH_OFFSET="$(tr -d '[:space:]' <<<"$(wc -c <"$logfile")")"

  (
    cd "$workdir"
    # shellcheck disable=SC2093
    exec "$PY" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
  ) >>"$logfile" 2>&1 &
  LAUNCH_PID=$!
  disown 2>/dev/null || true
  printf '%s\n' "$LAUNCH_PID" >"$pidfile"
  echo "[启动中] $name  pid=$LAUNCH_PID  日志=logs/${name}.log"
}

show_log_tail() {
  local logfile="$1"
  local offset="$2"
  echo "----- ${logfile#"$ROOT"/} 末尾 -----" >&2
  if [[ "$offset" -gt 0 ]]; then
    tail -c +$((offset + 1)) "$logfile" | tail -n 40 >&2 || true
  else
    tail -n 40 "$logfile" >&2 || true
  fi
}

fail_launch() {
  local name="$1"
  local detail="$2"
  echo "[失败] $name: $detail" >&2
  show_log_tail "$LAUNCH_LOG" "$LAUNCH_OFFSET"
  echo "已启动的进程会保持运行。可重新执行 ./scripts/start.sh，或先执行 ./scripts/stop.sh。" >&2
  exit 1
}

wait_http() {
  local name="$1"
  local url="$2"
  local tries="$3"
  local i
  for ((i = 0; i < tries; i++)); do
    if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      fail_launch "$name" "进程已退出"
    fi
    if curl -sf --noproxy '*' --connect-timeout 1 --max-time 2 -o /dev/null "$url"; then
      echo "[就绪] $name  $url  pid=$LAUNCH_PID"
      return 0
    fi
    sleep 0.5
  done
  fail_launch "$name" "在 $((tries / 2)) 秒内未响应 $url"
}

wait_log() {
  local name="$1"
  local pattern="$2"
  local tries="$3"
  local i
  for ((i = 0; i < tries; i++)); do
    if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      fail_launch "$name" "进程已退出"
    fi
    if tail -c +$((LAUNCH_OFFSET + 1)) "$LAUNCH_LOG" | grep -F -q -- "$pattern"; then
      echo "[就绪] $name  pid=$LAUNCH_PID"
      return 0
    fi
    sleep 0.5
  done
  fail_launch "$name" "在 $((tries / 2)) 秒内日志未出现: $pattern"
}

ensure_http() {
  local name="$1"
  local workdir="$2"
  local url="$3"
  local tries="$4"
  shift 4
  launch_or_reuse "$name" "$workdir" "$@"
  if [[ "$LAUNCH_REUSED" == 1 ]]; then
    if curl -sf --noproxy '*' --connect-timeout 1 --max-time 2 -o /dev/null "$url"; then
      echo "[已在运行] $name  $url  pid=$LAUNCH_PID"
      return 0
    fi
    fail_launch "$name" "pid 仍在，但 $url 未响应"
  fi
  wait_http "$name" "$url" "$tries"
}

ensure_log() {
  local name="$1"
  local workdir="$2"
  local pattern="$3"
  local tries="$4"
  shift 4
  launch_or_reuse "$name" "$workdir" "$@"
  if [[ "$LAUNCH_REUSED" == 1 ]]; then
    echo "[已在运行] $name  pid=$LAUNCH_PID"
    return 0
  fi
  wait_log "$name" "$pattern" "$tries"
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi
  if [[ $# -ne 0 ]]; then
    usage >&2
    exit 1
  fi

  require_tools
  acquire_lock
  check_redis
  assert_port_available webapi "$WEB_PORT"
  assert_port_available agentapi "$AGENT_PORT"
  assert_port_available frontend "$FRONTEND_PORT"

  export PYTHONUNBUFFERED=1

  ensure_http webapi "$BACKEND" "$WEB_URL" 80 \
    "$PY" manage.py runserver "127.0.0.1:${WEB_PORT}"

  ensure_http agentapi "$BACKEND" "$AGENT_URL" 60 \
    "$PY" -m uvicorn agentapi.main:app --host 127.0.0.1 --port "$AGENT_PORT"

  ensure_log celery-worker "$BACKEND" " ready." 80 \
    "$PY" -m celery -A config.celery_app:app worker --loglevel=INFO

  ensure_log celery-beat "$BACKEND" "celery beat v" 80 \
    "$PY" -m celery -A config.celery_app:app beat --loglevel=INFO \
    --schedule "$TMP_DIR/celerybeat-schedule"

  ensure_http frontend "$FRONTEND" "$FRONTEND_URL" 60 \
    pnpm dev --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort

  cat <<EOF

已启动。日志在 logs/，pid 与 Beat 调度文件在 tmp/。
  前端           http://127.0.0.1:${FRONTEND_PORT}/     pid=$(cat "$TMP_DIR/frontend.pid")
  WebAPI         http://127.0.0.1:${WEB_PORT}/         pid=$(cat "$TMP_DIR/webapi.pid")
  AgentAPI       http://127.0.0.1:${AGENT_PORT}/         pid=$(cat "$TMP_DIR/agentapi.pid")
  Celery worker                                  pid=$(cat "$TMP_DIR/celery-worker.pid")
  Celery beat                                    pid=$(cat "$TMP_DIR/celery-beat.pid")
停止: ./scripts/stop.sh
EOF
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
