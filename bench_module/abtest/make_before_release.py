#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按动态 bench 的档位，装配一次「发版前」AB 测试口径（02_before_release）。

做的事只有切库，不重跑任何东西：

    bench tier_1000.jsonl ─┬─→ 01_targets/questions.jsonl           目标 trace 清单
    （1000 条 trace_id）    ├─→ 02_before_release/inputs/<date>.jsonl  eval_input 原行，逐字节不改
                          ├─→ 02_before_release/attribution/…        归因原行，逐字节不改
                          └─→ 02_before_release/cluster_tree/…      全局树的 item_index 切片
                                        │
                                        └─→ 00_manifest/ coverage / summary / scope_summary

为什么不用重跑：进 bench 的 trace 本来就来自归因产出，且建池时要求「每条 problem 都能
join 到分类法叶子」才允许进池，所以这 1000 条的归因和聚类落点必然齐全。

用法（在目标目录的 99_provenance 下跑）：

    python3 make_before_release.py \
        --bench-dir /mnt/aime_eval_ssd/3_dynamic_bench \
        --eval-input /mnt/aime_eval_ssd/1_aime/eval_input \
        --eval-output /mnt/aime_eval_ssd/1_aime/eval_output \
        --taxonomy /mnt/aime_eval_ssd/code/2_cluster_algorithm/taxonomy/inventory.json \
        --out /mnt/aime_eval_ssd/1_aime/ab_test/2_version1119_test \
        --model aime_expert --tier 1000

只读上游；只写 --out。stdlib，无第三方依赖。
"""
import argparse
import collections
import csv
import hashlib
import json
import os
import shutil
import sys
import time

SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "GOLD": 5}

# 目标清单的列（前 21 列沿用 1106/1112 的既有格式，后 6 列是本轮 bench 口径）
COLUMNS = ["trace_id", "session_id", "user_id", "question_time", "mode", "question",
           "qtype", "status", "b_reason", "eval_category", "eval_store", "eval_file",
           "eval_line", "eval_n_problem", "eval_is_correct", "cls_n_items",
           "cls_paths", "cls_labels", "cls_dates", "eval_stores_alt", "recycle",
           "severity", "turn", "session_round", "tier", "bench_rank", "question_full",
           "leaf_vintage", "input_file", "input_model"]


# ---------------------------------------------------------------- 小工具

def now():
    """北京时间（服务器是 UTC，产物统一按 +08 打时间）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 8 * 3600)) + "+0800"


def jdump(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def jload(path, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_csv(path, cols, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    os.replace(tmp, path)


def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def md_mkdir(path):
    os.makedirs(path, exist_ok=True)
    return path


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 切库

def load_tier(path):
    rows = list(read_jsonl(path))
    rows.sort(key=lambda r: (r.get("bench") or {}).get("rank", 0))
    return rows


def load_locator(path):
    """trace_id → (relpath, offset, nbytes)。"""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) == 4:
                out[p[0]] = (p[1], int(p[2]), int(p[3]))
    return out


def _trace_of(raw):
    rec = json.loads(raw.decode("utf-8"))
    traces = rec.get("traces") or []
    return ((traces[0] or {}).get("meta") or {}).get("trace_id") if traces else None


def _rescan(path, want):
    """定位表过期时兜底：整文件扫一遍，建 trace_id → (offset, nbytes)。"""
    got = {}
    with open(path, "rb") as fh:
        while True:
            off = fh.tell()
            line = fh.readline()
            if not line:
                break
            if b'"trace_id"' not in line:
                continue
            try:
                tid = _trace_of(line)
            except Exception:
                continue
            if tid in want:
                got[tid] = (off, len(line))
    return got


def copy_input_lines(rows, loc, eval_input_root, dst_dir):
    """把 eval_input 原行按日期文件切出来（不改一个字节）。

    两个坑：
      * 同一日期可能来自多个源目录（如 expert / fast 各一份同名文件），按文件名合并写出；
      * 定位表可能在上游重写文件后过期，所以每条回验 trace_id，一旦对不上就整扫该文件重建偏移。
    返回 trace_id → (源文件相对路径, 该行的 model 字段)。
    """
    by_file = collections.defaultdict(list)
    for r in rows:
        tid = r["trace_id"]
        hit = loc.get(tid)
        if not hit:
            raise SystemExit("定位表缺 trace_id: %s" % tid)
        by_file[hit[0]].append((hit[1], hit[2], tid))

    by_name = collections.defaultdict(list)
    for rel, entries in by_file.items():
        by_name[os.path.basename(rel)].append((rel, entries))

    written = {}
    os.makedirs(dst_dir, exist_ok=True)
    for name in sorted(by_name):
        sources = by_name[name]
        rebuilt = []
        for rel, entries in sorted(sources):
            rebuilt.append((rel, entries, _rebuild_if_stale(rel, entries, eval_input_root)))
        with open(os.path.join(dst_dir, name), "wb") as out:
            for rel, entries, fresh in rebuilt:
                entries = fresh or entries
                with open(os.path.join(eval_input_root, rel), "rb") as rf:
                    for off, n, tid in entries:
                        rf.seek(off)
                        raw = rf.read(n).rstrip(b"\n")
                        rec = json.loads(raw.decode("utf-8"))
                        traces = rec.get("traces") or []
                        got = ((traces[0] or {}).get("meta") or {}).get("trace_id") if traces else None
                        if got != tid:
                            raise SystemExit("原文对不上：%s 期望 %s 实际 %s" % (rel, tid, got))
                        out.write(raw + b"\n")
                        written[tid] = (rel, ((traces[0] or {}).get("meta") or {}).get("model") or "")
    return written


def _rebuild_if_stale(rel, entries, eval_input_root):
    """定位表与文件不一致时整扫一遍，返回重建后的 [(offset, nbytes, trace_id)]。"""
    src = os.path.join(eval_input_root, rel)
    good = True
    with open(src, "rb") as rf:
        for off, n, tid in entries:
            rf.seek(off)
            try:
                if _trace_of(rf.read(n).rstrip(b"\n")) != tid:
                    good = False
                    break
            except Exception:
                good = False
                break
    if good:
        return None
    log("  定位表过期，整扫 %s" % os.path.basename(rel))
    want = {tid for _o, _n, tid in entries}
    fresh = _rescan(src, want)
    miss = want - set(fresh)
    if miss:
        raise SystemExit("%s 里找不到 %d 条：%s" % (src, len(miss), list(miss)[:3]))
    return sorted((off, n, tid) for tid, (off, n) in fresh.items())


def copy_attribution(rows, pool, eval_output_root, dst_dir):
    """覆盖这些 trace 的归因行原样拷出，按「归因源目录/文件名」分桶。"""
    need = collections.defaultdict(set)
    for r in rows:
        tid = r["trace_id"]
        rec = pool.get(tid)
        if not rec:
            raise SystemExit("主池缺 trace_id: %s" % tid)
        need[rec["attr_file"]].add(tid)

    info = {}
    for rel in sorted(need):
        want = need[rel]
        group = rel.split("/")[0]
        sub = os.path.join(dst_dir, group)
        md_mkdir(sub)
        out = open(os.path.join(sub, os.path.basename(rel)), "w", encoding="utf-8")
        try:
            with open(os.path.join(eval_output_root, rel), encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if '"trace_id"' not in line and "\"turns\"" not in line:
                        continue
                    rec = json.loads(line)
                    turns = rec.get("turns") or []
                    tids = {(t.get("meta") or {}).get("trace_id") for t in turns}
                    hit = tids & want
                    if not hit:
                        continue
                    out.write(line if line.endswith("\n") else line + "\n")
                    sm = rec.get("session_meta") or {}
                    for t in turns:
                        tid = (t.get("meta") or {}).get("trace_id")
                        if tid in hit:
                            info[tid] = {
                                "file": rel,
                                "line": lineno,
                                "user_id": sm.get("user_id"),
                                "n_problem": len(t.get("problem") or []),
                                "is_correct": t.get("is_correct"),
                                "severities": [(p.get("severity") or "") for p in
                                               (t.get("problem") or []) if isinstance(p, dict)],
                            }
        finally:
            out.close()
    missing = {r["trace_id"] for r in rows} - set(info)
    if missing:
        raise SystemExit("归因缺 %d 条，例：%s" % (len(missing), list(missing)[:3]))
    return info


def slice_item_index(global_index, ids, info, dst_path):
    """全局 item_index → 本批条目，并按归因对齐。

    对齐三条：
      1. 只留 problem_index < 归因条数 的行（树里常有同一 trace 被多次评测留下的尾巴）；
      2. severity 以本次归因为准，树里那次标的值原样留在 severity_at_ingest；
      3. 行的 src_rel 与本次归因文件不一致时记 leaf_vintage=other（叶子描述的是另一次评测）。
    """
    keep = []
    dropped = 0
    with open(global_index, encoding="utf-8") as fh:
        for line in fh:
            if '"trace_id"' not in line:
                continue
            rec = json.loads(line)
            tid = rec.get("trace_id")
            if tid not in ids:
                continue
            meta = info[tid]
            try:
                pi = int(rec.get("problem_index") or 0)
            except (TypeError, ValueError):
                pi = -1
            if pi < 0 or pi >= meta["n_problem"]:
                dropped += 1
                continue
            at_ingest = rec.get("severity") or ""
            rec["severity_at_ingest"] = at_ingest
            rec["severity"] = meta["severities"][pi]
            rec["leaf_vintage"] = ("same" if
                                   (rec.get("src_rel") or "").split("/")[-1]
                                   == meta["file"].split("/")[-1] else "other")
            keep.append(rec)
    keep.sort(key=lambda r: (r.get("case_id") or "", int(r.get("problem_index") or 0)))
    with open(dst_path, "w", encoding="utf-8") as fh:
        for rec in keep:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return keep, dropped


# ---------------------------------------------------------------- 装配

def build(args):
    out = os.path.abspath(args.out)
    t0 = time.time()

    tier_path = os.path.join(args.bench_dir, args.model, "tier_%d.jsonl" % args.tier)
    pool_path = os.path.join(args.bench_dir, "pool", "%s.jsonl" % args.model)
    loc_path = os.path.join(args.bench_dir, "_build", "trace_loc.tsv")
    global_index = os.path.join(args.eval_output, "cluster", args.model, "item_index.jsonl")

    log("1/7 读目标清单 %s" % tier_path)
    rows = load_tier(tier_path)
    ids = [r["trace_id"] for r in rows]
    idset = set(ids)
    log("  %d 条 trace；档位 %d" % (len(rows), args.tier))

    log("2/7 读主池取归因出处")
    pool = {}
    for rec in read_jsonl(pool_path):
        if rec.get("trace_id") in idset:
            pool[rec["trace_id"]] = rec
    log("  命中 %d 条" % len(pool))

    log("3/7 切 eval_input 原文 → inputs/%s/" % args.model)
    loc = load_locator(loc_path)
    inputs_dir = md_mkdir(os.path.join(out, "02_before_release", "inputs", args.model))
    src_of = copy_input_lines(rows, loc, args.eval_input, inputs_dir)
    dates = collections.Counter(os.path.basename(v[0])[:10] for v in src_of.values())
    model_c = collections.Counter(v[1] for v in src_of.values())
    off_model = [tid for tid, v in src_of.items() if args.model == "aime_expert"
                 and v[1] and "fast" in v[1].lower()]
    log("  %d 行，%d 个日期文件；输入里的模型：%s"
        % (len(src_of), len(dates), dict(model_c)))
    if off_model:
        log("  注意：%d 条的原文落在另一模型的 eval_input 里（按探测到的 model 字段判定）" % len(off_model))

    log("4/7 切归因原行 → attribution/")
    attr_dir = md_mkdir(os.path.join(out, "02_before_release", "attribution"))
    ainfo = copy_attribution(rows, pool, args.eval_output, attr_dir)
    log("  %d 行，%d 个源文件" % (len(ainfo), len({v["file"] for v in ainfo.values()})))

    log("5/7 切聚类树 → cluster_tree/%s/item_index.slice.jsonl" % args.model)
    tax = {}
    if os.path.isfile(args.taxonomy):
        for node in jload(args.taxonomy, {}).get("nodes") or []:
            tax[node.get("id")] = node.get("name") or ""
    cdir = md_mkdir(os.path.join(out, "02_before_release", "cluster_tree", args.model))
    items, stale = slice_item_index(global_index, idset, ainfo,
                                    os.path.join(cdir, "item_index.slice.jsonl"))
    per_trace = collections.defaultdict(list)
    for rec in items:
        per_trace[rec["trace_id"]].append(rec)
    log("  %d 个 problem-叶子条目，覆盖 %d 条 trace（丢弃树里残留的旧评测尾巴 %d 行）"
        % (len(items), len(per_trace), stale))

    # ---- 校验：归因里的每条 problem 都要有切片落点，且叶子合法
    bad_count, bad_path, bad_index = [], [], []
    for tid in ids:
        meta = ainfo[tid]
        got = per_trace.get(tid, [])
        if len(got) != meta["n_problem"]:
            bad_count.append((tid, meta["n_problem"], len(got)))
        if {r.get("problem_index") for r in got} != set(range(meta["n_problem"])):
            bad_index.append(tid)
        for r in got:
            p = r.get("path") or ""
            if len(p.split(".")) != 3 or not p.startswith("S"):
                bad_path.append((tid, p))
    recon = sum(1 for r in items if r.get("severity_at_ingest") != r.get("severity"))
    vintage = {r["trace_id"] for r in items if r.get("leaf_vintage") == "other"}
    log("  校验：条数不符 %d / problem_index 缺号 %d / 非法叶子 %d / 严重度按归因校正 %d 行 / "
        "叶子来自另一次评测 %d 条 trace"
        % (len(bad_count), len(bad_index), len(bad_path), recon, len(vintage)))

    # ---- 逐条清单
    log("6/7 写清单与统计")
    vintage_of = {}
    for tid, rs in per_trace.items():
        vintage_of[tid] = "other" if any(x.get("leaf_vintage") == "other" for x in rs) else "same"
    status_of = {}
    questions = []
    for rank, r in enumerate(rows):
        tid = r["trace_id"]
        meta = ainfo[tid]
        got = sorted(per_trace.get(tid, []), key=lambda x: int(x.get("problem_index") or 0))
        sev = r.get("severity") or "GOLD"
        if meta["n_problem"] == 0:
            status, breason = "已评测_无落点", "判无问题"
        elif len(got) == meta["n_problem"]:
            status, breason = "已评测_有落点", ""
        else:
            status, breason = "已评测_无落点", "有问题未进树"
        status_of[tid] = status
        group = meta["file"].split("/")[0]
        questions.append({
            "trace_id": tid,
            "session_id": r.get("session_id") or "",
            "user_id": meta["user_id"] if meta["user_id"] is not None else "",
            "question_time": r.get("request_time") or "",
            "mode": "expert" if args.model == "aime_expert" else "fast",
            "question": r.get("question") or "",
            "qtype": "string",
            "status": status,
            "b_reason": breason,
            "eval_category": "daily" if group.startswith("0_daily") else "task",
            "eval_store": ("daily_" if group.startswith("0_daily") else "task_")
                           + args.model.split("_", 1)[-1],
            "eval_file": "eval_output/" + meta["file"],
            "eval_line": meta["line"],
            "eval_n_problem": meta["n_problem"],
            "eval_is_correct": "" if meta["is_correct"] is None else meta["is_correct"],
            "cls_n_items": len(got),
            "cls_paths": ";".join(x.get("path") or "" for x in got),
            "cls_labels": ";".join(tax.get(x.get("path") or "", "") for x in got),
            "cls_dates": ";".join(sorted({str(x.get("batch_id") or "") for x in got})),
            "eval_stores_alt": "",
            "recycle": "",
            "severity": sev,
            "turn": r.get("turn") or "",
            "session_round": r.get("session_round") or 0,
            "tier": args.tier,
            "bench_rank": rank,
            "question_full": r.get("question_full") or "",
            "leaf_vintage": vintage_of.get(tid, ""),
            "input_file": src_of[tid][0],
            "input_model": src_of[tid][1],
        })

    targets_dir = md_mkdir(os.path.join(out, "01_targets"))
    qpath = os.path.join(targets_dir, "questions.jsonl")
    with open(qpath, "w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")

    man_dir = md_mkdir(os.path.join(out, "00_manifest"))
    write_csv(os.path.join(man_dir, "coverage_before_release.csv"), COLUMNS, questions)
    write_csv(os.path.join(man_dir, "coverage_before_release_cluster.csv"), COLUMNS, questions)
    write_csv(os.path.join(man_dir, "missing_without_eval.csv"),
              ["trace_id", "reason"],
              [{"trace_id": t, "reason": s} for t, s in status_of.items() if s == "无评测记录"])

    # ---- 切片侧统计
    leaves = collections.Counter()
    leaf_items = collections.Counter()
    l1l2 = collections.Counter()
    edges = 0
    for rec in items:
        p = rec.get("path") or ""
        parts = p.split(".")
        tid = rec["trace_id"]
        leaves[(tid, p)] += 1
        leaf_items[p] += 1
        l1l2[(parts[0], parts[1] if len(parts) > 1 else "")] += 1
        if rec.get("edge"):
            edges += 1
    leaf_traces = collections.Counter()
    for (tid, p) in leaves:
        leaf_traces[p] += 1

    write_csv(os.path.join(cdir, "batch_leaf_counts.csv"),
              ["leaf", "leaf_name", "n_traces", "n_items", "share_traces"],
              [{"leaf": p, "leaf_name": tax.get(p, ""), "n_traces": n,
                "n_items": leaf_items[p], "share_traces": round(n / len(ids), 4)}
               for p, n in leaf_traces.most_common()])
    write_csv(os.path.join(cdir, "batch_l1l2_counts.csv"),
              ["l1", "l2", "n_items", "n_traces"],
              [{"l1": a, "l2": b, "n_items": n,
                "n_traces": len({t for (t, p) in leaves if p.split(".")[0] == a
                                 and p.split(".")[1] == b})}
               for (a, b), n in l1l2.most_common()])

    # ---- 全局树快照（对照用，不作本批口径）
    src_tree = os.path.join(args.eval_output, "cluster", args.model)
    copied = []
    for fn in ("cluster_tree.json", "cluster_tree.txt", "cluster_summary.md",
               "cluster_brief.md", "cluster_matrix.png", "leaves.tsv",
               "syndrome.json", "syndrome.md", "verify.json"):
        s = os.path.join(src_tree, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(cdir, fn))
            copied.append(fn)
    if os.path.isfile(os.path.join(src_tree, "MANIFEST.json")):
        shutil.copy2(os.path.join(src_tree, "MANIFEST.json"),
                     os.path.join(cdir, "MANIFEST.global.json"))
        copied.append("MANIFEST.global.json")

    global_manifest = jload(os.path.join(src_tree, "MANIFEST.json"), {}) or {}
    manifest = {
        "bundle": "发版前口径（bench 档位切片）",
        "generated_at": now(),
        "model": args.model,
        "bench": {"dir": os.path.basename(args.bench_dir.rstrip("/")),
                  "tier": args.tier, "traces": len(ids)},
        "dataset": {
            "total_items": len(items),
            "total_traces": len(per_trace),
            "total_sessions": len({q["session_id"] for q in questions}),
            "total_cases": len(ids),
        },
        "tree": global_manifest.get("tree") or {},
        "quality": {"edge_flagged": edges,
                    "edge_rate": round(edges / len(items), 4) if items else 0.0,
                    "stale_rows_dropped": stale,
                    "severity_reconciled": recon,
                    "traces_leaf_vintage_other": len(vintage)},
        "source": {
            "global_index": "eval_output/cluster/%s/item_index.jsonl" % args.model,
            "global_generated_at": global_manifest.get("generated_at", ""),
            "taxonomy": (global_manifest.get("pipeline_version") or {}).get("taxonomy", ""),
            "pipeline_version": (global_manifest.get("pipeline_version") or {}).get("version", ""),
        },
        "files": [{"name": f, "bytes": os.path.getsize(os.path.join(cdir, f)),
                   "sha256_16": sha16(os.path.join(cdir, f))}
                  for f in sorted(os.listdir(cdir))
                  if os.path.isfile(os.path.join(cdir, f)) and f != "MANIFEST.json"],
        "notes": [
            "item_index.slice.jsonl 是本批（%d 条 trace）的唯一口径，叶子计数以 batch_*.csv 为准。" % len(ids),
            "cluster_tree.* / leaves.tsv / syndrome.* 是同一时刻的全局树快照，只作对照，计数不是本批的。",
            "本批条目由全局 item_index 按 trace_id 切出，未重跑分类，故与全局树叶子 id 完全一致。",
            "只保留 problem_index < 归因条数的行：树里同一 trace 被多次评测时会留旧尾巴，共丢弃 %d 行。" % stale,
            "severity 以本次归因为准；树里写入时的值留在 severity_at_ingest（相异 %d 行）。" % recon,
            "leaf_vintage=other 表示该叶子出自同一 trace 的另一次评测（%d 条 trace），叶子语义可信但未必对应当前这条 problem。" % len(vintage),
        ],
    }
    jdump(manifest, os.path.join(cdir, "MANIFEST.json"))

    # ---- 00_manifest 总账
    sev_c = collections.Counter(q["severity"] for q in questions)
    turn_c = collections.Counter(q["turn"] for q in questions)
    date_c = collections.Counter(q["eval_file"].split("attribution-")[-1][:10] for q in questions)
    status_c = collections.Counter(q["status"] for q in questions)
    breason_c = collections.Counter(q["b_reason"] for q in questions)
    files_sha = {}
    for root, _dirs, fns in os.walk(out):
        for fn in fns:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, out).replace("\\", "/")
            files_sha[rel] = {"bytes": os.path.getsize(p), "sha256_16": sha16(p)}

    summary = {
        "generated_at": now(),
        "target_traces": len(ids),
        "model": args.model,
        "tier": args.tier,
        "status": dict(status_c),
        "b_reason": {k: v for k, v in breason_c.items() if k},
        "eval_category": dict(collections.Counter(q["eval_category"] for q in questions)),
        "bench": {
            "tier": args.tier,
            "seed": (rows[0].get("bench") or {}).get("seed"),
            "pool_size": (rows[0].get("bench") or {}).get("pool_size"),
            "by_severity": dict(sev_c),
            "by_turn": dict(turn_c),
            "eval_batches": len(date_c),
            "eval_batch_range": [min(date_c), max(date_c)] if date_c else [],
        },
        "cluster": {"n_items": len(items), "n_traces_with_items": len(per_trace),
                    "leaves_hit": len(leaf_traces), "edge_flagged": edges,
                    "stale_rows_dropped": stale, "severity_reconciled": recon,
                    "traces_leaf_vintage_other": len(vintage)},
        "leaf_top20": [[p, n] for p, n in leaf_traces.most_common(20)],
        "checks": {
            "inputs": len(src_of),
            "attribution": len(ainfo),
            "count_mismatch": len(bad_count),
            "index_gap": len(bad_index),
            "invalid_leaf": len(bad_path),
            "stale_rows_dropped": stale,
            "severity_reconciled": recon,
            "traces_leaf_vintage_other": len(vintage),
            "input_model": dict(model_c),
            "traces_input_other_model": len(off_model),
        },
        "layout": "三个口径：02_before_release / 03_after_release_retest / 04_after_release_online_real",
        "files": files_sha,
    }
    jdump(summary, os.path.join(man_dir, "summary.json"))

    scope = {
        "generated_at": now(),
        "note": "三个口径，每个 = 「评测输出（归因）+ 聚类」；聚类沿用同一套 taxonomy 与同一棵全局树。",
        "scopes": {
            "1_before_release": {
                "title": "发版前（历史线上归因切片）",
                "dirs": ["02_before_release/attribution", "02_before_release/cluster_tree"],
                "detail_file": "coverage_before_release.csv",
                "target_traces": len(ids),
                "evaluated": len(ainfo),
                "not_evaluable": 0,
                "coverage": {
                    "有落点": status_c.get("已评测_有落点", 0),
                    "无落点_判无问题": status_c.get("已评测_无落点", 0) - breason_c.get("有问题未进树", 0),
                    "无落点_未进树": breason_c.get("有问题未进树", 0),
                    "无评测": status_c.get("无评测记录", 0),
                },
                "bench": {"tier": args.tier, "by_severity": dict(sev_c), "by_turn": dict(turn_c)},
                "note": "bench %d 档按 trace_id 切片；归因为各条 trace 生产当时的线上评测结果，非同一版本重跑。" % args.tier,
                "eval_output": "02_before_release/attribution",
            },
            "2_after_retest": {
                "title": "发版后重测",
                "dirs": ["03_after_release_retest/attribution", "03_after_release_retest/cluster_tree"],
                "detail_file": None,
                "target_traces": len(ids),
                "note": "待重推理 + 重评测；输入沿用 01_targets 的同一批 trace。",
                "eval_output": "03_after_release_retest/attribution",
            },
            "3_after_online": {
                "title": "发版后线上真实",
                "dirs": ["04_after_release_online_real/attribution",
                         "04_after_release_online_real/cluster_tree"],
                "detail_file": None,
                "records": 0,
                "note": "线上真实数据落盘后按同结构放入。",
                "eval_output": "04_after_release_online_real/attribution",
            },
        },
    }
    jdump(scope, os.path.join(man_dir, "scope_summary.json"))

    # ---- 05_compare 基线
    cmp_dir = md_mkdir(os.path.join(out, "05_compare"))
    write_csv(os.path.join(cmp_dir, "baseline_leaf_counts.csv"),
              ["leaf", "leaf_name", "n_traces", "n_items", "share"],
              [{"leaf": p, "leaf_name": tax.get(p, ""), "n_traces": n, "n_items": leaf_items[p],
                "share": round(n / len(ids), 4)} for p, n in leaf_traces.most_common()])
    write_csv(os.path.join(cmp_dir, "baseline_trace_level.csv"),
              ["group", "n_traces"],
              [{"group": k, "n_traces": v} for k, v in status_c.most_common()])
    write_csv(os.path.join(cmp_dir, "baseline_severity.csv"),
              ["severity", "n_traces", "share"],
              [{"severity": k, "n_traces": v, "share": round(v / len(ids), 4)}
               for k, v in sorted(sev_c.items(), key=lambda kv: SEV_ORDER.get(kv[0], 9))])

    log("7/7 写 README / 占位目录")
    write_readme(out, args, rows, questions, summary, leaf_traces, tax)
    for sub, title in (("03_after_release_retest", "发版后重测"),
                       ("04_after_release_online_real", "发版后线上真实")):
        for leaf in ("attribution", "cluster_tree"):
            d = md_mkdir(os.path.join(out, sub, leaf))
            p = os.path.join(d, "README.md")
            if not os.path.isfile(p):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("# %s · %s\n\n待落盘。结构与 `02_before_release/%s/` 一致，"
                             "按 trace_id 对齐 `01_targets/questions.jsonl`。\n" % (title, leaf, leaf))
    jdump({"released_at": args.released_at, "label": args.label,
           "title": "AIME v%s 发版前后对比" % args.label.split(" ")[0]},
          os.path.join(out, "round.json"))

    write_run_md(out, args)
    log("完成，用时 %.1fs → %s" % (time.time() - t0, out))
    return summary


def write_run_md(out, args):
    """复算命令，落 99_provenance/RUN.md。"""
    cmd = ("python3 make_before_release.py \\\n"
           "    --bench-dir %s \\\n"
           "    --eval-input %s \\\n"
           "    --eval-output %s \\\n"
           "    --taxonomy %s \\\n"
           "    --out %s \\\n"
           "    --model %s --tier %d"
           % (args.bench_dir, args.eval_input, args.eval_output, args.taxonomy,
              args.out, args.model, args.tier))
    txt = """# 复算

只切库，不重跑归因、不重跑聚类。上游全部只读。

```bash
cd 99_provenance

%s
```

产物：`01_targets/` + `02_before_release/{inputs,attribution,cluster_tree}` +
`00_manifest/` + `05_compare/` + 根 README。

重跑前先删掉 `00_manifest/ 01_targets/ 02_before_release/ 03_after_release_retest/
04_after_release_online_real/ 05_compare/ README.md round.json`（脚本只覆盖同名文件、
不清理残留）。`run.log` 追加写。换档位只改 `--tier`，但 `01_targets` 的 trace 集合
会跟着变，已落盘的 03 口径要重新对齐。
""" % cmd
    os.makedirs(os.path.join(out, "99_provenance"), exist_ok=True)
    with open(os.path.join(out, "99_provenance", "RUN.md"), "w", encoding="utf-8") as fh:
        fh.write(txt)


def write_readme(out, args, rows, questions, summary, leaf_traces, tax):
    sev = summary["bench"]["by_severity"]
    turn = summary["bench"]["by_turn"]
    st = summary["status"]
    ck = summary["checks"]
    top = leaf_traces.most_common(10)
    lines = []
    a = lines.append
    a("# 2_version1119_test — AB 测试（三个口径）")
    a("")
    a("> 目标 trace：**%d** 条，全部 `%s`，按动态 bench 的 **%d 档**规则抽。"
      % (len(rows), args.model, args.tier))
    a("> 三个口径，每个 = 「评测输出（归因）+ 聚类」；聚类沿用同一套 taxonomy、同一棵全局树。")
    a("")
    a("生成时间：%s" % summary["generated_at"])
    a("")
    a("| 口径 | 归因 | 聚类 | 现状 |")
    a("|---|---|---|---|")
    a("| ① 发版前 | `02_before_release/attribution/` | `02_before_release/cluster_tree/` | "
      "%d 条有落点、%d 条判无问题 |"
      % (st.get("已评测_有落点", 0), st.get("已评测_无落点", 0)))
    a("| ② 发版后重测 | `03_after_release_retest/attribution/` | `03_after_release_retest/cluster_tree/` | 待落 |")
    a("| ③ 线上真实 | `04_after_release_online_real/attribution/` | `04_after_release_online_real/cluster_tree/` | 待落 |")
    a("")
    a("## 采样口径（动态 bench %d 档）" % args.tier)
    a("")
    a("| 项 | 值 |")
    a("|---|---|")
    a("| 来源 | `/mnt/aime_eval_ssd/3_dynamic_bench/%s/tier_%d.jsonl` |" % (args.model, args.tier))
    a("| seed | `%s` |" % summary["bench"].get("seed"))
    a("| 严重度 | " + "、".join("%s %d" % (k, v) for k, v in
                                sorted(sev.items(), key=lambda kv: SEV_ORDER.get(kv[0], 9))) + " |")
    a("| 轮次 | 单轮 %d / 多轮 %d |" % (turn.get("single", 0), turn.get("multi", 0)))
    a("| 评测批次 | %d 个（%s ~ %s） |" % (summary["bench"]["eval_batches"],
                                          summary["bench"]["eval_batch_range"][0],
                                          summary["bench"]["eval_batch_range"][1]))
    a("")
    a("配额规则（详见 `3_dynamic_bench/README.md`）：金标 20%；P0 上限 25%；"
      "其余按 P1:P2:P3:P4 = 4:3:2:1 分；同一严重度内各 L2 机制尽量均摊；单轮:多轮 = 75:25。")
    a("`GOLD` = 归因未报出任何问题（判定通过），不是人工复核过的金标。")
    a("")
    a("## 目录")
    a("")
    a("```")
    a("00_manifest/")
    a("  summary.json                       总账（状态、叶子 Top20、校验、全文件 sha256）")
    a("  scope_summary.json                 三个口径的总账")
    a("  coverage_before_release.csv        口径① 逐条（评测层）")
    a("  coverage_before_release_cluster.csv  口径① 逐条（聚类落点后）")
    a("  missing_without_eval.csv           无法评测明细（本次为空）")
    a("01_targets/questions.jsonl           %d 条目标 trace（trace/user/问句/状态/叶子）" % len(rows))
    a("02_before_release/")
    a("  inputs/%s/<date>.jsonl  eval_input 原行，逐字节不改" % args.model)
    a("  attribution/<组>/<源文件名>  归因原行，逐字节不改")
    a("  cluster_tree/%s/             本批 item_index 切片 + 全局树快照" % args.model)
    a("03_after_release_retest/            口径②（待落）")
    a("04_after_release_online_real/       口径③（待落）")
    a("05_compare/                         基线（叶子分布 / 逐条状态 / 严重度）")
    a("99_provenance/                      装配脚本与运行命令")
    a("```")
    a("")
    a("## 校验")
    a("")
    a("| 项 | 结果 |")
    a("|---|---|")
    a("| 评测输入 | %d 行（%d 个日期文件） |" % (ck["inputs"],
                                              summary["bench"]["eval_batches"]))
    a("| 归因 | %d 行（原样拷贝） |" % ck["attribution"])
    a("| 聚类条目 | %d 个 problem-叶子 |" % summary["cluster"]["n_items"])
    a("| 归因条数 vs 切片条数 不符 | %d |" % ck["count_mismatch"])
    a("| problem_index 缺号 | %d |" % ck["index_gap"])
    a("| 非法叶子 | %d |" % ck["invalid_leaf"])
    a("| 丢弃树里残留的旧评测尾巴 | %d 行 |" % ck["stale_rows_dropped"])
    a("| 严重度按归因校正 | %d 行 |" % ck["severity_reconciled"])
    a("| 叶子出自另一次评测的 trace | %d 条 |" % ck["traces_leaf_vintage_other"])
    a("| 原文落在另一模型 eval_input 的 trace | %d 条 |" % ck["traces_input_other_model"])
    a("")
    a("## 三条要知道的偏差")
    a("")
    a("**① 树里有旧尾巴。** 同一个 `trace_id` 可能在多个日期的归因里出现（重评/回测），聚类树按"
      "「历史条目不重分类」只追加不改写，于是 `item_index` 里会留下超出本次归因条数的行。"
      "本包按 `problem_index < 归因条数` 截断，丢了 %d 行；截断后归因条数与切片条数"
      "%s。" % (ck["stale_rows_dropped"], "完全一致" if ck["count_mismatch"] == 0 else "仍有 %d 条不一致" % ck["count_mismatch"]))
    a("")
    a("**② 少数 trace 的叶子来自另一次评测。** %d 条 trace 的叶子行 `src_rel` 指向的不是本次归因文件，"
      "即这条 `problem` 被聚类的叶子其实来自同一 trace 的另一次评测（部分源文件已被上游清理，"
      "无法复原）。这些条目的 `leaf_vintage=other`，看叶子分布时应单独剔除或标注；"
      "**严重度一律以本次归因为准**（树里写入时的值留在 `severity_at_ingest`）。"
      % ck["traces_leaf_vintage_other"])
    a("")
    a("**③ 原文被挪到另一模型目录。** %d 条 trace 的原文只能在 `eval_input/…/%s/` 下找到"
      "（这些行 `meta.model` 是另一模型的标识），按 `trace_id` 照样能定位、内容未改。"
      "`input_model` 列记了探测到的模型，`input_file` 记了出处。做「模型维度」比较时要注意这批。"
      % (ck["traces_input_other_model"], "aime_fast" if args.model == "aime_expert" else "aime_expert"))
    a("")
    a("## 落点 Top 10")
    a("")
    a("| 叶子 | 名称 | trace 数 |")
    a("|---|---|---:|")
    for leaf, n in top:
        a("| %s | %s | %d |" % (leaf, tax.get(leaf, ""), n))
    a("")
    a("## 对号（踩过的坑）")
    a("")
    a("1. 一律按 **`trace_id`** 匹配，不用行号：归因/聚类/输入被上游重写过多次，行号会漂。")
    a("2. 归因一行 = 一个 turn = 一个 trace（本批已核，无一行多 turn）。")
    a("3. 多轮上下文已内联在 `traces[0].messages` 里，`len(messages)==meta.n_messages`；"
      "不需要额外拼接历史。")
    a("4. `02_before_release/inputs/` 的文件名是 **eval_input 的原始日期**，"
      "与本条归因所在批次（`batch_id`）可能差一天，不要拿它当匹配键。")
    a("")
    a("## 下一步")
    a("")
    a("1. 口径②：拿 `01_targets` 的同一批 trace 做重推理 + 重评测，归因落 "
      "`03_after_release_retest/attribution/`，再按同一棵全局树增量切片。")
    a("2. 口径③：线上真实批次落盘后按同结构放入 `04_after_release_online_real/`。")
    a("3. 对比：`05_compare/` 出配对表与 report（同一 trace 前后落点 + 严重度迁移）。")
    a("")
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="按 bench 档位装配发版前 AB 测试口径")
    ap.add_argument("--bench-dir", required=True)
    ap.add_argument("--eval-input", required=True)
    ap.add_argument("--eval-output", required=True)
    ap.add_argument("--taxonomy", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="aime_expert")
    ap.add_argument("--tier", type=int, default=1000)
    ap.add_argument("--label", default="1119 发版")
    ap.add_argument("--released-at", default="2026-11-19")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
