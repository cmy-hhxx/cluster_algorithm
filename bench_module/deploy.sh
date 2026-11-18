#!/usr/bin/env bash
# bench_module 部署：本机 → 弗吉尼亚 AIME 机器（rsync），可选立刻跑一次构建。
#
# 用法（仓库根）：
#   ./bench_module/deploy.sh                 # 只同步代码
#   ./bench_module/deploy.sh --run           # 同步 + 后台起一次构建
#   ./bench_module/deploy.sh --run --tiers 500,1000   # 指定档位
#
# 可覆盖的环境变量：
#   BENCH_HOST=aime_rsi_data  BENCH_REPO=/mnt/aime_eval_ssd/code/2_cluster_algorithm
#   BENCH_ATTR=/mnt/aime_eval_ssd/1_aime/eval_output  BENCH_INPUT=/mnt/aime_eval_ssd/1_aime/eval_input
#   BENCH_OUT=/mnt/aime_eval_ssd/3_dynamic_bench      BENCH_TIERS=500,1000,5000,10000
#
# 只同步 bench_module/：taxonomy/ 由聚类侧部署维护，本模块只读。
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

HOST=${BENCH_HOST:-aime_rsi_data}
REPO=${BENCH_REPO:-/mnt/aime_eval_ssd/code/2_cluster_algorithm}
ATTR=${BENCH_ATTR:-/mnt/aime_eval_ssd/1_aime/eval_output}
INPUT=${BENCH_INPUT:-/mnt/aime_eval_ssd/1_aime/eval_input}
OUT=${BENCH_OUT:-/mnt/aime_eval_ssd/3_dynamic_bench}
TIERS=${BENCH_TIERS:-500,1000,5000,10000}
SEED=${BENCH_SEED:-20261118}

RUN=0
while (($#)); do
  case "$1" in
    --run) RUN=1 ;;
    --tiers) TIERS=$2; shift ;;
    --seed) SEED=$2; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

echo "目标：$HOST:$REPO/bench_module"
echo "产物：$OUT"

RETRY=(ssh -o ConnectTimeout=10 -o ServerAliveInterval=15)
# 这台机器约 40% 的首次连接会被 Connection refused，重试一次
try() { "$@" || { sleep 4; "$@"; }; }

try "${RETRY[@]}" "$HOST" "mkdir -p '$REPO/bench_module' '$OUT'"
# 这台机器 SSH 约 40% 首次连接被拒/中途断开：rsync 重试 2 次，仍失败就退回 tar 管道
ok=0
for attempt in 1 2; do
  if rsync -az --timeout=60 -e "ssh -o ConnectTimeout=10" \
      --exclude '__pycache__' --exclude '*.pyc' \
      "$HERE/" "$HOST:$REPO/bench_module/"; then ok=1; break; fi
  echo "rsync 第 $attempt 次失败，重试 …" >&2
  sleep 5
done
if (( ! ok )); then
  echo "rsync 不稳定，改用 tar 管道 …" >&2
  tar czf - -C "$HERE" --exclude '__pycache__' . \
    | ssh -o ConnectTimeout=10 "$HOST" "mkdir -p '$REPO/bench_module' && tar xzf - -C '$REPO/bench_module'"
fi
echo "代码已同步。"

if (( RUN )); then
  STAMP=$(date +%Y%m%dT%H%M%S)
  REMOTE_LOG="$OUT/_build/build_${STAMP}.stdout.log"
  try "${RETRY[@]}" "$HOST" "mkdir -p '$OUT/_build' && cd '$REPO' && \
    nohup python3 bench_module/build_bench.py \
      --attr-root '$ATTR' --eval-input '$INPUT' --out '$OUT' \
      --tiers '$TIERS' --seed '$SEED' > '$REMOTE_LOG' 2>&1 &"
  echo
  echo "已在后台启动。跟踪："
  echo "  ssh $HOST 'tail -f $REMOTE_LOG'"
  echo "完成后看："
  echo "  ssh $HOST 'cat $OUT/manifest.json'"
fi
