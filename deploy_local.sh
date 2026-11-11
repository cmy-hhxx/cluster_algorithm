#!/usr/bin/env bash
# 本地侧部署：把代码 rsync 到服务器，并渲染该机器的 .deploy/env.sh。
# 为什么不用 git：乌兰/弗吉尼亚两台机器到内网 git 仓库的路由不一样（弗吉尼亚完全不通），
# 所以代码统一从本机 rsync 上去；两台机器的版本一致性靠 .deploy/DEPLOYED.json 里的代码指纹。
#
# 用法（本机，仓库根）：
#   ./deploy_local.sh wencai [--dry-run] [--no-code] [--no-preflight]
#   ./deploy_local.sh aime   [--dry-run] [--no-code] [--no-preflight]
#
# 目标机器/路径可用 .deploy/local.conf 覆盖（gitignore，不放密钥）：
#   WL_HOST=eval_data  WL_REPO=...  WL_VENV=...  WL_ROOT=...  WL_INDEX=...  WL_GATEWAYS=...  WL_CONC=12
#   VA_HOST=aime_rsi_data  VA_REPO=...  VA_VENV=...  VA_ROOT=/mnt/aime_eval_ssd/1_aime/eval_output  VA_INDEX=/mnt/aime_eval_ssd  VA_GATEWAYS=http://10.248.72.73:8000/v1,http://10.248.72.71:8000/v1  VA_CONC=8
#
# 不做的事：不装 Python 依赖、不下载字体（那些是环境初始化，见 DEPLOY.md §6，只做一次）。
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

CH=${1:?用法: ./deploy_local.sh <wencai|aime> [--dry-run] [--no-code] [--no-preflight]}
CH=${CH#--}; shift || true
DRY=0; CODE=1; PRE=1
while (($#)); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-code) CODE=0 ;;
    --no-preflight) PRE=0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

[[ -f .deploy/local.conf ]] && source .deploy/local.conf

case "$CH" in
  wencai)
    HOST=${WL_HOST:-eval_data}
    REPO=${WL_REPO:-/mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm}
    VENV=${WL_VENV:-$REPO/.venv}
    ROOT=${WL_ROOT:-/mnt/fin_bench/code_and_clean_data/datasets/0_wencai/eval_output}
    CASES=${WL_CASES:-$(dirname "${WL_ROOT:-/mnt/fin_bench/code_and_clean_data/datasets/0_wencai/eval_output}")/eval_input}
    INDEX=${WL_INDEX:-/mnt/fin_bench/code_and_clean_data/datasets}
    GW=${WL_GATEWAYS:-http://interactive-djahh4fczrhq:5001/v1,http://interactive-s4lvy0mmvu32:5001/v1}
    CONC=${WL_CONC:-12}
    ;;
  aime)
    HOST=${VA_HOST:-aime_rsi_data}
    REPO=${VA_REPO:-/mnt/aime_eval_ssd/code/2_cluster_algorithm}
    VENV=${VA_VENV:-/opt/cluster-venv}
    # 2026-11-09 起 AIME 的数据和代码都在 SSD 布局 /mnt/aime_eval_ssd（相对结构不变：code/、1_aime/eval_output）
    ROOT=${VA_ROOT:-/mnt/aime_eval_ssd/1_aime/eval_output}
    # 2026-11-11 起规整后的输入在 <渠道>/eval_input（trace 粒度，提供 question/session_id）
    CASES=${VA_CASES:-$(dirname "${VA_ROOT:-/mnt/aime_eval_ssd/1_aime/eval_output}")/eval_input}
    INDEX=${VA_INDEX:-/mnt/aime_eval_ssd}
    GW=${VA_GATEWAYS:-http://10.248.72.73:8000/v1,http://10.248.72.71:8000/v1}
    CONC=${VA_CONC:-8}
    ;;
  *) echo "渠道只能是 wencai 或 aime：$CH" >&2; exit 2 ;;
esac

VER=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])")
# 代码指纹：两台机器比这个值就知道跑的是不是同一版（弗吉尼亚没有 .git，只能靠这个）
CODE_SHA=$(find bin taxonomy prompts -type f \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.md' \) \
  | sort | xargs shasum -a 256 | shasum -a 256 | awk '{print $1}')
NOW=$(date '+%Y-%m-%dT%H:%M:%S%z')
SRC_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

echo "渠道：$CH -> $HOST:$REPO"
echo "版本：$VER  代码指纹：${CODE_SHA:0:16}  并发：$CONC"
[[ -n "$GW" ]] && echo "网关：$GW" || echo "网关：（留空，等开通）"
echo "数据根：$ROOT"
echo "输入根：$CASES"
echo "索引目录：$INDEX"
if (( DRY )); then echo; echo "[dry-run] 只打印，不做任何传输"; exit 0; fi

ssh "$HOST" "mkdir -p '$REPO/.deploy/fonts' '$REPO/logs' '$REPO/runs'"

if (( CODE )); then
  echo
  echo "== 同步代码 =="
  # 不带 --delete：服务器上的 .deploy/runs/logs 是本地没有的，删了就是事故
  rsync -a \
    --exclude '.git' --exclude 'runs' --exclude '.venv' --exclude '.deploy' \
    --exclude '__pycache__' --exclude 'experiments/cache' --exclude 'logs' \
    ./ "$HOST:$REPO/"
  # rsync 不带 --delete（怕删掉服务器上的 .deploy/runs），下线脚本在这里显式清
  ssh "$HOST" "rm -f '$REPO/bin/incremental_aime.sh'"
fi

echo
echo "== 渲染 .deploy/env.sh =="
# 小文件用 ssh 写入而不是 scp：这条链路上 scp 会间歇 Connection closed（DEPLOY.md §7.13）
put() { ssh "$HOST" "cat > '$1'" < "$2"; }
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
# 服务器运行环境（不入库；由本机 deploy_local.sh 渲染）。手动跑命令前：source .deploy/env.sh
D=$REPO
export PATH="$VENV/bin:\$PATH"
export CLUSTER_CHART_FONT="\$D/.deploy/fonts/wqy-zenhei.ttc"
export TZ=Asia/Shanghai
# 这台机器只跑这个渠道
export CLUSTER_CHANNEL=$CH
export WENCAI_ROOT="$([[ "$CH" == wencai ]] && echo "$ROOT")"
export AIME_ROOT="$([[ "$CH" == aime ]] && echo "$ROOT")"
export WENCAI_CASES_ROOT="$([[ "$CH" == wencai ]] && echo "$CASES")"
export AIME_CASES_ROOT="$([[ "$CH" == aime ]] && echo "$CASES")"
export CASE_INDEX_ROOT="$INDEX"
export CLUSTER_CONCURRENCY=$CONC
export CLUSTER_GATEWAYS="$GW"
export EVAL_GATEWAY_API_KEY=dummy
# 网关/内网 git 不走公司 squid（DEPLOY.md §5.2）
export NO_PROXY="interactive-s4lvy0mmvu32,interactive-djahh4fczrhq,interactive-n01laf763gwn,10.99.116.223,10.248.72.73,10.248.72.71,localhost,127.0.0.1"
export no_proxy="\$NO_PROXY"
EOF
put "$REPO/.deploy/env.sh" "$TMP"

echo "== 写部署指纹 =="
cat > "$TMP" <<EOF
{
 "channel": "$CH",
 "host": "$HOST",
 "repo": "$REPO",
 "version": "$VER",
 "code_sha256": "$CODE_SHA",
 "src_commit": "$SRC_SHA",
 "deployed_at": "$NOW",
 "deployed_from": "$(hostname -s)",
 "data_root": "$ROOT",
 "cases_root": "$CASES",
 "index_root": "$INDEX",
 "gateways": "$GW"
}
EOF
put "$REPO/.deploy/DEPLOYED.json" "$TMP"
ssh "$HOST" "cd '$REPO' && echo '代码文件数：'\$(find bin taxonomy prompts -type f | wc -l) && cat .deploy/DEPLOYED.json | head -6"

if (( PRE )); then
  echo
  echo "== 远端预检 =="
  set +e
  ssh "$HOST" "cd '$REPO' && bash bin/preflight.sh"
  rc=$?
  set -e
  echo "预检退出码：${rc}（1 = 有 FAIL；0 = 全 OK 或有 BLOCKED）"
  exit "$rc"
fi
