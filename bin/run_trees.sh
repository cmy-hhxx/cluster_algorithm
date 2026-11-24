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
# L4 下钻：默认对已验证的高影响叶子跑；CLUSTER_L4_LEAVES= 可关闭。
# 增量每轮都要跑，否则新条目永远停在 3 段，L4 占比会逐轮衰减。
L4_LEAVES=${CLUSTER_L4_LEAVES-S9.M6.4,S3.M3.2,S9.M7.4,S9.M6.1,S9.M5.1,S9.M5.4,S9.M5.2,S9.M5.3,S9.M6.2,S9.M6.3,S9.M2.1,S9.M2.2}
L4_CONC=${CLUSTER_L4_CONC:-$CONC}
(( L4_CONC <= 32 )) || { echo "L4 并发上限 32，当前 $L4_CONC" >&2; exit 2; }

run_l4() {  # $1=run 目录
  [[ -n "$L4_LEAVES" ]] || return 0
  echo "  L4 下钻：$L4_LEAVES（并发 $L4_CONC）"
  python3 bin/l4_apply.py --run "$1" --leaves "$L4_LEAVES" \
    --concurrency "$L4_CONC" --size 60 --min 1 \
    || echo "  WARN L4 归类未完全成功，本轮保留 3 段路径（不影响交付）" >&2
}

if [[ -n "$DS" ]]; then
  WENCAI_ROOT=${WENCAI_ROOT:-$DS/0_wencai/eval_output}
  AIME_ROOT=${AIME_ROOT:-$DS/1_aime/eval_output}
  CASE_INDEX_ROOT=${CASE_INDEX_ROOT:-$DS}
  WENCAI_CASES_ROOT=${WENCAI_CASES_ROOT:-$DS/0_wencai/eval_input}
  AIME_CASES_ROOT=${AIME_CASES_ROOT:-$DS/1_aime/eval_input}
fi
CH=${CH_FLAG:-${CLUSTER_CHANNEL:-auto}}
# 缺叶候选日志：写 logs/ 下（不随 run 目录清理），按渠道分开，classify 追加写
export CLUSTER_REPAIR_LOG="$ROOT/logs/missing_leaf_${CH}.jsonl"

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
# 网关探活：单次探测不可靠。2026-11-22 13:43 一次瞬时不可达（http_code=000）让整轮 BLOCKED、
# 白等 2 小时（14:50 手动探活又正常）。改为最多 4 次、每次间隔 15s：一轮本来要跑几分钟到十几分钟，
# 多等 45 秒换掉「一次抖动废掉一轮」是划算的。真的整段不可达仍返回 3（BLOCKED），语义不变。
GW_PROBE_ATTEMPTS=${CLUSTER_GW_PROBE_ATTEMPTS:-4}
GW_PROBE_SLEEP=${CLUSTER_GW_PROBE_SLEEP:-15}
check_gateways() {
  if [[ -z "${CLUSTER_GATEWAYS:-}" ]]; then
    echo "BLOCKED 没有配置归类网关（CLUSTER_GATEWAYS 为空）：环境装好了也跑不了分类。网关开通后只改 .deploy/env.sh。" >&2
    return 3
  fi
  local g code hit i
  for ((i = 1; i <= GW_PROBE_ATTEMPTS; i++)); do
    hit=0
    for g in ${CLUSTER_GATEWAYS//,/ }; do
      [[ -n "$g" ]] || continue
      code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${EVAL_GATEWAY_API_KEY:-dummy}" \
        "${g%/}/models" 2>/dev/null || true)
      echo "  网关 ${g%/}/models -> ${code:-000}（第 ${i}/${GW_PROBE_ATTEMPTS} 次）"
      [[ "$code" == "200" ]] && hit=$((hit + 1))
      [[ "$code" == "401" ]] && echo "    401：网关需要 API key，检查 .deploy/env.sh 的 EVAL_GATEWAY_API_KEY" >&2
    done
    (( hit > 0 )) && return 0
    if (( i < GW_PROBE_ATTEMPTS )); then
      echo "  网关全部不可达（第 ${i}/${GW_PROBE_ATTEMPTS} 次），${GW_PROBE_SLEEP}s 后重试" >&2
      sleep "$GW_PROBE_SLEEP"
    fi
  done
  echo "BLOCKED CLUSTER_GATEWAYS 里没有一个可达：$CLUSTER_GATEWAYS" >&2
  return 3
}

if [[ $DRY -eq 0 ]]; then check_gateways || exit $?; fi

# 树清单：tree<TAB>channel<TAB>src<TAB>deliver<TAB>src_root
ROWS=$(python3 bin/discover_trees.py --channel "$CH" \
  ${WENCAI_ROOT:+--wencai-root "$WENCAI_ROOT"} \
  ${AIME_ROOT:+--aime-root "$AIME_ROOT"} \
  --staging "$WORK/_src") || { echo "树发现失败（channel=${CH}，WENCAI_ROOT=${WENCAI_ROOT:-未设}，AIME_ROOT=${AIME_ROOT:-未设}）" >&2; exit 3; }

# case 身份索引：eval_input 148G，全量扫一次十几分钟，不能每轮都做。
# 规则：缺失 / 超过 TTL 才重建；其余轮次直接用旧索引，新 trace 查不到身份时 ingest 会退回
# 该条归因自带的 question/session（不影响条目与判定）。
# 4.4.13：TTL 12h -> 1h。原因：当天流量傍晚才落 eval_input，12h TTL 会让 19:00-00:42 这段
# 窗口的新 trace 全部查不到身份（2026-11-20 实测 traces_missing_case 涨到 2598/166k），
# 而 build_cases_index 是 state 增量重扫，稳态一轮只要 ~30s（实测 4 文件 / 31s），
# 每轮刷新完全付得起；eval_input 目录结构变动时会退化成全扫（实测 319 文件 / 337s），偶尔一次。
CASES_TTL_H=${CLUSTER_CASES_TTL_H:-1}
# 容器内存紧的时候（弗吉尼亚 16 GiB，eval 服务常驻 8 GB+）重建索引会 ENOMEM：
# 可用内存低于阈值直接跳过，本轮退回"归因自带的 question/session"，不影响条目与判定。
mem_avail_mb() {
  # 只能看 anon：file 是页缓存，cgroup 里可回收，算进来会把可用内存永远压到几十 MB，
  # 导致索引刷新每轮都被跳过（2026-11-20 23:08 实测 current/anon = 32278/21019MB，
  # 旧算法报 34MB 而真实可用约 19.9GB）。
  local mx cur anon
  mx=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)
  if [[ "$mx" == "max" || -z "$mx" ]]; then echo 999999; return; fi
  anon=$(awk '/^anon /{print $2}' /sys/fs/cgroup/memory.stat 2>/dev/null)
  if [[ -z "$anon" ]]; then
    cur=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0); anon=$cur
  fi
  echo $(( (mx - anon) / 1048576 ))
}
need_cases=0
if [[ $DRY -eq 0 && -n "$CASES_ROOT" && -n "$CASES_INDEX" && -d "$CASES_ROOT" ]]; then
  if [[ ! -f "$CASES_INDEX" ]]; then need_cases=1
  else
    mtime=$(python3 -c 'import os,sys;print(int(os.path.getmtime(sys.argv[1])))' "$CASES_INDEX")
    age_s=$(( $(date +%s) - mtime ))
    age_h=$(( age_s / 3600 ))
    if (( age_s >= CASES_TTL_H * 3600 )); then
      need_cases=1
      echo "case 身份索引已 ${age_h}h 未刷新（TTL ${CASES_TTL_H}h），本轮先增量刷新"
    else
      echo "case 身份索引 ${age_h}h 前刷新过（TTL ${CASES_TTL_H}h），本轮直接用"
    fi
  fi
  if (( need_cases )) && [[ "${CLUSTER_CASES_SKIP:-0}" == "1" ]]; then
    echo "  CLUSTER_CASES_SKIP=1：跳过 cases 索引重建"
    need_cases=0
  fi
  if (( need_cases )); then
    avail=$(mem_avail_mb)
    if (( avail < ${CLUSTER_CASES_MIN_MB:-4096} )); then
      echo "  跳过 cases 索引重建：可用内存 ${avail}MB < 阈值 ${CLUSTER_CASES_MIN_MB:-4096}MB（容器内存紧，本轮退回自带 question）"
      need_cases=0
    fi
  fi
  if (( need_cases )); then
    echo "刷新 case 身份索引（eval_input → trace_id，state 增量重扫）..."
    t_idx=$(date +%s)
    python3 bin/build_cases_index.py --channel "$CH" --root "$CASES_ROOT" --out "$CASE_INDEX_ROOT" 2>&1 | sed 's/^/  /' \
      || { echo "WARN cases 索引构建失败：ingest 将退回归因自带的 question/session" >&2; }
    echo "  索引刷新耗时 $(( $(date +%s) - t_idx ))s"
  fi
fi

selected() { [[ -z "$ONLY" ]] && return 0; [[ ",$ONLY," == *",$1,"* ]]; }
baseline() {
  local t=$1
  if [[ -f "$WORK/$t/run_merged/items.jsonl" ]]; then echo "$WORK/$t/run_merged"
  elif [[ -f "$WORK/$t/run/items.jsonl" ]]; then echo "$WORK/$t/run"
  else echo ""; fi
}
nitems() { if [[ -f "$1/items.jsonl" ]]; then wc -l < "$1/items.jsonl" | tr -d ' '; else echo 0; fi; }
sid_filled_of() {  # $1=run 目录；ingest 里 session_id 缺失改用 trace_id 的条数
  if [[ -f "$1/stats.json" ]]; then
    python3 -c "import json;print(json.load(open('$1/stats.json')).get('session_filled',0))" 2>/dev/null || echo 0
  else echo 0; fi
}
rep_count() {  # 缺叶候选日志当前行数（文件不存在算 0）
  if [[ -f "$CLUSTER_REPAIR_LOG" ]]; then wc -l < "$CLUSTER_REPAIR_LOG" | tr -d ' '; else echo 0; fi
}

ledger() {  # $1=树 $2=模式 $3=条目 $4=增量 $5=耗时 $6=校验 $7=session_id 兜底条数
  python3 - "$LEDGER" "$1" "$2" "$3" "$4" "$5" "$6" "$VER" "${CH:-unknown}" \
           "${REP_BASE:-0}" "$CLUSTER_REPAIR_LOG" "${7:-0}" <<'PY'
import json, os, sys, datetime
p, tree, mode, items, delta, elapsed, verify, ver, ch, base, rep, sidf = sys.argv[1:13]
row = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
       "channel": ch, "tree": tree, "mode": mode, "items": int(items), "delta": int(delta),
       "elapsed_s": int(elapsed), "verify": verify, "version": ver}
# 本轮这片新增的缺叶候选 / 重问命中数（见 docs/troubleshooting.md §5）
if os.path.exists(rep):
    ok = none = 0
    with open(rep, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i < int(base):
                continue
            o = json.loads(line).get("outcome")
            ok += o == "repaired"
            none += o == "none"
    if ok or none:
        row["repaired"], row["nofit"] = ok, none
if int(sidf or 0):      # 上游回补把 session_id 写成 "None" 的条数（ingest 已用 trace_id 兜底）
    row["session_filled"] = int(sidf)
with open(p, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

run_tree() {  # $1=树名 $2=输入目录 $3=交付目录 $4=src_root
  local t=$1 src=$2 deliver=$3 srcroot=$4
  local out base final t0 n delta v mode sidf=0
  out="$WORK/$t/out"
  REP_BASE=$(rep_count)

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
    sidf=$(sid_filled_of "$WORK/$t/run_new")
    python3 bin/make_delta.py --old "$base" --new "$WORK/$t/run_new" --out "$WORK/$t/run_delta" | tail -1
    delta=$(python3 -c "import json;print(json.load(open('$WORK/$t/run_delta/stats.json'))['delta'])")
    if [[ "$delta" == "0" ]]; then
      echo "  无新增条目，跳过"
      rm -rf "$WORK/$t/run_new" "$WORK/$t/run_delta"
      ledger "$t" incremental "$(nitems "$base")" 0 "$(( $(date +%s) - t0 ))" "noop" 0
      return 0
    fi
    python3 bin/cluster.py chunk --work "$WORK/$t/run_delta" --size 60
    ./run.sh classify "$WORK/$t/run_delta" --concurrency "$CONC"
    python3 bin/merge_runs.py --out "$WORK/$t/run_next" "$base" "$WORK/$t/run_delta" | tail -1
    run_l4 "$WORK/$t/run_next"
    mkdir -p "$out"; rm -f "$out/src_map.json"   # out 目录跨轮复用，旧 src_map 不删会被拷进交付
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
    sidf=$(sid_filled_of "$WORK/$t/run")
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
    run_l4 "$WORK/$t/run"
    mkdir -p "$out"; rm -f "$out/src_map.json"
    ./run.sh finish "$WORK/$t/run" "$out" 0 "$srcroot"
    rm -rf "$WORK/$t/run_merged" "$WORK/$t/run_delta" "$WORK/$t/run_new"
    final="$WORK/$t/run"
    n=$(nitems "$final")
    delta=$n
  fi

  v=$(python3 -c "import json;d=json.load(open('$out/verify.json'));print(f\"err={d['errors']},mismatch={d['locator_mismatch']}\")")
  if [[ $DELIVER -eq 1 ]]; then
    mkdir -p "$deliver"
    rm -f "$deliver/src_map.json"        # 4.4 起不再产出；旧交付里的残留要清掉，否则还是 12 件
    cp "$out"/* "$deliver"/
    echo "  交付：${deliver}（$(ls -1 "$deliver" | wc -l | tr -d ' ') 个文件）"
  fi
  echo "  结果：$n 条，$v"
  ledger "$t" "$mode" "$n" "$delta" "$(( $(date +%s) - t0 ))" "$v" "$sidf"
}

# 单棵树跑失败时，若原因是 cgroup ENOMEM（页缓存把 memory.max 打满），
# sync 后重试一次：实测第一次失败后缓存会被回收（16.3GB -> 6.1GB），重启即过。
# 只重试内存类错误，其他失败照旧立刻上报。
ENOMEM_RETRY=${CLUSTER_ENOMEM_RETRY:-1}
run_tree_guarded() {  # 参数同 run_tree
  local t=$1 tlog rc
  tlog="$ROOT/logs/tree-${t}.log"
  # 不在这里动 set -e：调用方已经 set +e，函数内改回 -e 会让非零返回直接掀掉调用方
  ( set -e; run_tree "$@" ) 2>&1 | tee "$tlog"
  rc=${PIPESTATUS[0]}
  if (( rc != 0 && ENOMEM_RETRY > 0 )) && grep -qE "Cannot allocate memory|Errno 12" "$tlog"; then
    echo "  ⟳ ENOMEM（cgroup 页缓存打满）→ sync 后重试一次 ${t}"
    sync; sleep 10
    ( set -e; run_tree "$@" ) 2>&1 | tee -a "$tlog"
    rc=${PIPESTATUS[0]}
    (( rc == 0 )) && echo "  ✓ 重试成功 ${t}"
  fi
  return "$rc"
}

FAILED_TREES=""
rep_before=0
[[ -f "$CLUSTER_REPAIR_LOG" ]] && rep_before=$(wc -l < "$CLUSTER_REPAIR_LOG" | tr -d ' ')
while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  IFS=$'\t' read -r t ch src deliver srcroot <<< "$row"
  selected "$t" || continue
  if [[ ! -d "$src" ]]; then echo "跳过 ${t}：输入目录不存在 $src"; continue; fi
  # 单棵树失败不连坐：其余树照常跑完并交付，最后统一非零退出（否则快树会被慢树的偶发失败饿死）
  # 注意：子 shell 里显式 set -e，保证树内部任何一步失败立刻中止该树，而不是带着半成品往下走
  set +e
  run_tree_guarded "$t" "$src" "$deliver" "$srcroot"
  local_rc=$?
  set -e
  if (( local_rc != 0 )); then
    FAILED_TREES="$FAILED_TREES $t"
    echo "  ✗ ${t} 失败（rc=${local_rc}，见上方日志），继续下一棵树" >&2
  fi
done <<< "$ROWS"

rep_after=0
[[ -f "$CLUSTER_REPAIR_LOG" ]] && rep_after=$(wc -l < "$CLUSTER_REPAIR_LOG" | tr -d ' ')
if (( rep_after > rep_before )); then
  echo "缺叶候选 +$((rep_after - rep_before)) 条 -> ${CLUSTER_REPAIR_LOG#$ROOT/}（重问命中/无叶子可装，明细见文件）"
  tail -n $((rep_after - rep_before)) "$CLUSTER_REPAIR_LOG" | python3 -c "
import sys, json, collections
c = collections.Counter()
for line in sys.stdin:
    d = json.loads(line)
    c[(d.get('outcome'), d.get('rejected'), d.get('chosen'))] += 1
for (o, p, q), n in c.most_common():
    print(f'  {o:9} {p} -> {q or \"(无)\"} x{n}')"
fi

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
