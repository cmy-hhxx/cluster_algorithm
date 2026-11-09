#!/usr/bin/env bash
# 看门狗：每 N 秒检查本机的 cluster-incr-<渠道> 在不在，不在就重新拉起。
# 为什么要它：这台机器的 cgroup 累计 OOM 很多次，杀掉过 sleep 循环本身；又没有 crontab/systemd，
# 所以再挂一个极简会话盯着。
#
# 用法：
#   tmux new -d -s cluster-watch-wencai "bin/watch_daemon.sh wencai >> logs/watchdog-wencai.log 2>&1"
#   tmux new -d -s cluster-watch-aime   "bin/watch_daemon.sh aime   >> logs/watchdog-aime.log 2>&1"
# 停止：tmux kill-session -t cluster-watch-<渠道>
#
# 注意顺序：要停守护，先停看门狗，否则主循环 5 分钟内会被拉回来。
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
CH=${1:?用法: bin/watch_daemon.sh <wencai|aime> [检查间隔秒]}
SLEEP=${2:-300}
INTERVAL=${CLUSTER_INTERVAL:-1800}   # 被拉起的增量循环保持同一节奏（弗吉尼亚现为 7200s）
SESSION="cluster-incr-${CH}"
mkdir -p logs
echo "[$(date '+%F %T')] 看门狗启动：每 ${SLEEP}s 检查 ${SESSION}"
while true; do
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[$(date '+%F %T')] ${SESSION} 不在，重新拉起（间隔 ${INTERVAL}s）"
    tmux new-session -d -s "$SESSION" "cd $ROOT && CLUSTER_INTERVAL=$INTERVAL bin/channel_daemon.sh $CH >> logs/incremental-$CH.log 2>&1"
    echo "[$(date '+%F %T')] 已重新拉起 ${SESSION}" >> "logs/failures-$CH.log"
  fi
  sleep "$SLEEP"
done
