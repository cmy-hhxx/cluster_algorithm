#!/usr/bin/env bash
# 6 棵树一键跑（服务器口径）。
#   全量：init → classify（直调网关）→ finish → make_src_map → 拷到 cluster/
#   增量：ingest 新导出 → make_delta（只取未归类）→ chunk → classify → merge → finish → 交付
#
# 基线约定：每棵树只有一个「当前全量状态」目录——
#   runs/<树>/run_merged（跑过增量后）或 runs/<树>/run（只跑过全量）。
#   增量只判新增条目，已归类的条目一条不重判；全量重跑后会清掉旧的增量基线。
#
# 用法：
#   bin/run_trees.sh <datasets_root> [--incremental] [--concurrency N] [--only a,b] [--dry-run] [--no-deliver]
#
# 例：
#   bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets                 # 全量重跑 6 棵树
#   bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets --incremental   # 新数据并入既有 6 棵树
#   bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets --only aime_expert --dry-run
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# 先加载服务器环境（.deploy/env.sh 把 PATH 指到带 Pillow 的 venv、设 NO_PROXY 与字体）
# 不加载的话，tmux 里直接跑会用系统 python3，chart 步骤因缺 PIL 直接中断
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
    -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done
CONC=${CONC:-24}
(( CONC <= 32 )) || { echo "并发上限 32，当前 $CONC" >&2; exit 2; }

W="$DS/0_wencai/eval_output/0_daily_eval"
A="$DS/1_aime/eval_output"
WORK="$ROOT/runs"
VER=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])")

selected() { [[ -z "$ONLY" ]] && return 0; [[ ",$ONLY," == *",$1,"* ]]; }

stage_aime() {  # $1=树名 $2=model 目录名；AIME 是「日常+流失」两处，ingest 只吃单目录，先做软链
  local d="$WORK/_src/$1" f
  rm -rf "$d"; mkdir -p "$d/daily" "$d/lost"
  for f in "$A/0_daily_eval/$2"/*attribution*.jsonl; do [[ -e "$f" ]] && ln -sf "$f" "$d/daily/"; done
  for f in "$A/1_lost_user_eval/$2"/*attribution*.jsonl; do [[ -e "$f" ]] && ln -sf "$f" "$d/lost/"; done
  echo "$d"
}

baseline() {  # 当前全量状态目录
  local t=$1
  if [[ -f "$WORK/$t/run_merged/items.jsonl" ]]; then echo "$WORK/$t/run_merged"
  elif [[ -f "$WORK/$t/run/items.jsonl" ]];  then echo "$WORK/$t/run"
  else echo ""; fi
}

run_tree() {  # $1=树名 $2=输入目录 $3=src_root（verify 用） $4=交付目录
  local t=$1 src=$2 srcroot=$3 dst=$4
  local base out final n
  out="$WORK/$t/out"
  echo
  echo "=== $t ==="

  if [[ $DRY -eq 1 ]]; then   # 干跑：只写 *.dry，绝不碰正式目录
    python3 bin/cluster.py ingest --input "$src" --out "$WORK/$t/run_dry" | tail -1
    python3 bin/cluster.py chunk --work "$WORK/$t/run_dry" --size 60
    echo "[dry-run] 已 ingest+chunk 到 $WORK/$t/run_dry，未归类、未覆盖正式基线"
    return 0
  fi

  if [[ $INC -eq 0 ]]; then
    ./run.sh init "$src" "$WORK/$t/run"
    ./run.sh classify "$WORK/$t/run" --concurrency "$CONC"
    ./run.sh finish "$WORK/$t/run" "$out" 0 "$srcroot"
    rm -rf "$WORK/$t/run_merged" "$WORK/$t/run_delta" "$WORK/$t/run_new"   # 全量重跑后旧增量基线作废
    final="$WORK/$t/run"
  else
    base=$(baseline "$t")
    [[ -n "$base" ]] || { echo "缺少基线（$WORK/$t/run 或 run_merged），增量前要先跑一次全量" >&2; exit 1; }
    echo "  基线：${base}（$(wc -l < "$base/items.jsonl" | tr -d ' ') 条）"
    python3 bin/cluster.py ingest --input "$src" --out "$WORK/$t/run_new" | tail -1
    python3 bin/make_delta.py --old "$base" --new "$WORK/$t/run_new" --out "$WORK/$t/run_delta" | tail -1
    n=$(python3 -c "import json;print(json.load(open('$WORK/$t/run_delta/stats.json'))['delta'])")
    if [[ "$n" == "0" ]]; then
      echo "  无新增条目，跳过"
      rm -rf "$WORK/$t/run_new" "$WORK/$t/run_delta"
      return 0
    fi
    python3 bin/cluster.py chunk --work "$WORK/$t/run_delta" --size 60
    ./run.sh classify "$WORK/$t/run_delta" --concurrency "$CONC"
    python3 bin/merge_runs.py --out "$WORK/$t/run_next" "$base" "$WORK/$t/run_delta" | tail -1
    ./run.sh finish "$WORK/$t/run_next" "$out" 0 "$srcroot"
    rm -rf "$WORK/$t/run_merged"
    mv "$WORK/$t/run_next" "$WORK/$t/run_merged"
    final="$WORK/$t/run_merged"
    rm -rf "$WORK/$t/run_new" "$WORK/$t/run_delta"
  fi

  python3 bin/make_src_map.py --run "$final" --out "$out" --version "$VER"
  if [[ $DELIVER -eq 1 ]]; then
    mkdir -p "$dst"; cp "$out"/* "$dst"/
    echo "交付：${dst}（$(ls -1 "$dst" | wc -l | tr -d ' ') 个文件）"
  fi
}

if selected wencai_a_fast;   then run_tree wencai_a_fast   "$W/a_fast"   "$W" "$W/cluster/a_fast";   fi
if selected wencai_b_fast;   then run_tree wencai_b_fast   "$W/b_fast"   "$W" "$W/cluster/b_fast";   fi
if selected wencai_a_expert; then run_tree wencai_a_expert "$W/a_expert" "$W" "$W/cluster/a_expert"; fi
if selected wencai_b_expert; then run_tree wencai_b_expert "$W/b_expert" "$W" "$W/cluster/b_expert"; fi
if selected aime_fast;       then S=$(stage_aime aime_fast aime_fast);     run_tree aime_fast   "$S" "$S" "$A/cluster/aime_fast";   fi
if selected aime_expert;     then S=$(stage_aime aime_expert aime_expert); run_tree aime_expert "$S" "$S" "$A/cluster/aime_expert"; fi

echo
echo "全部完成。逐棵校验：for f in runs/*/out/verify.json; do echo \$f; cat \$f; done"
