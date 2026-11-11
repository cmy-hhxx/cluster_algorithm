#!/usr/bin/env bash
# 按渠道跑聚类。渠道、数据根都由环境变量/参数决定，树由目录自动发现。
#   全量：init → classify → finish → 拷到 <交付目录>（4.4 起不再产出 src_map，定位只认 trace_id/session_id）
#   增量：ingest → make_delta（只取未归类）→ chunk → classify → merge → finish → 交付 → 索引
#
# 两个渠道分开跑（2026-11-03 起，各一台机器）：
#   问财  <WENCAI_ROOT>/<组>/<model>            交付 <组>/cluster/<model>/
#   AIME  <AIME_ROOT>/{0_daily_eval,1_lost_user_eval}/<model>
#         日常 + 流失合成一棵树（软链到 runs/_src/<树>/{daily,lost}/，ingest 只吃单目录）
#         交付 <AIME_ROOT>/cluster/<model>/
#   其中 WENCAI_ROOT=<...>/0_wencai/eval_output、AIME_ROOT=<...>/1_aime/eval_output。
#   树由 bin/discover_trees.py 扫目录发现，新增 model 不用改代码。
#
# 根怎么定（优先级从高到低）：
#   1) 环境变量 WENCAI_ROOT / AIME_ROOT / *_CASES_ROOT / CASE_INDEX_ROOT（写在 .deploy/env.sh 里）
#   2) 位置参数 <datasets_root>：问财 <DS>/0_wencai/eval_output、AIME <DS>/1_aime/eval_output，
#      索引写 <DS>（拆分前的单根布局仍可用）
#
# 基线约定：每棵树一个「当前全量状态」目录 runs/<树>/run_merged（跑过增量）或 run（只跑过全量）。
#   增量时若基线不存在 → 自动退回全量（--no-full-fallback 则直接报错退 3）。
#
# 用法：
#   bin/run_trees.sh [<datasets_root>] [--incremental] [--rebuild] [--concurrency N] [--only a,b]
#                    [--channel wencai|aime] [--dry-run] [--no-deliver] [--no-full-fallback]
#   --rebuild：全量重新 ingest，但按 (trace_id, problem_index) 复用上一版判定（只归类新增/变更条目），
#              用于换了身份口径/交付格式之后重建树，避免 20 万条重判漂移。
#
# 例：
#   bin/run_trees.sh                                             # 只跑 .deploy/env.sh 那个渠道的全部树
#   bin/run_trees.sh --channel aime --incremental --no-full-fallback
#   bin/run_trees.sh <DS> --channel wencai                       # 兼容旧的单根用法
#   bin/run_trees.sh --only wencai_a_fast                        # 只跑一棵
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# 先加载服务器环境（.deploy/env.sh 设渠道根、网关、字体、PATH、TZ）
if [[ -f "$ROOT/.deploy/env.sh" ]]; then source "$ROOT/.deploy/env.sh"; fi

DS=""
if (($#)) && [[ $1 != -* ]]; then DS=$1; shift; fi
INC=0; CONC=${CLUSTER_CONCURRENCY:-12}; ONLY=""; DRY=0; DELIVER=1; NOFULL=0; CH_FLAG=""; REBUILD=0
while (($#)); do
  case "$1" in
    --incremental) INC=1 ;;
    --concurrency) CONC=${2:?}; shift ;;
    --only)        ONLY=${2:?}; shift ;;
    --channel)     CH_FLAG=${2:?}; shift ;;
    --dry-run)     DRY=1 ;;
    --no-deliver)  DELIVER=0 ;;
    --no-full-fallback) NOFULL=1 ;;
    --rebuild)     REBUILD=1 ;;
    -h|--help)     sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done
CONC=${CONC:-12}
(( CONC <= 32 )) || { echo "并发上限 32，当前 $CONC" >&2; exit 2; }

if [[ -n "$DS" ]]; then
  WENCAI_ROOT=${WENCAI_ROOT:-$DS/0_wencai/eval_output}
  AIME_ROOT=${AIME_ROOT:-$DS/1_aime/eval_output}
  CASE_INDEX_ROOT=${CASE_INDEX_ROOT:-$DS}
  WENCAI_CASES_ROOT=${WENCAI_CASES_ROOT:-$DS/0_wencai/eval_input}
  AIME_CASES_ROOT=${AIME_CASES_ROOT:-$DS/1_aime/eval_input}
fi
CH=${CH_FLAG:-${CLUSTER_CHANNEL:-auto}}

WORK="$ROOT/runs"
VER=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])")
# 规整后的输入根（eval_input，trace 粒度）：只提供 question/session_id/request_time，不含 problem
if [[ "$CH" == "wencai" ]]; then CASES_ROOT=${WENCAI_CASES_ROOT:-}; elif [[ "$CH" == "aime" ]]; then CASES_ROOT=${AIME_CASES_ROOT:-}; else CASES_ROOT=""; fi
CASES_INDEX=""
[[ -n "$CASES_ROOT" && -n "${CASE_INDEX_ROOT:-}" ]] && CASES_INDEX="$CASE_INDEX_ROOT/cases_${CH}.jsonl"
if [[ "$CH" == "wencai" ]]; then ART_ROOT=${WENCAI_ROOT:-}; elif [[ "$CH" == "aime" ]]; then ART_ROOT=${AIME_ROOT:-}; else ART_ROOT=""; fi
LEDGER="$WORK/_runs.jsonl"

# 同一时刻只允许一批在跑（守护与手动撞车时，后到的直接让路）
mkdir -p "$WORK"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$WORK/.lock"
  if ! flock -n 9; then echo "另一批聚类正在运行，本次跳过（锁 $WORK/.lock）"; exit 0; fi
else
  echo "提示：本机没有 flock，跳过并发锁（仅本地联调会遇到，服务器不触发）" >&2
fi

# 网关是外部依赖：没配 / 一个都连不上就别往下走，省得跑到 classify 才炸
check_gateways() {
  if [[ -z "${CLUSTER_GATEWAYS:-}" ]]; then
    echo "BLOCKED 没有配置归类网关（CLUSTER_GATEWAYS 为空）：环境装好了也跑不了分类。网关开通后只改 .deploy/env.sh。" >&2
    return 3
  fi
  local g code hit=0
  for g in ${CLUSTER_GATEWAYS//,/ }; do
    [[ -n "$g" ]] || continue
    code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "${g%/}/models" 2>/dev/null || true)
    echo "  网关 ${g%/}/models -> ${code:-000}"
    [[ "$code" == "200" ]] && hit=$((hit + 1))
  done
  if (( hit == 0 )); then
    echo "BLOCKED CLUSTER_GATEWAYS 里没有一个可达：$CLUSTER_GATEWAYS" >&2
    return 3
  fi
}

if [[ $DRY -eq 0 ]]; then check_gateways || exit $?; fi

# 树清单：tree<TAB>channel<TAB>src<TAB>deliver<TAB>src_root
ROWS=$(python3 bin/discover_trees.py --channel "$CH" \
  ${WENCAI_ROOT:+--wencai-root "$WENCAI_ROOT"} \
  ${AIME_ROOT:+--aime-root "$AIME_ROOT"} \
  --staging "$WORK/_src") || { echo "树发现失败（channel=${CH}，WENCAI_ROOT=${WENCAI_ROOT:-未设}，AIME_ROOT=${AIME_ROOT:-未设}）" >&2; exit 3; }

if [[ $DRY -eq 0 && -n "$CASES_ROOT" && -n "$CASES_INDEX" && -d "$CASES_ROOT" ]]; then
  echo "压 case 身份索引（eval_input → trace_id）..."
  python3 bin/build_cases_index.py --channel "$CH" --root "$CASES_ROOT" --out "$CASE_INDEX_ROOT" 2>&1 | sed 's/^/  /' \
    || { echo "WARN cases 索引构建失败：ingest 将退回归因自带的 question/session（无行号/路径，但可能缺问句）" >&2; }
fi

selected() { [[ -z "$ONLY" ]] && return 0; [[ ",$ONLY," == *",$1,"* ]]; }
baseline() {
  local t=$1
  if [[ -f "$WORK/$t/run_merged/items.jsonl" ]]; then echo "$WORK/$t/run_merged"
  elif [[ -f "$WORK/$t/run/items.jsonl" ]]; then echo "$WORK/$t/run"
  else echo ""; fi
}
nitems() { if [[ -f "$1/items.jsonl" ]]; then wc -l < "$1/items.jsonl" | tr -d ' '; else echo 0; fi; }
ledger() {  # $1=树 $2=模式 $3=条目 $4=增量 $5=耗时 $6=校验
  python3 - "$LEDGER" "$1" "$2" "$3" "$4" "$5" "$6" "$VER" "${CH:-unknown}" <<'PY'
import json, os, sys, datetime
p, tree, mode, items, delta, elapsed, verify, ver, ch = sys.argv[1:10]
row = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
       "channel": ch, "tree": tree, "mode": mode, "items": int(items), "delta": int(delta),
       "elapsed_s": int(elapsed), "verify": verify, "version": ver}
with open(p, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

run_tree() {  # $1=树名 $2=输入目录 $3=交付目录 $4=src_root
  local t=$1 src=$2 deliver=$3 srcroot=$4
  local out base final t0 n delta v mode
  out="$WORK/$t/out"

  echo
  echo "=== $t ==="
  echo "  输入：$src"

  if [[ $DRY -eq 1 ]]; then
    python3 bin/cluster.py ingest --input "$src" --out "$WORK/$t/run_dry" \
      ${CASES_INDEX:+--cases "$CASES_INDEX"} ${CH:+--channel "$CH"} | tail -1
    python3 bin/cluster.py chunk --work "$WORK/$t/run_dry" --size 60
    echo "[dry-run] 已 ingest+chunk 到 $WORK/$t/run_dry，未归类"
    return 0
  fi

  base=$(baseline "$t")
  if [[ $INC -eq 1 && -z "$base" && $NOFULL -eq 1 ]]; then
    echo "  $t 缺少基线，且指定了 --no-full-fallback：拒绝退化成全量" >&2
    return 3
  fi
  t0=$(date +%s)

  if [[ $INC -eq 1 && -n "$base" ]]; then
    mode=incremental
    echo "  模式：增量（基线 $(nitems "$base") 条）"
    rm -rf "$WORK/$t/run_delta"        # 上一次可能被中途杀掉，清掉避免复用半成品判定
    python3 bin/cluster.py ingest --input "$src" --out "$WORK/$t/run_new" \
      ${CASES_INDEX:+--cases "$CASES_INDEX"} ${CH:+--channel "$CH"} | tail -1
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
    # 全量必须干净：旧 assign/*.out.jsonl 若 seq 恰好对得上，classify 会误判「已完成」而复用旧判定
    rm -rf "$WORK/$t/run"
    ./run.sh init "$src" "$WORK/$t/run" "$CASES_INDEX" "$CH"
    REUSE=""
    if [[ $REBUILD -eq 1 ]]; then
      OLD=""
      [[ -f "$WORK/$t/run_merged/items.jsonl" ]] && OLD="$WORK/$t/run_merged"
      [[ -z "$OLD" && -f "$deliver/item_index.jsonl" ]] && OLD="$deliver"
      if [[ -n "$OLD" ]]; then
        python3 bin/build_reuse_map.py --old "$OLD" --work "$WORK/$t/run" --out "$WORK/$t/reuse.jsonl" | tail -1
        REUSE="$WORK/$t/reuse.jsonl"
      else
        echo "  没有可复用的旧判定（$WORK/$t/run_merged 与 $deliver 都不存在），本次为新判" >&2
      fi
    fi
    ./run.sh classify "$WORK/$t/run" --concurrency "$CONC" ${REUSE:+--reuse "$REUSE"}
    ./run.sh finish "$WORK/$t/run" "$out" 0 "$srcroot"
    rm -rf "$WORK/$t/run_merged" "$WORK/$t/run_delta" "$WORK/$t/run_new"
    final="$WORK/$t/run"
    n=$(nitems "$final")
    delta=$n
  fi

  v=$(python3 -c "import json;d=json.load(open('$out/verify.json'));print(f\"err={d['errors']},mismatch={d['locator_mismatch']}\")")
  if [[ $DELIVER -eq 1 ]]; then
    mkdir -p "$deliver"
    cp "$out"/* "$deliver"/
    echo "  交付：${deliver}（$(ls -1 "$deliver" | wc -l | tr -d ' ') 个文件）"
  fi
  echo "  结果：$n 条，$v"
  ledger "$t" "$mode" "$n" "$delta" "$(( $(date +%s) - t0 ))" "$v"
}

FAILED_TREES=""
while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  IFS=$'\t' read -r t ch src deliver srcroot <<< "$row"
  selected "$t" || continue
  if [[ ! -d "$src" ]]; then echo "跳过 ${t}：输入目录不存在 $src"; continue; fi
  # 单棵树失败不连坐：其余树照常跑完并交付，最后统一非零退出（否则快树会被慢树的偶发失败饿死）
  # 注意：子 shell 里显式 set -e，保证树内部任何一步失败立刻中止该树，而不是带着半成品往下走
  set +e
  ( set -e; run_tree "$t" "$src" "$deliver" "$srcroot" )
  local_rc=$?
  set -e
  if (( local_rc != 0 )); then
    FAILED_TREES="$FAILED_TREES $t"
    echo "  ✗ ${t} 失败（rc=${local_rc}，见上方日志），继续下一棵树" >&2
  fi
done <<< "$ROWS"

echo
if [[ -n "$FAILED_TREES" ]]; then
  echo "部分失败：${FAILED_TREES# }（其余树已正常交付，索引重建仅覆盖已交付内容）" >&2
fi
if [[ $DELIVER -eq 1 && $DRY -eq 0 ]]; then
  if [[ -n "${CASE_INDEX_ROOT:-}" ]]; then
    echo "重建 badcase / goldencase 清单（渠道 ${CH}）..."
    python3 bin/build_case_lists.py --channel "$CH" --attr-root "$ART_ROOT" \
      --out "$CASE_INDEX_ROOT" ${CASES_INDEX:+--cases-root "$CASES_INDEX"} 2>&1 | sed 's/^/  /' || \
      echo "  WARN 清单重建失败，但树本身已交付（下次跑会重试）"
  else
    echo "WARN 没有 CASE_INDEX_ROOT，跳过 bad_case 索引重建"
  fi
fi
if [[ -z "$FAILED_TREES" ]]; then
  echo "全部完成。运行记录：$LEDGER"
  exit 0
fi
echo "未全部完成，失败树：${FAILED_TREES# }。运行记录：$LEDGER" >&2
exit 1
