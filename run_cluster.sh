#!/usr/bin/env bash
# 在服务器上用 DSH headless 跑一次归因聚类。
# 用法：./run_cluster.sh <输入 jsonl|目录> <run_dir> <out_dir> [全量会话数] [cases 目录]
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/.deploy/env.sh"

SRC=${1:?输入 jsonl 或目录}
RUN=${2:?run_dir}
OUT=${3:?out_dir}
SESSIONS=${4:-0}
CASES=${5:-}

mkdir -p "$OUT"
LOG="$OUT/dsh-$(date +%Y%m%d-%H%M%S).jsonl"

TASK="用 attribution-cluster skill 对这批归因数据做聚类。

仓库：$HERE
输入：$SRC
run 目录：$RUN
输出目录：$OUT
全量会话数：$SESSIONS
${CASES:+cases 目录：$CASES}

严格按仓库 README.md 与 RUNBOOK.md 执行：run.sh init → 按 manifest 派归类子代理 → run.sh finish。
最后用三句话给出：最大单点、最普遍机制、诚实刻度。"

# 工作区 = sync_datasets：datasets/ 可读、code/ 可写，都在沙箱内
cd "$(dirname "$HERE")"
printf '%s\n' "$TASK" | dsh --profile headless --json > "$LOG"
echo "log: $LOG"
