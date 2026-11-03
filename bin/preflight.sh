#!/usr/bin/env bash
# 开跑前的预检。分三种结果：
#   OK      这一项没问题
#   BLOCKED 外部条件没到（网关没开、数据还没搬来），环境本身是对的 → 不算失败，但不许开跑
#   FAIL    环境有问题，必须修
# 退出码：有 FAIL → 1；只有 BLOCKED → 0（这样「环境部署完成」可验证）；全 OK → 0 并提示可以开跑。
#
# 用法：bin/preflight.sh [<datasets_root>]
#   渠道根优先取 .deploy/env.sh 里的 WENCAI_ROOT / AIME_ROOT / CASE_INDEX_ROOT / CLUSTER_CHANNEL，
#   没设才用位置参数 <datasets_root> 拼（兼容拆分前的单根布局）。
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
DS=${1:-}
# 先加载环境（PATH 指到 venv、字体、渠道根、网关、TZ）
if [[ -f .deploy/env.sh ]]; then source .deploy/env.sh; fi

if [[ -n "$DS" ]]; then
  WENCAI_ROOT=${WENCAI_ROOT:-$DS/0_wencai/eval_output}
  AIME_ROOT=${AIME_ROOT:-$DS/1_aime/eval_output}
  CASE_INDEX_ROOT=${CASE_INDEX_ROOT:-$DS}
fi
CH=${CLUSTER_CHANNEL:-auto}

FAIL=0; BLOCKED=()
ok()   { printf '  \033[32mOK\033[0m      %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m    %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m    %s\n' "$1"; }
blk()  { printf '  \033[35mBLOCKED\033[0m %s\n' "$1"; BLOCKED+=("$1"); }

echo "[1/8] 分类法"
OUT=$(python3 bin/taxonomy_lint.py 2>&1 | head -1) || true
echo "$OUT" | grep -q "errors=0" && ok "$OUT" || bad "$OUT"

echo "[2/8] 版本"
V=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])" 2>/dev/null || echo "?")
# 只要求 >= 4.1（分类法三轴定稿），不再钉死小版本，避免每次发版都要改预检
python3 - "$V" <<'PYV'
import sys
v = sys.argv[1]
try:
    major, minor = (int(x) for x in v.split('.')[:2])
except Exception:
    print(f"FAIL  无法解析版本号 {v!r}"); sys.exit(1)
sys.exit(0 if (major, minor) >= (4, 1) else 1)
PYV
if [[ $? -eq 0 ]]; then ok "VERSION ${V}"; else bad "VERSION ${V}，低于 4.1（分类法三轴定稿）"; fi

echo "[3/8] .deploy/env.sh"
if [[ -f .deploy/env.sh ]]; then
  ( source .deploy/env.sh ) >/dev/null 2>&1 && ok "存在且可 source（渠道 ${CH}）" || bad "存在但 source 失败"
else
  bad "缺少 .deploy/env.sh（服务器上按 DEPLOY.md 建；本地没有是正常的）"
fi

echo "[4/8] 渠道与数据根"
HR=0
if [[ "$CH" == "wencai" || "$CH" == "auto" ]] && [[ -n "${WENCAI_ROOT:-}" ]]; then
  N=$(find "$WENCAI_ROOT" -name '*attribution*.jsonl' 2>/dev/null | grep -vE '/(\.|cluster|comparison|criteria)' | wc -l | tr -d ' ')
  if [[ -d "$WENCAI_ROOT" ]]; then
    if [[ "$N" -gt 0 ]]; then ok "问财 ${WENCAI_ROOT}（${N} 个 attribution 文件）"; HR=$((HR + 1))
    else blk "问财 ${WENCAI_ROOT} 存在但没有 attribution 文件（等数据）"; fi
  else bad "问财根不存在：$WENCAI_ROOT"; fi
fi
if [[ "$CH" == "aime" || "$CH" == "auto" ]] && [[ -n "${AIME_ROOT:-}" ]]; then
  N=$(find "$AIME_ROOT" -name '*attribution*.jsonl' 2>/dev/null | grep -vE '/(\.|cluster|comparison|criteria)' | wc -l | tr -d ' ')
  if [[ -d "$AIME_ROOT" ]]; then
    if [[ "$N" -gt 0 ]]; then ok "AIME ${AIME_ROOT}（${N} 个 attribution 文件）"; HR=$((HR + 1))
    else blk "AIME ${AIME_ROOT} 存在但没有 attribution 文件（等数据搬过来）"; fi
  else bad "AIME 根不存在：$AIME_ROOT"; fi
fi
(( HR > 0 )) || { [[ ${#BLOCKED[@]} -gt 0 ]] || bad "渠道 ${CH} 没有可用的数据根（检查 WENCAI_ROOT / AIME_ROOT）"; }
if [[ -n "${CASE_INDEX_ROOT:-}" ]]; then
  [[ -d "$CASE_INDEX_ROOT" ]] && ok "索引目录 ${CASE_INDEX_ROOT}" || warn "索引目录不存在，会新建：$CASE_INDEX_ROOT"
else
  warn "没设 CASE_INDEX_ROOT：跑完后不会重建 bad_case 索引"
fi

echo "[5/8] 归类网关"
if [[ -z "${CLUSTER_GATEWAYS:-}" ]]; then
  blk "CLUSTER_GATEWAYS 为空：环境装好了，但分类跑不了（等网关开通，改 .deploy/env.sh 一行）"
else
  HIT=0
  for g in ${CLUSTER_GATEWAYS//,/ }; do
    code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "${g%/}/models" 2>/dev/null)
    if [[ "$code" == "200" ]]; then ok "${g%/}/models -> 200"; HIT=$((HIT + 1))
    else warn "${g%/}/models -> ${code:-000}"; fi
  done
  (( HIT > 0 )) || blk "没有任何网关可达：$CLUSTER_GATEWAYS（外部依赖，等开通）"
fi

echo "[6/8] 中文字体（chart 用 PIL 画图，缺字体会出方框）"
FONT=${CLUSTER_CHART_FONT:-/System/Library/Fonts/Hiragino Sans GB.ttc}
python3 - "$FONT" <<'PY'
import sys, os
try:
    from PIL import ImageFont
except Exception as e:
    print(f"FAIL  Pillow 不可用：{e}"); sys.exit(1)
p = sys.argv[1]
if not os.path.exists(p):
    print(f"FAIL  字体不存在：{p}（在 .deploy/env.sh 里设 CLUSTER_CHART_FONT）"); sys.exit(1)
try:
    ImageFont.truetype(p, 16)
except Exception as e:
    print(f"FAIL  字体无法加载：{p}：{e}"); sys.exit(1)
print(f"OK    {p}")
PY
[[ $? -eq 0 ]] || FAIL=1

echo "[7/8] 并发与资源"
CORES=$(python3 - <<'PY'
import os
try:
    q = int(open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').read())
    p = int(open('/sys/fs/cgroup/cpu/cpu.cfs_period_us').read())
    print(max(1, q // p))
except Exception:
    try:
        a, b = open('/sys/fs/cgroup/cpu.max').read().split()
        print(max(1, int(a) // int(b)) if a != 'max' else os.cpu_count() or 1)
    except Exception:
        print(os.cpu_count() or 1)
PY
)
MEMG=$(python3 - <<'PY'
try:
    print(int(open('/sys/fs/cgroup/memory/memory.limit_in_bytes').read()) // (1024 ** 3))
except Exception:
    try:
        v = open('/sys/fs/cgroup/memory.max').read().strip()
        print(int(v) // (1024 ** 3) if v != 'max' else 0)
    except Exception:
        print(0)
PY
)
CONC=${CLUSTER_CONCURRENCY:-12}
if (( MEMG > 0 )); then MEM_SHOW="${MEMG} GiB"; else MEM_SHOW="未限制"; fi
ok "容器配额：${CORES} 核 / ${MEM_SHOW}，当前并发 ${CONC}"
if (( CONC > 32 )); then bad "并发 ${CONC} 超过硬上限 32"
elif (( CONC > CORES * 2 )); then warn "并发 ${CONC} 高于 ${CORES} 核的 2 倍，建议降到 $((CORES * 2)) 以内"
fi
(( MEMG == 0 || MEMG >= 16 )) || warn "内存配额只有 ${MEMG} GiB，并发别超过 8"

echo "[8/8] 工作区"
if [[ -d .git ]]; then
  SHA=$(git rev-parse --short HEAD 2>/dev/null || true)
  if [[ -z "$SHA" ]]; then
    warn "有 .git 但 git 用不了（常见：ceph 上仓库属主与当前用户不同 → dubious ownership）；版本看 .deploy/DEPLOYED.json"
  else
    DIRTY=$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')
    [[ "$DIRTY" == "0" ]] && ok "git 干净（$SHA）" || warn "有 $DIRTY 个未提交改动（代码是 rsync 上来的，属正常）"
  fi
else
  warn "不是 git 仓库（用 rsync 部署的机器正常）；版本看 .deploy/DEPLOYED.json 的 code_sha256"
fi

echo
if (( FAIL )); then
  echo "预检未通过，先修上面 FAIL 项"
elif (( ${#BLOCKED[@]} )); then
  echo "环境就绪，但还不许开跑，等待外部条件："
  for b in "${BLOCKED[@]}"; do echo "  - $b"; done
else
  echo "预检通过，可以开跑：bin/run_trees.sh"
fi
exit $FAIL
