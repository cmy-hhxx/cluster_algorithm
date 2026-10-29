#!/usr/bin/env bash
# 服务器端更新：快进拉取 main，再跑冒烟测试。
# 用法：./deploy.sh
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "工作区有未提交改动，停止更新" >&2
  exit 1
fi

git fetch origin main
git merge --ff-only origin/main

if [[ -f "$HERE/.deploy/env.sh" ]]; then
  source "$HERE/.deploy/env.sh"
else
  echo "缺少 .deploy/env.sh，按 DEPLOY.md 初始化服务器环境" >&2
  exit 1
fi

bash tests/smoke.sh
echo "updated to $(git rev-parse --short HEAD)"
