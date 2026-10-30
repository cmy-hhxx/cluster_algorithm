#!/usr/bin/env bash
# 增量聚类：新数据作为新批次加入，只分类新批次，再合并出全量树。
#
# 用法：./run_incremental.sh <组名> <新文件或目录> [批次标签]
#   组名：aime_lost_fast / aime_lost_expert / aime_daily_fast / wencai_* ...
#   目录结构：runs/incremental/<组名>/batches/<标签>/run
#             runs/incremental/<组名>/current/{run,out}
#
# 首次使用：把已有批次放进 runs/incremental/<组名>/batches/<标签>/run
# 之后每次新增只跑新批次，旧批次一条都不重分类。
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/.deploy/env.sh"

GROUP=${1:?组名，例如 aime_lost_fast}
SRC=${2:?新文件或目录}
LABEL=${3:-$(date +%Y-%m-%d)}
BASE="$HERE/runs/incremental/$GROUP"
BATCH="$BASE/batches/$LABEL/run"

if [ -d "$BATCH" ]; then
  echo "批次 $LABEL 已存在：$BATCH（换个标签，或先确认是不是重复投放）" >&2
  exit 1
fi
if ! ls "$BASE"/batches/*/run/items.jsonl >/dev/null 2>&1; then
  echo "没有历史批次：请先把已有 run 放到 $BASE/batches/<标签>/run" >&2
  exit 1
fi

cd "$HERE"
echo "[1/4] 解析新批次 $LABEL"
./run.sh init "$SRC" "$BATCH"

echo "[2/4] 只分类新批次"
./run.sh classify "$BATCH" --concurrency "${CLUSTER_CONCURRENCY:-12}"

echo "[3/4] 合并全部批次"
./run.sh merge "$BASE/current/run" "$BASE"/batches/*/run

SESSIONS=$(python3 - "$BASE/current/run/items.jsonl" <<'PY'
import json, sys
print(len({json.loads(l)["session_id"] for l in open(sys.argv[1], encoding="utf-8") if l.strip()}))
PY
)

echo "[4/4] 出全量树（$SESSIONS 会话）"
./run.sh finish "$BASE/current/run" "$BASE/current/out" "$SESSIONS" "$BASE/current/run"
echo "done: $BASE/current/out"
