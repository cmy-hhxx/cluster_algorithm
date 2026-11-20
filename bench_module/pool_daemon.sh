#!/usr/bin/env bash
# 主池每 24 小时增量刷新（只刷池，不动档位）。可手动触发，也可挂在 tmux 里常驻。
#
# 用法（服务器上，仓库根）：
#   bash bench_module/pool_daemon.sh once      # 手动刷一次
#   bash bench_module/pool_daemon.sh start     # 起 tmux 守护（每 24h 一次）
#   bash bench_module/pool_daemon.sh status
#   bash bench_module/pool_daemon.sh stop
#
# 守护的只是"池子新鲜"；档位什么时候重抽由发版节奏决定（--no-refresh 从池子秒级重抽）。
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname "$HERE")

HOST_NAME=${BENCH_HOST:-aime_rsi_data}
ATTR=${BENCH_ATTR:-/mnt/aime_eval_ssd/1_aime/eval_output}
INPUT=${BENCH_INPUT:-/mnt/aime_eval_ssd/1_aime/eval_input}
OUT=${BENCH_OUT:-/mnt/aime_eval_ssd/3_dynamic_bench}
INTERVAL=${BENCH_INTERVAL:-86400}          # 24h
SESSION=${BENCH_SESSION:-bench-pool-aime}
LOG="$OUT/_build/pool_refresh.log"

refresh_once() {
  mkdir -p "$OUT/_build"
  local ts
  ts=$(date '+%Y-%m-%dT%H:%M:%S%z')
  echo "=== $ts 刷新主池 ===" >> "$LOG"
  python3 "$REPO/bench_module/build_bench.py" \
      --attr-root "$ATTR" --eval-input "$INPUT" --out "$OUT" --pool-only >> "$LOG" 2>&1
  echo "=== rc=$? ===" >> "$LOG"
}

case "${1:-once}" in
  once)
    refresh_once
    tail -6 "$LOG"
    ;;
  loop)
    while true; do
      refresh_once
      sleep "$INTERVAL"
    done
    ;;
  start)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "已存在：tmux attach -t $SESSION"
      exit 0
    fi
    tmux new-session -d -s "$SESSION" "bash '$HERE/pool_daemon.sh' loop"
    echo "已启动：tmux attach -t $SESSION（每 ${INTERVAL}s 刷一次，日志 $LOG）"
    ;;
  status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "running: $SESSION"; tail -4 "$LOG" 2>/dev/null
    else
      echo "not running"
    fi
    ;;
  stop)
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "stopped" || echo "not running"
    ;;
  *)
    echo "用法: $0 {once|start|stop|status|loop}" >&2
    exit 2
    ;;
esac
