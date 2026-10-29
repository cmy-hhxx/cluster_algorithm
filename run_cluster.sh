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

# 相对路径按调用时的 cwd 解析成绝对路径（下面会切工作区）
abspath() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac; }
SRC=$(abspath "$SRC"); RUN=$(abspath "$RUN"); OUT=$(abspath "$OUT")
if [ -n "$CASES" ]; then CASES=$(abspath "$CASES"); fi

mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M%S)

TASK="用 attribution-cluster skill 对这批归因数据做聚类。

仓库：$HERE
输入：$SRC
run 目录：$RUN
输出目录：$OUT
全量会话数：$SESSIONS
${CASES:+cases 目录：$CASES}

严格按仓库 README.md 与 RUNBOOK.md 执行：run.sh init → 按 manifest 派归类子代理 → run.sh finish。

硬性要求：
- 必须在本回合内等到所有分片都产出 .out.jsonl、finish 与 verify 跑完，并且 verify 输出 errors=0，才能结束。
- 派完子代理后不要结束回合、不要只汇报进度；用 bash 轮询 assign 目录里的 .out.jsonl 数量，直到等于 manifest 的分片数。
- 最后用三句话给出：最大单点、最普遍机制、诚实刻度。"

# 工作区 = 仓库的上一级目录（datasets 只读、code 可写）
cd "$(dirname "$HERE")"

SID=""
ATTEMPT=1
while [ "$ATTEMPT" -le 4 ]; do
  L="$OUT/dsh-$STAMP-$ATTEMPT.jsonl"
  if [ -z "$SID" ]; then
    printf '%s\n' "$TASK" | dsh --profile headless --json > "$L" || true
  else
    printf '%s\n' "继续同一任务：run 目录 $RUN 里可能还有缺失的 assign/*.out.jsonl。补齐所有缺失分片，然后跑 run.sh finish 与 verify；verify 必须 errors=0。不要提前结束回合。" \
      | dsh --profile headless --session-id "$SID" --json > "$L" || true
  fi
  SID=$(head -1 "$L" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("sessionId",""))' 2>/dev/null || true)
  if grep -q 'leaves=[0-9]* total=' "$L"; then break; fi
  echo "attempt $ATTEMPT ended without verify; resuming" >&2
  ATTEMPT=$((ATTEMPT + 1))
done

echo "log: $OUT/dsh-$STAMP-*.jsonl"
