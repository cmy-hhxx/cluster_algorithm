#!/usr/bin/env bash
# 开跑前的预检。任何一项不过就退出，不要等跑到一半才炸。
# 用法：bin/preflight.sh [datasets_root]
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
DS=${1:-/mnt/fin_bench/sync_datasets/datasets}
FAIL=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

echo "[1/7] 分类法"
OUT=$(python3 bin/taxonomy_lint.py 2>&1 | head -1) || true
echo "$OUT" | grep -q "errors=0" && ok "$OUT" || bad "$OUT"

echo "[2/7] 版本"
V=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])" 2>/dev/null || echo "?")
[[ "$V" == 4.1.* ]] && ok "VERSION ${V}（12 段 / 171 叶）" || bad "VERSION ${V}，与文档的 4.1.x 不一致"

echo "[3/7] .deploy/env.sh"
if [[ -f .deploy/env.sh ]]; then
  # shellcheck disable=SC1091
  ( source .deploy/env.sh ) >/dev/null 2>&1 && ok "存在且可 source" || bad "存在但 source 失败"
else
  bad "缺少 .deploy/env.sh（服务器上按 DEPLOY.md §6 建；本地没有是正常的）"
fi

echo "[4/7] 归类网关"
GW=${CLUSTER_GATEWAYS:-"http://interactive-djahh4fczrhq:5001/v1,http://interactive-s4lvy0mmvu32:5001/v1"}
HIT=0
IFS=',' read -ra GWS <<< "$GW"
for g in "${GWS[@]}"; do
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "${g%/}/models" 2>/dev/null)
  [[ "$code" == "200" ]] && { ok "$g -> 200"; HIT=$((HIT+1)); } || warn "$g -> $code"
done
[[ $HIT -gt 0 ]] || bad "没有任何网关可达：bin/classify.py 用不了（本地属正常，服务器上必须修）"

echo "[5/7] 中文字体（chart 用 PIL 画图，缺字体会出方框）"
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

echo "[6/7] 数据目录"
[[ -d "$DS" ]] && ok "${DS}（$(find "$DS" -name '*attribution*.jsonl' 2>/dev/null | wc -l | tr -d ' ') 个 attribution 文件）" || bad "数据目录不存在：${DS}"

echo "[7/7] 工作区"
DIRTY=$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')
[[ "$DIRTY" == "0" ]] && ok "干净（$(git rev-parse --short HEAD)）" || warn "有 $DIRTY 个未提交改动，deploy.sh 会拒绝更新"

echo
if [[ $FAIL -eq 0 ]]; then echo "预检通过，可以开跑：bin/run_trees.sh $DS"; else echo "预检未通过，先修上面 FAIL 项"; fi
exit $FAIL
