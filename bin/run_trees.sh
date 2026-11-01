#!/usr/bin/env bash
# 8 棵树一键跑（问财日常 4 + AIME 日常 2 + AIME 流失 2）。
#   全量：init → classify → finish → make_src_map → 拷到 <输入目录同级>/cluster/<model>/
#   增量：ingest → make_delta（只取未归类）→ chunk → classify → merge → finish → 交付
#
# 树与目录（<D> = datasets 根）：
#   问财日常  <D>/0_wencai/eval_output/0_daily_eval/{a_fast,b_fast,a_expert,b_expert}
#   AIME日常  <D>/1_aime/eval_output/0_daily_eval/{aime_fast,aime_expert}
#   AIME流失  <D>/1_aime/eval_output/1_lost_user_eval/{aime_fast,aime_expert}
#   交付      就在各自 eval 目录下的 cluster/<model>/
#
# 基线约定：每棵树一个「当前全量状态」目录 runs/<树>/run_merged（跑过增量）或 run（只跑过全量）。
#   增量时若基线不存在 → 自动退回全量（cron 第一次触发就能把基线建好）。
#'
# 用法：
#   bin/run_trees.sh <datasets_root> [--incremental] [--concurrency N] [--only a,b] [--dry-run] [--no-deliver]
#
# 例：
#   bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets                        # 8 棵全量
#   bin/run_trees.sh <D> --only wencai_a_fast,aime_fast_daily                    # 只跑几棵
#   bin/run_trees.sh <D> --incremental --only aime_fast_lost,aime_expert_lost     # 流失增量
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# 先加载服务器环境（.deploy/env.sh 把 PATH 指到带 Pillow 的 venv、设 NO_PROXY 与字体）
if [[ -f "$ROOT/.deploy/env.sh" ]]; then source "$ROOT/.deploy/env.sh"; fi

DS=${1:?用法: bin/run_trees.sh <datasets_root> [--incremental] [--concurrency N] [--only a,b] [--dry-run] [--no-deliver]}
shift || true
INC=0; CONC=${CLUSTER_CONCURRENCY:-24}; ONLY=""; DRY=0; DELIVER=1
while (($#)); do
  case "$1" in
    --incremental) INC=1 ;;
    --concurrency) CONC=${2:?}; shift ;;
    --only)        ONLY=${2:?}; shift ;;
    --dry-run)     DRY=1 ;;
    --no-deliver)  DELIVER=0 ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done
CONC=${CONC:-24}
(( CONC <= 32 )) || { echo "并发上限 32，当前 $CONC" >&2; exit 2; }

WORK="$ROOT/runs"
VER=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])")
LEDGER="$WORK/_runs.jsonl"

# 同一时刻只允许一批在跑（cron 与手动撞车时，后到的直接让路）
mkdir -p "$WORK"
exec 9>"$WORK/.lock"
if ! flock -n 9; then echo "另一批聚类正在运行，本次跳过（锁 $WORK/.lock）"; exit 0; fi

W="$DS/0_wencai/eval_output/0_daily_eval"
AD="$DS/1_aime/eval_output/0_daily_eval"
AL="$DS/1_aime/eval_output/1_lost_user_eval"

TREES=(
  "wencai_a_fast|$W/a_fast"
  "wencai_b_fast|$W/b_fast"
  "wencai_a_expert|$W/a_expert"
  "wencai_b_expert|$W/b_expert"
  "aime_fast_daily|$AD/aime_fast"
  "aime_expert_daily|$AD/aime_expert"
  "aime_fast_lost|$AL/aime_fast"
  "aime_expert_lost|$AL/aime_expert"
)

selected() { [[ -z "$ONLY" ]] && return 0; [[ ",$ONLY," == *",$1,"* ]]; }
baseline() {
  local t=$1
  if [[ -f "$WORK/$t/run_merged/items.jsonl" ]]; then echo "$WORK/$t/run_merged"
  elif [[ -f "$WORK/$t/run/items.jsonl" ]]; then echo "$WORK/$t/run"
  else echo ""; fi
}
nitems() { if [[ -f "$1/items.jsonl" ]]; then wc -l < "$1/items.jsonl" | tr -d ' '; else echo 0; fi; }
ledger() {  # $1=树 $2=模式 $3=条目 $4=增量 $5=耗时 $6=校验
  python3 - "$LEDGER" "$1" "$2" "$3" "$4" "$5" "$6" "$VER" <<'PY'
import json, os, sys, datetime
p, tree, mode, items, delta, elapsed, verify, ver = sys.argv[1:9]
row = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
       "tree": tree, "mode": mode, "items": int(items), "delta": int(delta),
       "elapsed_s": int(elapsed), "verify": verify, "version": ver}
with open(p, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

run_tree() {  # $1=树名 $2=输入目录
  local t=$1 src=$2
  local srcroot out deliver base final t0 n delta v mode
  srcroot=$(dirname "$src")
  out="$WORK/$t/out"
  deliver="$srcroot/cluster/$(basename "$src")"

  echo
  echo "=== $t ==="
  echo "  输入：$src"

  if [[ $DRY -eq 1 ]]; then
    python3 bin/cluster.py ingest --input "$src" --out "$WORK/$t/run_dry" | tail -1
    python3 bin/cluster.py chunk --work "$WORK/$t/run_dry" --size 60
    echo "[dry-run] 已 ingest+chunk 到 $WORK/$t/run_dry，未归类"
    return 0
  fi

  base=$(baseline "$t")
  t0=$(date +%s)

  if [[ $INC -eq 1 && -n "$base" ]]; then
    mode=incremental
    echo "  模式：增量（基线 $(nitems "$base") 条）"
    python3 bin/cluster.py ingest --input "$src" --out "$WORK/$t/run_new" | tail -1
    python3 bin/make_delta.py --old "$base" --new "$WORK/$t/run_new" --out "$WORK/$t/run_delta" | tail -1
    delta=$(python3 -c "import json;print(json.load(open('$WORK/$t/run_delta/stats.json'))['delta'])")
    if [[ "$delta" == "0" ]]; then
      echo "  无新增条目，跳过"
      rm -rf "$WORK/$t/run_new" "$WORK/$t/run_delta"
      ledger "$t" incremental "$(nitems "$base")" 0 "$(( $(date +%s) - t0 ))" "noop"
      return 0
    fi
    python3 bin/cluster.py chunk --work "$WORK/$t/run_delta" --size 60
    ./run.sh classify "$WORK/$t/run_delta" --concurrency "$CONC"
    python3 bin/merge_runs.py --out "$WORK/$t/run_next" "$base" "$WORK/$t/run_delta" | tail -1
    ./run.sh finish "$WORK/$t/run_next" "$out" 0 "$srcroot"
    rm -rf "$WORK/$t/run_merged"; mv "$WORK/$t/run_next" "$WORK/$t/run_merged"
    final="$WORK/$t/run_merged"
    rm -rf "$WORK/$t/run_new" "$WORK/$t/run_delta"
    n=$(nitems "$final")
  else
    mode=full
    [[ $INC -eq 1 ]] && echo "  模式：全量（尚无基线，自动建基线）"
    ./run.sh init "$src" "$WORK/$t/run"
    ./run.sh classify "$WORK/$t/run" --concurrency "$CONC"
    ./run.sh finish "$WORK/$t/run" "$out" 0 "$srcroot"
    rm -rf "$WORK/$t/run_merged" "$WORK/$t/run_delta" "$WORK/$t/run_new"
    final="$WORK/$t/run"
    n=$(nitems "$final")
    delta=$n
  fi

  python3 bin/make_src_map.py --run "$final" --out "$out" --version "$VER"
  v=$(python3 -c "import json;d=json.load(open('$out/verify.json'));print(f\"err={d['errors']},mismatch={d['locator_mismatch']}\")")
  if [[ $DELIVER -eq 1 ]]; then
    mkdir -p "$deliver"
    cp "$out"/* "$deliver"/
    echo "  交付：$deliver（$(ls -1 "$deliver" | wc -l | tr -d ' ') 个文件）"
  fi
  echo "  结果：$n 条，$v"
  ledger "$t" "$mode" "$n" "$delta" "$(( $(date +%s) - t0 ))" "$v"
}

for row in "${TREES[@]}"; do
  t=${row%%|*}; src=${row#*|}
  selected "$t" || continue
  if [[ ! -d "$src" ]]; then echo "跳过 $t：输入目录不存在 $src"; continue; fi
  run_tree "$t" "$src"
done

echo
echo "全部完成。运行记录：$LEDGER"
