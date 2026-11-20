#!/usr/bin/env python3
"""session_id 兜底单测（4.4.11）

上游回补（fill_source=session_fill）会把缺失的 session_id 写成字符串 "None"，
若原样进树，verify 会判「缺 trace_id/session_id」并让整棵树失败。
约定：session_id 不可信时退用 trace_id，并在 stats/账本里记 session_filled。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CASES = [
    # (session_meta.session_id, trace_id, 期望落库的 session_id, 期望是否计入 session_filled)
    ("None", "t_none", "t_none", 1),
    ("", "t_empty", "t_empty", 1),
    (None, "t_null", "t_null", 1),
    ("s_real", "t_real", "s_real", 0),
]


def attr(sid, tid):
    rec = {
        "session_meta": {"session_id": sid, "request_time": "2026-11-11 05:21:36"},
        "turns": [{
            "meta": {"trace_id": tid, "task": "数据查询类"},
            "problem": [{"problem_id": "p1", "component": "finquery",
                         "label": "SQL 执行报错返回零行",
                         "definition": "SQL 执行报错返回零行 的具体定义",
                         "reason": "证据显示如此"}],
        }],
        "session_problem": "[]",
    }
    if sid is None:
        rec["session_meta"].pop("session_id")
    return rec


def main():
    tmp = tempfile.mkdtemp(prefix="sessfill-")
    src = os.path.join(tmp, "attr.jsonl")
    with open(src, "w", encoding="utf-8") as fh:
        for sid, tid, _, _ in CASES:
            fh.write(json.dumps(attr(sid, tid), ensure_ascii=False) + "\n")
    out = os.path.join(tmp, "run")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "cluster.py"), "ingest",
                        "--input", src, "--out", out, "--channel", "aime"],
                       capture_output=True, text=True)
    fails = []
    if r.returncode != 0:
        fails.append(f"ingest 退出码 {r.returncode}: {r.stderr.strip()[:300]}")
    else:
        stats = json.load(open(os.path.join(out, "stats.json"), encoding="utf-8"))
        items = [json.loads(l) for l in open(os.path.join(out, "items.jsonl"), encoding="utf-8")]
        by_tid = {it["trace_id"]: it for it in items}
        want_fill = sum(1 for _, _, _, f in CASES if f)
        if stats.get("session_filled") != want_fill:
            fails.append(f"session_filled 期望 {want_fill}，实际 {stats.get('session_filled')}")
        for _, tid, want_sid, _ in CASES:
            got = (by_tid.get(tid) or {}).get("session_id")
            if got != want_sid:
                fails.append(f"{tid}: session_id 期望 {want_sid!r}，实际 {got!r}")
        # 兜底后每个条目都应可追溯，verify 不该再因缺 session_id 报错
        if any(not it["session_id"] for it in items):
            fails.append("仍有条目 session_id 为空")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print(f"SESSION-FILL OK （{len(CASES)}/{len(CASES)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
