#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把多个按批次跑出来的 run 目录合并成一个（全局 seq + 去重）。

用法：
    python3 bin/merge_runs.py --out <merged_run> <batch_run1> <batch_run2> ...

每个 batch run 必须已经有完整的 assign/*.out.jsonl（即已经 classify 过）。
合并后写 items.jsonl / locators.jsonl / assign/merged.out.jsonl / stats.json。
去重键默认 (trace_id, problem_index)，重复条目保留最早批次的那条。

2026-11-11 起身份只认 trace_id：eval_input 一行一 trace，同一 trace 在多个输入文件里有快照，
行号与绝对路径不可复现，因此 items/locators 里不再有 src_path/file/line/json_path，
去重也不能再用 json_path。
"""
import argparse, collections, glob, json, os, sys


def iter_jsonl(path):
    """流式读：baseline 一轮 25 万条 / 1 GB+，全量展开成 dict 会把 16 GiB 容器顶爆。"""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dedup-key", default="trace_id,problem_index")
    ap.add_argument("--allow-partial", action="store_true",
                    help="批次未分类完时，只合并已有归类的条目（默认报错）")
    ap.add_argument("batches", nargs="+")
    a = ap.parse_args()

    key_fields = [k.strip() for k in a.dedup_key.split(",") if k.strip()]
    seen = set()
    dropped = seq = 0
    comp_counter = collections.Counter()
    sessions, traces = set(), set()
    per_batch = []

    os.makedirs(os.path.join(a.out, "assign"), exist_ok=True)
    open(os.path.join(a.out, "locators.jsonl"), "w").close()   # 每批 append，先清空
    items_fh = open(os.path.join(a.out, "items.jsonl"), "w", encoding="utf-8")
    assign_fh = open(os.path.join(a.out, "assign", "merged.out.jsonl"), "w", encoding="utf-8")
    loc_map = {}          # 本批 old_seq -> 新 seq，用于第二遍流式写 locators
    try:
        for b in a.batches:
            ipath = os.path.join(b, "items.jsonl")
            lpath = os.path.join(b, "locators.jsonl")
            if not os.path.exists(ipath):
                sys.exit(f"缺少 {ipath}")
            # assign 只留 path/edge（不需要整行），streaming 读
            assign = {}
            for f in sorted(glob.glob(os.path.join(b, "assign", "*.out.jsonl"))):
                for r in iter_jsonl(f):
                    assign[r["seq"]] = (r["path"], bool(r.get("edge", False)))
            n_items, kept, n_missing = 0, 0, 0
            loc_map.clear()
            for it in iter_jsonl(ipath):
                n_items += 1
                old_seq = it["seq"]
                hit = assign.get(old_seq)
                if hit is None:
                    n_missing += 1
                    continue
                key = tuple(str(it.get(k, "")) for k in key_fields)
                if key in seen:
                    dropped += 1
                    continue
                seq += 1
                seen.add(key)
                it = dict(it); it["seq"] = seq
                items_fh.write(json.dumps(it, ensure_ascii=False) + "\n")
                assign_fh.write(json.dumps({"seq": seq, "path": hit[0], "edge": hit[1]},
                                           ensure_ascii=False) + "\n")
                loc_map[old_seq] = seq
                comp_counter[it["component"]] += 1
                sessions.add(it["session_id"])
                traces.add(it["trace_id"])
                kept += 1
            if n_missing and not a.allow_partial:
                sys.exit(f"{b}: {n_missing} 条没有归类结果（先跑 run.sh classify，或加 --allow-partial）")
            if n_missing:
                print(f"  batch {b}: 跳过 {n_missing} 条未归类条目")
            # locators 第二遍流式过滤（只留保留下来的 seq）
            with open(os.path.join(a.out, "locators.jsonl"), "a", encoding="utf-8") as lfh:
                for loc in iter_jsonl(lpath):
                    ns = loc_map.get(loc["seq"])
                    if ns is None:
                        continue
                    loc = dict(loc); loc["seq"] = ns
                    lfh.write(json.dumps(loc, ensure_ascii=False) + "\n")
            per_batch.append((b, n_items, kept))
    finally:
        items_fh.close()
        assign_fh.close()

    stats = {
        "batches": [{"run": b, "items": n, "kept": k} for b, n, k in per_batch],
        "items": seq, "deduped": dropped, "dedup_key": key_fields,
        "components": dict(comp_counter.most_common()),
        "sessions": len(sessions),
        "traces": len(traces),
    }
    json.dump(stats, open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for b, n, k in per_batch:
        print(f"  batch {b}: {n} items, kept {k}")
    print(f"merged: items={seq} deduped={dropped} sessions={stats['sessions']} -> {a.out}")


if __name__ == "__main__":
    main()
