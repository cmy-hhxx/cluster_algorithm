#!/usr/bin/env bash
# 在服务器上跑一次归因聚类。
# 用法：./run_cluster.sh <输入 jsonl|目录> <run_dir> <out_dir> [全量会话数] [cases 目录]
#
# 引擎（环境变量 CLUSTER_ENGINE）：
#   script  默认。init → classify（直调网关，24 并发，默认 none）→ finish。
#           快速约 4,700 条/分（24–32 并发，3.5 万约 8 分钟）；质量设 CLUSTER_REASONING=low，约 2 小时。
#   dsh     DSH headless agent 跑 skill，慢一个数量级，只在需要 agent 判断时用。
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/.deploy/env.sh"

SRC=${1:?输入 jsonl 或目录}
RUN=${2:?run_dir}
OUT=${3:?out_dir}
SESSIONS=${4:-0}
CASES=${5:-}
ENGINE=${CLUSTER_ENGINE:-script}

# 相对路径按调用时的 cwd 解析成绝对路径（下面会切工作区）
abspath() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac; }
SRC=$(abspath "$SRC"); RUN=$(abspath "$RUN"); OUT=$(abspath "$OUT")
if [ -n "$CASES" ]; then CASES=$(abspath "$CASES"); fi
mkdir -p "$OUT"

# ingest 记录的 locator 是「相对 src 根」的路径：src 根 = src_path 去掉 file 前缀
src_root() {
  python3 - "$RUN" <<'PY'
import json, os, sys
run = sys.argv[1]
it = json.loads(open(os.path.join(run, "items.jsonl"), encoding="utf-8").readline())
sp, fl = it.get("src_path", ""), it.get("file", "")
# file 自 2026-11-09 起固定是「父目录名/文件名」相对形式，src_path 必须是它的绝对版
if not sp or not fl or not sp.endswith(fl):
    sys.exit(f"无法从 items.jsonl 推导 src 根：src_path={sp!r} file={fl!r}")
root = os.path.dirname(sp[: -len(fl)])
if not os.path.isdir(root):
    sys.exit(f"推导出的 src 根不存在：{root}")
print(root)
PY
}

if [ "$ENGINE" = "dsh" ]; then
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
  cd "$(dirname "$HERE")"
  SID=""; ATTEMPT=1
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
  exit 0
fi

cd "$HERE"
# 质量模式单请求更重，默认降到 12 并发；快速模式默认 24。
if [ "${CLUSTER_REASONING:-none}" = "low" ]; then CONC=${CLUSTER_CONCURRENCY:-12}; else CONC=${CLUSTER_CONCURRENCY:-24}; fi
./run.sh init "$SRC" "$RUN" $CASES
./run.sh classify "$RUN" --concurrency "$CONC" --effort "${CLUSTER_REASONING:-none}"
./run.sh finish "$RUN" "$OUT" "$SESSIONS" "$(src_root)"
