#!/usr/bin/env bash
# 看门狗：tl;dr —— 守护被 OOM/重启杀掉后，5 分钟内自动拉起。
#
# 这台机器的 cgroup 累计 OOM 上百次，杀掉过 sleep 循环本身；没有 crontab/systemd，
# 所以再挂一个极简会话：每 5 分钟检查 aime-incr 在不在，不在就重启。
#
# 用法：tmux new -d -s incr-watchdog "bin/watch_daemon.sh >> logs/watchdog.log 2>&1"
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
SLEEP=${1:-300}
mkdir -p logs
echo "[$(date '+%F %T')] 看门狗启动：每 ${SLEEP}s 检查 aime-incr"
while true; do
  if ! tmux has-session -t aime-incr 2>/dev/null; then
    echo "[$(date '+%F %T')] aime-incr 不在，重启"
    tmux new-session -d -s aime-incr "bin/incremental_aime.sh >> logs/incremental.log 2>&1"
    echo "[$(date '+%F %T')] 已重启 aime-incr" >> logs/failures.log
  fi
  sleep "$SLEEP"
done
