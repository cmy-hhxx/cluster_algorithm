#!/usr/bin/env bash
# 冒烟测试：3 条样本走完 init → 假归类 → finish → verify
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE"); TMP=$(mktemp -d)
cd "$ROOT"
./run.sh init "$HERE/fixture/attribution.jsonl" "$TMP/run" >/dev/null
python3 - "$TMP/run" <<'PY'
import json, glob, sys, os
run = sys.argv[1]
m = {'finquery': 'S3.M3.2', 'planner-search': 'S2.M2.4', '终稿生成轮': 'S9.M6.4'}
for f in glob.glob(os.path.join(run, 'assign', '*.jsonl')):
    if f.endswith('manifest.json'):
        continue
    rows = [json.loads(l) for l in open(f, encoding='utf-8') if l.strip()]
    with open(f.replace('.jsonl', '.out.jsonl'), 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps({'seq': r['seq'], 'path': m[r['component']], 'edge': False}) + '\n')
PY
./run.sh finish "$TMP/run" "$TMP/out" 3 "$ROOT/tests" >/dev/null
python3 "$ROOT/bin/cluster.py" verify --work "$TMP/run" --out "$TMP/out" --src "$ROOT/tests" | tail -1
rm -rf "$TMP"
echo "SMOKE OK"
