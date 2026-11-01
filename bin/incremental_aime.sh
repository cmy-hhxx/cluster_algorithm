#!/usr/bin/env bash
# AIME 增量常驻循环（服务器上没有 cron/systemd，用 tmux 常驻）。
#
# 用法（在服务器仓库根）：
#   tmux new -d -s aime-incr "bin/incremental_aime.sh >> logs/incremental.log 2>&1"
# 停止：
#   tmux kill-session -t aime-incr
#
# 默认每 7200 秒（2 小时）跑一次；--incremental 会自动判断：
#   基线不存在 → 全量建基线；有基线 → 只判新增条目；无新增 → 跳过并记账。
# 与全量重建共用一把锁（runs/.lock），撞车时后到的直接让路。
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
DS=${DATASETS:-/mnt/fin_bench/sync_datasets/datasets}
INTERVAL=${1:-7200}
CONC=${CLUSTER_CONCURRENCY:-16}
mkdir -p logs

echo "[$(date '+%F %T')] 常驻增量启动：每 ${INTERVAL}s 跑 aime_fast,aime_expert，并发 ${CONC}"
while true; do
  sleep "$INTERVAL"
  echo "[$(date '+%F %T')] 增量触发"
  rc=0
  bin/run_trees.sh "$DS" --incremental --only aime_fast,aime_expert --concurrency "$CONC" || rc=$?
  if (( rc != 0 )); then
    echo "[$(date '+%F %T')] 本次增量失败（退出码 ${rc}），等下一轮"
  else
    echo "[$(date '+%F %T')] 本次增量完成"
  fi
done
