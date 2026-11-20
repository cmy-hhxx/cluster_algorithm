#!/usr/bin/env python3
"""prune_items.py 单测：items / locators / assign 三处必须同步删，且留备份与留痕。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def item(seq, cid, tid, sid):
    return {"seq": seq, "case_id": cid, "trace_id": tid, "session_id": sid,
            "date": "2026-11-11", "model": "aime_expert", "src_rel": "x.jsonl",
            "bad_case_id": "", "user_id": "u", "batch_id": "2026-11-11",
            "question": "q", "request_time": "2026-11-11 05:00:00"}


ITEMS = [
    item(1, "c_ok", "t_ok", "s_ok"),      # 正常
    item(2, "c_bad", "t_bad", "t_bad"),   # session_id==trace_id → 删
    item(3, "c_drop", "t_drop", "s_drop"),  # 在 --drop-ids 里 → 删
    item(4, "c_ok2", "t_ok2", "s_ok2"),   # 正常
]


def main():
    tmp = tempfile.mkdtemp(prefix="prune-")
    work = os.path.join(tmp, "run")
    os.makedirs(os.path.join(work, "assign"))
    with open(os.path.join(work, "items.jsonl"), "w", encoding="utf-8") as fh:
        for it in ITEMS:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    with open(os.path.join(work, "locators.jsonl"), "w", encoding="utf-8") as fh:
        for it in ITEMS:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    with open(os.path.join(work, "assign", "merged.out.jsonl"), "w", encoding="utf-8") as fh:
        for it in ITEMS:
            fh.write(json.dumps({"seq": it["seq"], "path": "S1.M1.1", "edge": False}) + "\n")
    ids = os.path.join(tmp, "drop.txt")
    open(ids, "w").write("c_drop\n")

    fails = []
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "prune_items.py"),
                        "--work", work, "--drop-ids", ids, "--self-session",
                        "--reason", "test"], capture_output=True, text=True)
    if r.returncode != 0:
        fails.append(f"退出码 {r.returncode}: {r.stderr.strip()[:300]}")
    else:
        items = [json.loads(l) for l in open(os.path.join(work, "items.jsonl"), encoding="utf-8")]
        locs = [json.loads(l) for l in open(os.path.join(work, "locators.jsonl"), encoding="utf-8")]
        asg = [json.loads(l) for l in open(os.path.join(work, "assign", "merged.out.jsonl"), encoding="utf-8")]
        if [i["seq"] for i in items] != [1, 4]:
            fails.append(f"items 应剩 [1,4]，实际 {[i['seq'] for i in items]}")
        if [i["seq"] for i in locs] != [1, 4]:
            fails.append(f"locators 应剩 [1,4]，实际 {[i['seq'] for i in locs]}")
        if [i["seq"] for i in asg] != [1, 4]:
            fails.append(f"assign 应剩 [1,4]，实际 {[i['seq'] for i in asg]}")
        bdirs = [d for d in os.listdir(work) if d.startswith(".prune-")]
        if len(bdirs) != 1:
            fails.append(f"应有一个备份目录，实际 {bdirs}")
        else:
            bk = os.path.join(work, bdirs[0], "items.jsonl")
            if sum(1 for _ in open(bk, encoding="utf-8")) != 4:
                fails.append("备份应保留原件 4 行")
        rec = json.loads(open(os.path.join(work, "prune.jsonl"), encoding="utf-8").read().strip())
        if rec.get("dropped_items") != 2:
            fails.append(f"prune.jsonl 应记 dropped_items=2，实际 {rec.get('dropped_items')}")
        if rec.get("left_items") != 2:
            fails.append(f"prune.jsonl 应记 left_items=2，实际 {rec.get('left_items')}")
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("PRUNE OK （1/1）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
