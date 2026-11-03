#!/usr/bin/env bash
# 6 棵树增量常驻循环（服务器无 cron/systemd，用 tmux 常驻）。
#
# 用法（服务器仓库根）：
#   tmux new -d -s aime-incr "bin/incremental_aime.sh >> logs/incremental.log 2>&1"
# 停止：tmux kill-session -t aime-incr
#
# 默认每 1800 秒（30 分钟）跑一次；--incremental 只判新增条目，无新增直接跳过。
# 带 --no-full-fallback：基线缺失时**报错退出**，绝不悄悄退化成全量。
# 每轮结束写 logs/incr_status.json（最近一次时间/退出码/失败树），失败同时追加 logs/failures.log。
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
DS=${DATASETS:-/mnt/fin_bench/sync_datasets/datasets}
INTERVAL=${1:-1800}
CONC=${CLUSTER_CONCURRENCY:-16}
TREES=${TREES:-wencai_a_fast,wencai_b_fast,wencai_a_expert,wencai_b_expert,aime_fast,aime_expert}
mkdir -p logs

status() {  # $1=rc $2=note
  python3 - "$ROOT/logs/incr_status.json" "$1" "$2" "$TREES" <<'PY'
import json, sys, datetime
p, rc, note, trees = sys.argv[1:5]
json.dump({"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "rc": int(rc), "note": note, "trees": trees.split(","),
           "interval_s": int(__import__("os").environ.get("INTERVAL", "0") or 0)},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PY
}

echo "[$(date '+%F %T')] 常驻增量启动：每 ${INTERVAL}s 跑 ${TREES}，并发 ${CONC}"
while true; do
  sleep "$INTERVAL"
  echo "[$(date '+%F %T')] 增量触发"
  rc=0
  bin/run_trees.sh "$DS" --incremental --no-full-fallback --only "$TREES" --concurrency "$CONC" || rc=$?
  if (( rc != 0 )); then
    echo "[$(date '+%F %T')] 本次增量失败（退出码 ${rc}），等下一轮" | tee -a logs/failures.log
    status "$rc" "failed"
  else
    echo "[$(date '+%F %T')] 本次增量完成"
    status 0 "ok"
  fi
done
