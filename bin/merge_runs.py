#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把多个按批次跑出来的 run 目录合并成一个（全局 seq + 去重）。

用法：
    python3 bin/merge_runs.py --out <merged_run> <batch_run1> <batch_run2> ...

每个 batch run 必须已经有完整的 assign/*.out.jsonl（即已经 classify 过）。
合并后写 items.jsonl / locators.jsonl / assign/merged.out.jsonl / stats.json。
去重键默认 (session_id, trace_id, problem_id)，重复条目保留最早批次的那条。
"""
import argparse, glob, json, os, sys


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dedup-key", default="session_id,trace_id,json_path")
    ap.add_argument("--allow-partial", action="store_true",
                    help="批次未分类完时，只合并已有归类的条目（默认报错）")
    ap.add_argument("batches", nargs="+")
    a = ap.parse_args()

    key_fields = [k.strip() for k in a.dedup_key.split(",") if k.strip()]
    seen, dropped, seq = {}, 0, 0
    items_out, locs_out, assign_out = [], [], []
    per_batch = []

    for b in a.batches:
        ipath = os.path.join(b, "items.jsonl")
        lpath = os.path.join(b, "locators.jsonl")
        if not os.path.exists(ipath):
            sys.exit(f"缺少 {ipath}")
        items = {r["seq"]: r for r in load_jsonl(ipath)}
        locs = {r["seq"]: r for r in load_jsonl(lpath)}
        assign = {}
        for f in sorted(glob.glob(os.path.join(b, "assign", "*.out.jsonl"))):
            for r in load_jsonl(f):
                assign[r["seq"]] = r
        missing = set(items) - set(assign)
        if missing and not a.allow_partial:
            sys.exit(f"{b}: {len(missing)} 条没有归类结果（先跑 run.sh classify，或加 --allow-partial）")
        if missing:
            print(f"  batch {b}: 跳过 {len(missing)} 条未归类条目")
        kept = 0
        for s in sorted(items):
            it = items[s]
            key = tuple(str(it.get(k, "")) for k in key_fields)
            if key in seen:
                dropped += 1
                continue
            seq += 1
            seen[key] = seq
            it = dict(it); it["seq"] = seq
            # locator 统一成绝对路径：相对路径按当前工作目录（仓库根）解析
            sp = it.get("src_path", "")
            if sp and not os.path.isabs(sp):
                sp = os.path.normpath(os.path.join(os.getcwd(), sp))
                it["src_path"] = sp
            if sp:
                it["file"] = sp
            items_out.append(it)
            loc = dict(locs[s]); loc["seq"] = seq
            if sp:
                loc["file"] = sp
            locs_out.append(loc)
            r = assign[s]
            assign_out.append({"seq": seq, "path": r["path"], "edge": bool(r.get("edge", False))})
            kept += 1
        per_batch.append((b, len(items), kept))

    os.makedirs(os.path.join(a.out, "assign"), exist_ok=True)
    with open(os.path.join(a.out, "items.jsonl"), "w", encoding="utf-8") as fh:
        for r in items_out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(a.out, "locators.jsonl"), "w", encoding="utf-8") as fh:
        for r in locs_out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(a.out, "assign", "merged.out.jsonl"), "w", encoding="utf-8") as fh:
        for r in assign_out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    stats = {
        "batches": [{"run": b, "items": n, "kept": k} for b, n, k in per_batch],
        "items": len(items_out), "deduped": dropped, "dedup_key": key_fields,
        "components": dict(sorted(
            {c: sum(1 for r in items_out if r["component"] == c)
             for c in {r["component"] for r in items_out}}.items(),
            key=lambda kv: -kv[1])),
        "sessions": len({r["session_id"] for r in items_out}),
        "traces": len({r["trace_id"] for r in items_out}),
    }
    json.dump(stats, open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for b, n, k in per_batch:
        print(f"  batch {b}: {n} items, kept {k}")
    print(f"merged: items={len(items_out)} deduped={dropped} sessions={stats['sessions']} -> {a.out}")


if __name__ == "__main__":
    main()
