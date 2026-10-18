#!/usr/bin/env bash
# 停止 scripts/start.sh 拉起的进程。日志保留在 logs/，pid 文件在进程退出后删除。
# 僵尸进程（state Z）已经退出，kill 不会让它消失。本脚本会先结束其仍存活的子孙，
# 再在父进程属于同一服务进程组时结束父进程，由 init 回收僵尸条目。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$ROOT/tmp"
LOCK_DIR="$TMP_DIR/script.lock"

FRONTEND_PORT=7100
WEB_PORT=7101
AGENT_PORT=7102

# 与启动顺序相反：先停调度与前端，再停消费和两个 API。
SERVICES=(frontend celery-beat celery-worker agentapi webapi)

usage() {
  cat <<EOF
用法: ./scripts/stop.sh

停止 start.sh 记录在 tmp/*.pid 中的进程、同一进程组里的子进程，以及仍占着
本机端口的匹配进程（前端 ${FRONTEND_PORT}，WebAPI ${WEB_PORT}，AgentAPI ${AGENT_PORT}）。

pid 若已是僵尸进程：不把它当成仍在运行。能安全回收时结束它的父进程；
父进程不属于本服务时只清掉 pid 文件，避免对无关进程发信号。

日志保留在 logs/。Celery Beat 调度文件保留在 tmp/。
EOF
}

die() {
  echo "$*" >&2
  exit 1
}

acquire_lock() {
  mkdir -p "$TMP_DIR"
  if [[ -d "$LOCK_DIR" ]]; then
    local old=""
    if [[ -f "$LOCK_DIR/pid" ]]; then
      old="$(tr -d '[:space:]' <"$LOCK_DIR/pid")"
    fi
    if [[ "$old" =~ ^[0-9]+$ ]] && process_exists "$old" && ! is_zombie "$old"; then
      die "另一个 start/stop 正在运行 (pid $old)"
    fi
    rm -rf "$LOCK_DIR"
  fi
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" >"$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
}

process_args() {
  ps -ww -o args= -p "$1" 2>/dev/null || true
}

process_state() {
  ps -o state= -p "$1" 2>/dev/null | tr -d '[:space:]' || true
}

process_exists() {
  kill -0 "$1" 2>/dev/null
}

is_zombie() {
  local state
  state="$(process_state "$1")"
  [[ "$state" == Z* ]]
}

is_running() {
  process_exists "$1" || return 1
  is_zombie "$1" && return 1
  return 0
}

process_pgid() {
  ps -o pgid= -p "$1" 2>/dev/null | tr -d '[:space:]' || true
}

process_ppid() {
  ps -o ppid= -p "$1" 2>/dev/null | tr -d '[:space:]' || true
}

service_port() {
  case "$1" in
    frontend) printf '%s\n' "$FRONTEND_PORT" ;;
    webapi) printf '%s\n' "$WEB_PORT" ;;
    agentapi) printf '%s\n' "$AGENT_PORT" ;;
    *) return 1 ;;
  esac
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

children_of() {
  local parent="$1"
  local pid ppid state
  while read -r pid ppid state; do
    [[ "$ppid" == "$parent" ]] || continue
    printf '%s\n' "$pid"
  done < <(ps -ax -o pid=,ppid=,state= 2>/dev/null || true)
}

kill_tree() {
  local pid="$1"
  local sig="$2"
  local child
  local children
  children="$(children_of "$pid")"
  for child in $children; do
    kill_tree "$child" "$sig"
  done
  if ! is_zombie "$pid"; then
    kill -"$sig" "$pid" 2>/dev/null || true
  fi
}

signal_service() {
  local pid="$1"
  local sig="$2"
  local pgid self_pgid
  pgid="$(process_pgid "$pid")"
  self_pgid="$(process_pgid "$$")"
  if [[ "$pgid" =~ ^[0-9]+$ && "$pgid" == "$pid" && "$pgid" != "$self_pgid" && "$pgid" -gt 1 ]]; then
    kill -"$sig" -- "-${pgid}" 2>/dev/null || kill_tree "$pid" "$sig"
  else
    kill_tree "$pid" "$sig"
  fi
}

parent_can_reap() {
  local name="$1"
  local zombie="$2"
  local ppid="$3"
  local self_pgid parent_pgid zombie_pgid args
  [[ "$ppid" =~ ^[0-9]+$ && "$ppid" -gt 1 ]] || return 1
  [[ "$ppid" == "$$" ]] && return 1
  self_pgid="$(process_pgid "$$")"
  parent_pgid="$(process_pgid "$ppid")"
  zombie_pgid="$(process_pgid "$zombie")"
  [[ -n "$parent_pgid" && "$parent_pgid" == "$self_pgid" ]] && return 1
  if pid_matches "$name" "$ppid"; then
    return 0
  fi
  args="$(process_args "$ppid")"
  if [[ "$args" == *"os.setsid"* ]]; then
    return 0
  fi
  if [[ -n "$zombie_pgid" && "$parent_pgid" == "$zombie_pgid" && "$parent_pgid" != "$self_pgid" ]]; then
    return 0
  fi
  return 1
}

reap_zombie() {
  local name="$1"
  local pid="$2"
  local depth="${3:-0}"
  local child children ppid
  is_zombie "$pid" || return 0
  if [[ "$depth" -gt 8 ]]; then
    echo "[失败] $name 僵尸进程嵌套过深 pid=$pid" >&2
    return 1
  fi

  children="$(children_of "$pid")"
  for child in $children; do
    if is_zombie "$child"; then
      reap_zombie "$name" "$child" $((depth + 1)) || return 1
    elif is_running "$child"; then
      echo "[停止] $name 僵尸进程 pid=$pid 下仍在运行的子进程 pid=$child"
      signal_service "$child" TERM
    fi
  done

  ppid="$(process_ppid "$pid")"
  echo "[僵尸] $name pid=$pid ppid=${ppid:-unknown}"
  if [[ "$ppid" =~ ^[0-9]+$ && "$ppid" -gt 1 ]]; then
    kill -CHLD "$ppid" 2>/dev/null || true
  fi

  local i
  for ((i = 0; i < 10; i++)); do
    if ! process_exists "$pid"; then
      echo "[已回收] $name 僵尸进程 pid=$pid"
      return 0
    fi
    if ! is_zombie "$pid"; then
      return 0
    fi
    sleep 0.1
  done

  if parent_can_reap "$name" "$pid" "$ppid"; then
    echo "[回收] 结束 $name 僵尸进程的父进程 pid=${ppid}，以便系统回收 pid=${pid}"
    local zombie_pgid self_pgid
    zombie_pgid="$(process_pgid "$pid")"
    self_pgid="$(process_pgid "$$")"
    if [[ "$zombie_pgid" =~ ^[0-9]+$ && "$zombie_pgid" -gt 1 && "$zombie_pgid" != "$self_pgid" ]]; then
      kill -TERM -- "-${zombie_pgid}" 2>/dev/null || signal_service "$ppid" TERM
    else
      signal_service "$ppid" TERM
    fi
    for ((i = 0; i < 20; i++)); do
      if ! process_exists "$pid"; then
        echo "[已回收] $name 僵尸进程 pid=$pid"
        return 0
      fi
      sleep 0.1
    done
    if is_running "$ppid"; then
      signal_service "$ppid" KILL
      sleep 0.2
    fi
    if ! process_exists "$pid"; then
      echo "[已回收] $name 僵尸进程 pid=$pid"
      return 0
    fi
    echo "[失败] $name 僵尸进程 pid=$pid 未能回收 (ppid=$ppid)" >&2
    return 1
  fi

  echo "[已退出] $name pid=$pid 是僵尸进程。父进程 ${ppid:-unknown} 不属于本服务，未向它发信号。"
  return 0
}

wait_gone() {
  local pid="$1"
  local i
  for ((i = 0; i < 50; i++)); do
    if is_zombie "$pid" || ! process_exists "$pid"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

stop_pid() {
  local name="$1"
  local pid="$2"
  STOP_ACTED=1
  if ! process_exists "$pid"; then
    echo "[已退出] $name  pid=$pid"
    return 0
  fi
  if is_zombie "$pid"; then
    reap_zombie "$name" "$pid"
    return $?
  fi
  if ! pid_matches "$name" "$pid"; then
    echo "[跳过] $name 的 pid $pid 已不属于该服务，不发送信号"
    return 0
  fi

  echo "[停止] $name  pid=$pid"
  signal_service "$pid" TERM
  if wait_gone "$pid"; then
    if is_zombie "$pid"; then
      reap_zombie "$name" "$pid"
      return $?
    fi
    echo "[已停止] $name"
    return 0
  fi

  echo "[强制停止] $name  pid=$pid"
  signal_service "$pid" KILL
  sleep 0.3
  if is_zombie "$pid"; then
    reap_zombie "$name" "$pid"
    return $?
  fi
  if process_exists "$pid" && is_running "$pid"; then
    echo "[失败] $name 仍在运行 pid=$pid" >&2
    return 1
  fi
  echo "[已停止] $name"
}

sweep_port() {
  local name="$1"
  local port="$2"
  local listeners pid
  command -v lsof >/dev/null 2>&1 || return 0
  listeners="$(lsof -nP -a -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "$listeners" ]] || return 0
  for pid in $listeners; do
    process_exists "$pid" || continue
    if is_zombie "$pid"; then
      STOP_ACTED=1
      reap_zombie "$name" "$pid" || return 1
      continue
    fi
    if pid_matches "$name" "$pid"; then
      stop_pid "$name" "$pid" || return 1
    fi
  done
}

sweep_matching_zombies() {
  local name="$1"
  local pid state
  while read -r pid state; do
    [[ "$state" == Z* ]] || continue
    if pid_matches "$name" "$pid"; then
      STOP_ACTED=1
      reap_zombie "$name" "$pid" || return 1
    fi
  done < <(ps -ax -o pid=,state= 2>/dev/null || true)
}

stop_one() {
  local name="$1"
  local pidfile="$TMP_DIR/${name}.pid"
  local failed=0
  STOP_ACTED=0
  if [[ -f "$pidfile" ]]; then
    local recorded
    recorded="$(tr -d '[:space:]' <"$pidfile")"
    if [[ ! "$recorded" =~ ^[0-9]+$ || "$recorded" -le 1 ]]; then
      echo "[清理] $name 的 pid 文件无效"
      rm -f "$pidfile"
      STOP_ACTED=1
    else
      stop_pid "$name" "$recorded" || failed=1
      if [[ "$failed" -eq 0 ]]; then
        rm -f "$pidfile"
      fi
    fi
  fi

  local port=""
  if port="$(service_port "$name")"; then
    sweep_port "$name" "$port" || failed=1
  fi
  sweep_matching_zombies "$name" || failed=1

  if [[ "$failed" -ne 0 ]]; then
    return 1
  fi
  if [[ "$STOP_ACTED" -eq 0 ]]; then
    echo "[未运行] $name"
  fi
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

  acquire_lock
  local name
  local failed=0
  for name in "${SERVICES[@]}"; do
    stop_one "$name" || failed=1
  done
  if [[ "$failed" -ne 0 ]]; then
    exit 1
  fi
  echo "已停止。日志仍在 logs/。"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
