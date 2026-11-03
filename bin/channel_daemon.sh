#!/usr/bin/env bash
# 单渠道增量常驻循环（服务器没有 cron/systemd，用 tmux 常驻）。
#
# 用法（仓库根，一台机器只跑自己那个渠道）：
#   tmux new -d -s cluster-incr-wencai "bin/channel_daemon.sh wencai >> logs/incremental-wencai.log 2>&1"
#   tmux new -d -s cluster-incr-aime   "bin/channel_daemon.sh aime   >> logs/incremental-aime.log 2>&1"
# 配合看门狗：bin/watch_daemon.sh <渠道>
# 停止：tmux kill-session -t cluster-incr-<渠道>
#
# 默认每 1800 秒跑一轮；--incremental 只判新增条目，无新增直接跳过（只写一行 noop 记录）。
# 带 --no-full-fallback：基线缺失时**报错退出**，绝不悄悄退化成全量。
# 每轮写 logs/incr_status_<渠道>.json；失败追加 logs/failures_<渠道>.log。
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
CH=${1:?用法: bin/channel_daemon.sh <wencai|aime> [间隔秒]}
INTERVAL=${2:-${CLUSTER_INTERVAL:-1800}}
CONC=${CLUSTER_CONCURRENCY:-12}
mkdir -p logs
STATUS="logs/incr_status_${CH}.json"
FAILS="logs/failures_${CH}.log"
# 和 run_trees.sh 一样先从 .deploy/env.sh 拿渠道根与网关
if [[ -f "$ROOT/.deploy/env.sh" ]]; then source "$ROOT/.deploy/env.sh"; fi

status() {  # $1=rc $2=note
  python3 - "$STATUS" "$1" "$2" "$CH" "$INTERVAL" <<'PY'
import json, sys, datetime
p, rc, note, ch, itv = sys.argv[1:6]
json.dump({"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "channel": ch, "rc": int(rc), "note": note, "interval_s": int(itv)},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PY
}

echo "[$(date '+%F %T')] 常驻增量启动：渠道 ${CH}，每 ${INTERVAL}s 一轮，并发 ${CONC}"
while true; do
  sleep "$INTERVAL"
  # 网关还没开通的机器就安静等待，不往失败日志里刷垃圾
  if [[ -z "${CLUSTER_GATEWAYS:-}" ]]; then
    echo "[$(date '+%F %T')] 渠道 ${CH} 未配置网关，本轮等待"
    status 0 "blocked-no-gateway"
    continue
  fi
  echo "[$(date '+%F %T')] 增量触发（渠道 ${CH}）"
  rc=0
  bin/run_trees.sh --channel "$CH" --incremental --no-full-fallback --concurrency "$CONC" || rc=$?
  if (( rc != 0 )); then
    echo "[$(date '+%F %T')] 本次增量失败（退出码 ${rc}），等下一轮" | tee -a "$FAILS"
    status "$rc" "failed"
  else
    echo "[$(date '+%F %T')] 本次增量完成"
    status 0 "ok"
  fi
done
