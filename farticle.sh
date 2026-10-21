#!/usr/bin/env bash
# Farticle 服务管理: install / start / stop / restart / update / status
set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$APP/venv/bin/python3"
PORT="${FARTICLE_PORT:-8900}"
CDP="${FARTICLE_CDP:-127.0.0.1:9222}"
RUN="$APP/run"
PIDFILE="$RUN/farticle.pid"
LOG="$RUN/farticle.log"
mkdir -p "$RUN"

alive() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

do_install() {
  command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
  [[ -x "$PY" ]] || { echo "创建 venv..."; python3 -m venv "$APP/venv"; }
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r "$APP/requirements.txt"
  echo "依赖就绪 ($("$PY" -V 2>&1))"
}

start() {
  if alive; then echo "已在运行 (pid $(cat "$PIDFILE"))"; return 0; fi
  [[ -x "$PY" ]] || do_install
  cd "$APP"
  nohup "$PY" -u server.py --port "$PORT" --cdp "$CDP" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 20); do
    sleep 0.5
    if curl -sf "localhost:$PORT/api/health" >/dev/null 2>&1; then
      echo "已启动 http://localhost:$PORT (pid $(cat "$PIDFILE"), 日志 $LOG)"
      return 0
    fi
    alive || break
  done
  echo "启动失败, 末尾日志:"; tail -5 "$LOG" || true
  rm -f "$PIDFILE"; exit 1
}

stop() {
  if alive; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    for _ in $(seq 20); do alive || break; sleep 0.5; done
    if alive; then kill -9 "$(cat "$PIDFILE")" 2>/dev/null || true; fi
    echo "已停止"
  else
    echo "未在运行"
  fi
  rm -f "$PIDFILE"
  # ponytail: 清端口只兜自己留的僵尸; 若 8900 被别的服务占用也会被杀, 换端口即可
  fuser -k "$PORT/tcp" 2>/dev/null && echo "已清理端口 $PORT 残留" || true
}

update() {
  cd "$APP"
  local before; before="$(git rev-parse HEAD)"
  git pull --ff-only
  if [[ "$(git rev-parse HEAD)" == "$before" ]]; then
    echo "代码已是最新 (${before:0:7})"
  else
    echo "代码更新 ${before:0:7} -> $(git rev-parse HEAD | cut -c1-7)"
    do_install
    stop; start
  fi
}

status() {
  if alive; then
    echo "运行中 pid $(cat "$PIDFILE")"
    curl -s "localhost:$PORT/api/health" && echo
    echo "文章数: $(curl -s "localhost:$PORT/api/articles" | "$PY" -c 'import json,sys;print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"
  else
    echo "未运行"
  fi
  echo "端口 $PORT · 日志 $LOG"
}

case "${1:-}" in
  install) do_install; start ;;
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  update)  update ;;
  status)  status ;;
  *) echo "用法: $0 {install|start|stop|restart|update|status}"; exit 2 ;;
esac