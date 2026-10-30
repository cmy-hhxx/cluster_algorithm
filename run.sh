#!/usr/bin/env bash
# 确定性部分。用法：
#   ./run.sh init     <attribution.jsonl|dir> <run_dir> [cases_dir]   # 解析 + 分片
#   ./run.sh classify <run_dir> [--concurrency N]                    # 批量归类（直调模型）
#   ./run.sh finish   <run_dir> <out_dir> [sessions_all] [src_dir]   # 聚合 + 校验 + 报告
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CMD=${1:?usage: run.sh init|classify|finish ...}
shift
case "$CMD" in
  init)
    SRC=${1:?src}; RUN=${2:?run}; CASES=${3:-}
    python3 "$HERE/bin/cluster.py" ingest --input "$SRC" ${CASES:+--cases "$CASES"} --out "$RUN"
    python3 "$HERE/bin/cluster.py" chunk --work "$RUN" --size 200
    echo
    echo "下一步：./run.sh classify $RUN"
    echo "完成后：./run.sh finish $RUN <out_dir> <sessions_all> <src_dir>"
    ;;
  classify)
    RUN=${1:?run}; shift
    python3 "$HERE/bin/classify.py" --work "$RUN" "$@"
    ;;
  finish)
    RUN=${1:?run}; OUT=${2:?out}; SESSIONS_ALL=${3:-0}; SRC=${4:-$RUN}
    python3 "$HERE/bin/cluster.py" aggregate --work "$RUN" --out "$OUT" --sessions-all "$SESSIONS_ALL"
    python3 "$HERE/bin/cluster.py" verify --work "$RUN" --out "$OUT" --src "$SRC"
    python3 "$HERE/bin/cluster.py" report --out "$OUT"
    python3 "$HERE/bin/cluster.py" chart --out "$OUT"
    python3 "$HERE/bin/cluster.py" syndrome --out "$OUT"
    python3 "$HERE/bin/make_manifest.py" --out "$OUT" --run "$RUN"
    ;;
  *) echo "unknown command: $CMD" >&2; exit 2 ;;
esac
