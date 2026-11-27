#!/usr/bin/env bash
# AIME PG-Native 聚类运行环境与守护自愈脚本（供容器重建后一键恢复或 eval_supervisor.sh 自动拉起）。
#
# 做什么（全部幂等，已就绪时毫秒级退出）：
#   0. 若存在 runs/.daemon_pause 维护标记，直接退出（不与人工维护打架）。
#   1. 校验 5003 data-core health 可达（容器刚重建 PG 还在 PITR 时不抢跑）。
#   2. 确保 /opt/cluster-venv 存在且开启 --system-site-packages 并可 import PIL/psycopg/zstandard。
#   3. 确保系统时区 tzdata 就绪（TZ=Asia/Shanghai 必须为 CST，保证每日 04:00 发版窗口准确）。
#   4. 确保 tmux 就绪并拉起 cluster-incr-aime 与 cluster-watch-aime（不重启已存活会话）。
#
# 用法：
#   bash bin/ensure_aime_pg_daemon.sh            # 幂等补齐环境并拉起缺失守护
#   bash bin/ensure_aime_pg_daemon.sh --check    # 只检查不改动（缺失返回非零）
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then CHECK_ONLY=1; fi

PAUSE_FILE="$ROOT/runs/.daemon_pause"
if [[ -f "$PAUSE_FILE" ]]; then
  echo "[$(date '+%F %T')] [ensure] 检测到维护标记 ${PAUSE_FILE}，跳过自愈"
  exit 0
fi

# 单实例锁（放 /tmp 避免 CephFS 锁残留）：避免 eval_supervisor 30s 周期与人工调用并发建 venv/装包
mkdir -p "$ROOT/logs" "$ROOT/runs"
if command -v flock >/dev/null 2>&1; then
  exec 8>"/tmp/cluster_aime_ensure.lock"
  if ! flock -n 8; then
    exit 0
  fi
fi

VENV=${VA_VENV:-/opt/cluster-venv}
PG_CORE=${PG_DATA_CORE_PATH:-/opt/pg_data_core}
HEALTH_URL=${DATA_CORE_HEALTH_URL:-http://127.0.0.1:5003/api/v1/data-core/health}
PIP_INDEX=${CLUSTER_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}

log() { echo "[$(date '+%F %T')] [ensure] $*"; }

venv_ready() {
  [[ -x "$VENV/bin/python3" ]] || return 1
  PYTHONPATH="${PG_CORE}:${PYTHONPATH:-}" "$VENV/bin/python3" -c \
    "import PIL, psycopg, zstandard; import pg_data_core" >/dev/null 2>&1
}

tz_ready() {
  [[ "$(TZ=Asia/Shanghai date '+%Z')" == "CST" ]]
}

daemons_ready() {
  command -v tmux >/dev/null 2>&1 || return 1
  tmux has-session -t cluster-incr-aime 2>/dev/null || return 1
  tmux has-session -t cluster-watch-aime 2>/dev/null || return 1
}

# 快速路径：全部就绪直接退出（零日志刷屏、毫秒级）
if venv_ready && tz_ready && daemons_ready; then
  exit 0
fi

if (( CHECK_ONLY )); then
  log "CHECK FAIL: venv=$(venv_ready && echo ok || echo miss) tz=$(tz_ready && echo ok || echo miss) daemons=$(daemons_ready && echo ok || echo miss)"
  exit 1
fi

# 1. 容器刚重启时先等 5003 + PG 就绪，不抢跑
if [[ ! -f "$ROOT/.deploy/env.sh" ]]; then
  log "WARN 缺少 .deploy/env.sh，暂无法拉起（请从本机执行 ./deploy_local.sh aime）"
  exit 1
fi
H_CODE=$(curl -s -m 4 --noproxy '*' -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || echo 000)
if [[ "$H_CODE" != "200" ]]; then
  log "等待 5003 data-core 就绪（当前 HTTP ${H_CODE}），稍后重试"
  exit 0
fi

# 2. 补齐 /opt/cluster-venv（--system-site-packages + pillow）
if ! venv_ready; then
  log "重建或补齐 ${VENV}（--system-site-packages + pillow）..."
  if [[ ! -x "$VENV/bin/python3" ]]; then
    python3 -m venv --system-site-packages "$VENV"
  elif grep -q '^include-system-site-packages = false' "$VENV/pyvenv.cfg" 2>/dev/null; then
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
  fi
  if ! "$VENV/bin/python3" -c "import PIL" >/dev/null 2>&1; then
    "$VENV/bin/pip" install -q -i "$PIP_INDEX" pillow
  fi
  if ! venv_ready; then
    log "ERROR ${VENV} 补齐后仍无法 import PIL/psycopg/zstandard/pg_data_core"
    exit 1
  fi
  log "${VENV} 就绪"
fi

# 3. 补齐 tzdata / tmux
if ! tz_ready; then
  log "安装 tzdata 以修复 Asia/Shanghai 时区..."
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends tzdata
fi
if ! command -v tmux >/dev/null 2>&1; then
  log "安装 tmux..."
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends tmux
fi

# 4. 加载环境并拉起缺失的 tmux 会话（绝不杀已有会话；关闭锁 fd 8 防止泄漏给子守护）
# shellcheck disable=SC1091
source "$ROOT/.deploy/env.sh"

if ! tmux has-session -t cluster-incr-aime 2>/dev/null; then
  log "拉起 tmux 会话 cluster-incr-aime（间隔 ${CLUSTER_INTERVAL:-900}s，并发 ${CLUSTER_CONCURRENCY:-4}）"
  tmux new -d -s cluster-incr-aime \
    "cd '$ROOT' && bin/channel_daemon.sh aime >> logs/incremental-aime.log 2>&1" 8>&-
fi

if ! tmux has-session -t cluster-watch-aime 2>/dev/null; then
  log "拉起 tmux 会话 cluster-watch-aime"
  tmux new -d -s cluster-watch-aime \
    "cd '$ROOT' && bin/watch_daemon.sh aime >> logs/watchdog-aime.log 2>&1" 8>&-
fi

log "AIME PG 聚类环境与双守护自愈完成"
