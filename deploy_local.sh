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
#   WL_HOST=eval_data  WL_REPO=...  WL_VENV=...  WL_ROOT=...  WL_INDEX=...  WL_GATEWAYS=...  WL_GATEWAY_KEY=...  WL_CONC=12
#   VA_HOST=aime_rsi_data  VA_REPO=...  VA_VENV=...  VA_ROOT=/mnt/aime_eval_ssd/1_aime/eval_output  VA_INDEX=/mnt/aime_eval_ssd  VA_GATEWAYS=http://10.217.220.51/ai-gateway/v1  VA_GATEWAY_KEY=sk-gw-...  VA_CONC=8
#
# 不做的事：不装 Python 依赖、不下载字体（那些是环境初始化，见 docs/deploy.md §5，只做一次）。
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
    BACKEND=files
    HOST=${WL_HOST:-eval_data}
    REPO=${WL_REPO:-/mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm}
    VENV=${WL_VENV:-$REPO/.venv}
    ROOT=${WL_ROOT:-/mnt/fin_bench/code_and_clean_data/datasets/0_wencai/eval_output}
    CASES=${WL_CASES:-$(dirname "${WL_ROOT:-/mnt/fin_bench/code_and_clean_data/datasets/0_wencai/eval_output}")/eval_input}
    INDEX=${WL_INDEX:-/mnt/fin_bench/code_and_clean_data/datasets}
    GW=${WL_GATEWAYS:-http://interactive-djahh4fczrhq:5001/v1,http://interactive-s4lvy0mmvu32:5001/v1}
    GWKEY=${WL_GATEWAY_KEY:-dummy}
    CONC=${WL_CONC:-12}
    INTERVAL=${WL_INTERVAL:-1800}
    ;;
  aime)
    BACKEND=${VA_BACKEND:-pg}
    HOST=${VA_HOST:-aime_rsi_data}
    REPO=${VA_REPO:-/mnt/aime_eval_ssd/code/2_cluster_algorithm}
    VENV=${VA_VENV:-/opt/cluster-venv}
    # 2026-11-27 起 AIME 切至 PG 16 + 多帧 Zstd（/mnt/aime_eval_ssd/1_aime），不再扫旧 .jsonl
    DATA_ROOT=${VA_DATA_ROOT:-/mnt/aime_eval_ssd/1_aime}
    PG_CORE=${VA_PG_CORE:-/opt/pg_data_core}
    BIND_URL=${VA_BIND_URL:-http://127.0.0.1:5003/api/v1/data-core/clusters/bind}
    HEALTH_URL=${VA_HEALTH_URL:-http://127.0.0.1:5003/api/v1/data-core/health}
    ROOT="$DATA_ROOT"
    CASES=""
    INDEX=""
    # 2026-11-17 起 AIME 网关走统一网关 http://10.217.220.51/ai-gateway/v1（需要 API key）
    # API key 不入库：写在 .deploy/local.conf 的 VA_GATEWAY_KEY（.deploy/ 已 gitignore）
    GW=${VA_GATEWAYS:-http://10.217.220.51/ai-gateway/v1}
    GWKEY=${VA_GATEWAY_KEY:-dummy}
    # 2026-11-17 起网关是 LiteLLM 托管网关（按 key 限流）：并发降下来，靠 429 退避兜底
    CONC=${VA_CONC:-4}
    INTERVAL=${VA_INTERVAL:-900}
    DAILY_PUBLISH_HOUR=${VA_DAILY_PUBLISH_HOUR:-4}
    BATCH_LIMIT=${VA_BATCH_LIMIT:-5000}
    MODELS=${VA_MODELS:-aime_fast,aime_expert}
    ;;
  *) echo "渠道只能是 wencai 或 aime：$CH" >&2; exit 2 ;;
esac

VER=$(python3 -c "import json;print(json.load(open('VERSION.json'))['version'])")
# 代码指纹：两台机器比这个值就知道跑的是不是同一版（弗吉尼亚没有 .git，只能靠这个）
CODE_SHA=$(find bin taxonomy prompts -type f \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.md' \) \
  | sort | xargs shasum -a 256 | shasum -a 256 | awk '{print $1}')
NOW=$(date '+%Y-%m-%dT%H:%M:%S%z')
SRC_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

echo "渠道：${CH}（后端：${BACKEND}） -> ${HOST}:${REPO}"
echo "版本：$VER  代码指纹：${CODE_SHA:0:16}  并发：$CONC  周期：${INTERVAL}s"
[[ -n "$GW" ]] && echo "网关：$GW" || echo "网关：（留空，等开通）"
echo "数据根：$ROOT"
if [[ "$BACKEND" == "pg" ]]; then
  echo "PG 内核：${PG_CORE}  写入接口：${BIND_URL}"
  echo "模型列表：${MODELS}  日发版窗口：CST ${DAILY_PUBLISH_HOUR}:00  单轮上限：${BATCH_LIMIT}"
else
  echo "输入根：$CASES"
  echo "索引目录：$INDEX"
fi
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
  ssh "$HOST" "cd '$REPO' && rm -f \
    deploy.sh run_cluster.sh run_incremental.sh \
    bin/incremental_aime.sh bin/backfill_case_fields.py bin/build_case_index.py \
    bin/extend_v4_chart_orchestration.py bin/extend_v4_request_stage.py \
    bin/make_src_map.py bin/migrate_l1.py bin/migrate_v4_role_axis.py \
    bin/rebase_paths.py bin/taxonomy_fix.py prompts/01_assign_agent.md"
fi

echo
echo "== 渲染 .deploy/env.sh =="
# 小文件用 ssh 写入而不是 scp：这条链路上 scp 会间歇 Connection closed
put() { ssh "$HOST" "cat > '$1'" < "$2"; }
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
if [[ "$BACKEND" == "pg" ]]; then
cat > "$TMP" <<EOF
# 服务器运行环境（不入库；由本机 deploy_local.sh 渲染）。手动跑命令前：source .deploy/env.sh
D=$REPO
export PATH="$VENV/bin:\$PATH"
export PYTHONPATH="${PG_CORE}:\${PYTHONPATH:-}"
export CLUSTER_CHART_FONT="\$D/.deploy/fonts/wqy-zenhei.ttc"
export TZ=Asia/Shanghai
# 这台机器只跑这个渠道（PG 16 + 多帧 Zstd 原生模式）
export CLUSTER_CHANNEL=$CH
export CLUSTER_BACKEND=pg
export PG_DATA_CORE_PATH="$PG_CORE"
export AIME_DATA_ROOT="$DATA_ROOT"
export DATA_CORE_BIND_URL="$BIND_URL"
export DATA_CORE_HEALTH_URL="$HEALTH_URL"
export CLUSTER_MODELS="$MODELS"
export CLUSTER_CONCURRENCY=$CONC
export CLUSTER_INTERVAL=$INTERVAL
export CLUSTER_DAILY_PUBLISH_HOUR=$DAILY_PUBLISH_HOUR
export CLUSTER_PG_BATCH_LIMIT=$BATCH_LIMIT
export CLUSTER_GATEWAYS="$GW"
export EVAL_GATEWAY_API_KEY=$GWKEY
# 网关/内网不走公司 squid（docs/troubleshooting.md §3）
export NO_PROXY="interactive-s4lvy0mmvu32,interactive-djahh4fczrhq,interactive-n01laf763gwn,10.99.116.223,10.244.192.108,10.217.220.51,localhost,127.0.0.1"
export no_proxy="\$NO_PROXY"
EOF
else
cat > "$TMP" <<EOF
# 服务器运行环境（不入库；由本机 deploy_local.sh 渲染）。手动跑命令前：source .deploy/env.sh
D=$REPO
export PATH="$VENV/bin:\$PATH"
export CLUSTER_CHART_FONT="\$D/.deploy/fonts/wqy-zenhei.ttc"
export TZ=Asia/Shanghai
# 这台机器只跑这个渠道
export CLUSTER_CHANNEL=$CH
export CLUSTER_BACKEND=files
export WENCAI_ROOT="$([[ "$CH" == wencai ]] && echo "$ROOT")"
export AIME_ROOT="$([[ "$CH" == aime ]] && echo "$ROOT")"
export WENCAI_CASES_ROOT="$([[ "$CH" == wencai ]] && echo "$CASES")"
export AIME_CASES_ROOT="$([[ "$CH" == aime ]] && echo "$CASES")"
export CASE_INDEX_ROOT="$INDEX"
export CLUSTER_CONCURRENCY=$CONC
export CLUSTER_INTERVAL=$INTERVAL
export CLUSTER_GATEWAYS="$GW"
export EVAL_GATEWAY_API_KEY=$GWKEY
# 网关/内网不走公司 squid（docs/troubleshooting.md §3）
export NO_PROXY="interactive-s4lvy0mmvu32,interactive-djahh4fczrhq,interactive-n01laf763gwn,10.99.116.223,10.244.192.108,10.217.220.51,localhost,127.0.0.1"
export no_proxy="\$NO_PROXY"
EOF
fi
put "$REPO/.deploy/env.sh" "$TMP"

echo "== 写部署指纹 =="
cat > "$TMP" <<EOF
{
 "channel": "$CH",
 "backend": "$BACKEND",
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
