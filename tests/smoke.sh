#!/usr/bin/env bash
# 仓库回归冒烟测试：原位流水线 + 归类/重问/探活 + cluster_pkg + bench_module
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
cd "$ROOT"

python3 -m unittest discover -s "$HERE" -p "test_*.py" -v
if [[ -d "$ROOT/cluster_pkg/tests" ]]; then
  python3 -m unittest discover -s "$ROOT/cluster_pkg/tests" -p "test_*.py" -v
fi
echo "SMOKE OK"
