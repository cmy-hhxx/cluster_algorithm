#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两批归因数据在同一套分类法下的分布对比。

用法：
    python3 bin/compare_batches.py \
        --before <out_dir_before> --after <out_dir_after> --out <compare_dir> \
        --sessions-before 628 --sessions-after 490 \
        --traces-before 1075 --traces-after 1601 \
        --label-before "修复前 09-17~10-14" --label-after "修复后 11-01~11-05" \
        --model aime_expert --png

两套口径都算，因为两批会话长度不同（每会话被评测的轮次数不一样）：
  * 轮次率 = 命中该节点的轮次数 / 该批全部轮次数（跨批次可比性更好，用作排序与判定）
  * 会话率 = 命中该节点的会话数 / 该批全部会话数（与仓库 README 的日常聚类口径对齐）
两套都是「同一节点内去重」；不同节点可重复命中，因此各节点率相加 > 100%。

显著性：Wilson 95% 区间 + 两比例 z 检验（正态近似，双侧）。
任一侧轮次数 < 10 的节点标 low_sample，只展示、不排序、不给判定。
"""
import argparse, collections, csv, json, math, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MECH = ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]

FIELDS = [
    "model", "level", "path", "name",
    "before_items", "after_items", "before_share_pct", "after_share_pct", "delta_share_pp",
    "before_traces", "before_trace_pct", "after_traces", "after_trace_pct", "delta_trace_pp",
    "before_sessions", "before_session_pct", "after_sessions", "after_session_pct", "delta_session_pp",
    "p_value", "direction", "low_sample",
]


def load_jsonl(p):
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def wilson(k, n, z=1.96):
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - s) / d), min(1.0, (c + s) / d)


def two_prop_p(k1, n1, k2, n2):
    if n1 <= 0 or n2 <= 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        return 1.0
    z = (k2 / n2 - k1 / n1) / se
    return math.erfc(abs(z) / math.sqrt(2))


def load_leaf_names(out_dir):
    names = {}
    p = os.path.join(out_dir, "leaves.tsv")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("path"):
                    names[row["path"]] = row.get("name", "")
    return names


def node_key(level):
    if level == "l1":
        return lambda p: p.split(".")[0]
    if level == "l2":
        return lambda p: ".".join(p.split(".")[:2])
    return lambda p: p


def count(items_src, level):
    key = node_key(level)
    ts = collections.defaultdict(set)
    ss = collections.defaultdict(set)
    icnt = collections.Counter()
    for it in items_src:
        path = it.get("path") or ""
        if not path:
            continue
        k = key(path)
        ts[k].add(it.get("trace_id"))
        ss[k].add(it.get("session_id"))
        icnt[k] += 1
    return ({k: len(v) for k, v in ts.items()},
            {k: len(v) for k, v in ss.items()},
            dict(icnt))


def make_row(model, level, path, name, bt, bs, bit, at, as_, ait, nTB, nSB, nIB, nTA, nSA, nIA):
    trB, trA = (bt / nTB if nTB else 0.0), (at / nTA if nTA else 0.0)
    srB, srA = (bs / nSB if nSB else 0.0), (as_ / nSA if nSA else 0.0)
    shB, shA = (bit / nIB if nIB else 0.0), (ait / nIA if nIA else 0.0)
    dtr = (trA - trB) * 100.0
    dsr = (srA - srB) * 100.0
    p = two_prop_p(bt, nTB, at, nTA)
    low = bt < 10 or at < 10
    if low:
        direction = "样本不足"
    elif p < 0.05 and dtr < 0:
        direction = "改善"
    elif p < 0.05 and dtr > 0:
        direction = "恶化"
    else:
        direction = "无显著变化"
    return {
        "model": model, "level": level, "path": path, "name": name,
        "before_items": bit, "after_items": ait,
        "before_share_pct": round(shB * 100, 2), "after_share_pct": round(shA * 100, 2),
        "delta_share_pp": round((shA - shB) * 100, 2),
        "before_traces": bt, "before_trace_pct": round(trB * 100, 2),
        "after_traces": at, "after_trace_pct": round(trA * 100, 2),
        "delta_trace_pp": round(dtr, 2),
        "before_sessions": bs, "before_session_pct": round(srB * 100, 2),
        "after_sessions": as_, "after_session_pct": round(srA * 100, 2),
        "delta_session_pp": round(dsr, 2),
        "p_value": round(p, 4), "direction": direction, "low_sample": "Y" if low else "",
    }


def md_table(rows, title, limit=None):
    rows = list(rows)
    if limit:
        rows = rows[:limit]
    out = [f"### {title}\n\n",
           "| 节点 | 名称 | 前轮次 | 前轮次率 | 后轮次 | 后轮次率 | Δ轮次(pp) | p | 判定 | 前会话率 | 后会话率 |\n",
           "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|\n"]
    for r in rows:
        out.append(f"| {r['path']} | {r['name']} | {r['before_traces']} | {r['before_trace_pct']}% | "
                   f"{r['after_traces']} | {r['after_trace_pct']}% | {r['delta_trace_pp']:+.2f} | "
                   f"{r['p_value']:.3f} | {r['direction']} | "
                   f"{r['before_session_pct']}% | {r['after_session_pct']}% |\n")
    out.append("\n")
    return "".join(out)


def md_share_table(rows, title):
    out = [f"### {title}\n\n",
           "| 节点 | 名称 | 前占比 | 后占比 | Δ占比(pp) |\n|---|---|---:|---:|---:|\n"]
    for r in rows:
        out.append(f"| {r['path']} | {r['name']} | {r['before_share_pct']:.2f}% | "
                   f"{r['after_share_pct']:.2f}% | {r['delta_share_pp']:+.2f} |\n")
    out.append("\n")
    return "".join(out)


# ------------------------------------------------------------------ charts
def draw_charts(a, res, font_path):
    from PIL import Image, ImageDraw, ImageFont
    if not font_path or not os.path.exists(font_path):
        for cand in ("/System/Library/Fonts/PingFang.ttc",
                     "/System/Library/Fonts/Supplemental/Songti.ttc",
                     "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"):
            if os.path.exists(cand):
                font_path = cand
                break
    if not font_path:
        print("chart skipped: no CJK font")
        return

    def font(sz):
        return ImageFont.truetype(font_path, sz)

    rows_l1 = res["levels"]["l1"]["rows"]
    rows_l2 = res["levels"]["l2"]["rows"]
    l1s = sorted({r["path"] for r in rows_l1}, key=lambda s: int(s[1:]) if s[1:].isdigit() else 99)
    l1name = {r["path"]: r["name"] for r in rows_l1}
    gb = {(r["path"].split(".")[0], r["path"].split(".")[1]): r["before_trace_pct"] for r in rows_l2}
    ga = {(r["path"].split(".")[0], r["path"].split(".")[1]): r["after_trace_pct"] for r in rows_l2}
    gd = {(r["path"].split(".")[0], r["path"].split(".")[1]): r["delta_trace_pp"] for r in rows_l2}
    l2name = {r["path"].split(".")[1]: r["name"] for r in rows_l2}

    cw, chh = 76, 34
    pad_x, pad_top = 150, 132
    W = pad_x + cw * 7 * 3 + 40 * 2 + 40
    H = pad_top + chh * len(l1s) + 130
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((26, 18), f"{a.model} 环节 × 机制 · 轮次率对比（{a.label_before} → {a.label_after}）",
           font=font(21), fill=(15, 23, 42))
    vmax = max([v for v in gb.values()] + [v for v in ga.values()] + [1])
    dmax = max([abs(v) for v in gd.values()] + [0.5])
    for pi, ttl in enumerate(["修复前 轮次率 %", "修复后 轮次率 %", "Δ 百分点（后-前）"]):
        ox = 26 + pi * (cw * 7 + 40)
        d.text((ox + pad_x - 4, 62), ttl, font=font(17), fill=(30, 58, 138))
        for j, m in enumerate(MECH):
            d.text((ox + pad_x + j * cw + cw // 2 - 12, pad_top - 30), m,
                   font=font(14), fill=(100, 116, 139))
    for i, l1 in enumerate(l1s):
        y = pad_top + i * chh
        d.text((26, y + 9), f"{l1} {l1name.get(l1, '')}", font=font(15), fill=(15, 23, 42))
        for pi, g in enumerate((gb, ga, gd)):
            ox = 26 + pi * (cw * 7 + 40)
            for j, m in enumerate(MECH):
                x = ox + pad_x + j * cw
                v = g.get((l1, m), 0.0)
                box = [x, y, x + cw - 8, y + chh - 8]
                if pi < 2:
                    if v:
                        al = math.sqrt(min(1.0, v / max(1e-9, vmax)))
                        col = tuple(int(b + (t - b) * al) for b, t in zip((232, 240, 254), (30, 64, 175)))
                        d.rectangle(box, fill=col)
                        s = f"{v:.1f}"
                        bb = d.textbbox((0, 0), s, font=font(12))
                        d.text((x + (cw - 8 - bb[2]) / 2, y + (chh - 8 - bb[3]) / 2 - 3), s,
                               font=font(12), fill="white" if al > 0.55 else (30, 41, 59))
                    else:
                        d.rectangle(box, outline=(226, 232, 240))
                else:
                    if abs(v) < 0.05:
                        d.rectangle(box, outline=(226, 232, 240))
                        continue
                    al = min(1.0, abs(v) / dmax)
                    if v > 0:
                        col = tuple(int(b + (t - b) * al) for b, t in zip((254, 242, 242), (185, 28, 28)))
                        fg = "white" if al > 0.5 else (127, 29, 29)
                    else:
                        col = tuple(int(b + (t - b) * al) for b, t in zip((240, 253, 244), (21, 128, 61)))
                        fg = "white" if al > 0.5 else (22, 101, 52)
                    d.rectangle(box, fill=col)
                    s = f"{v:+.1f}"
                    bb = d.textbbox((0, 0), s, font=font(11))
                    d.text((x + (cw - 8 - bb[2]) / 2, y + (chh - 8 - bb[3]) / 2 - 3), s,
                           font=font(11), fill=fg)
    d.text((26, H - 66), "  ·  ".join(f"{m} {l2name.get(m, '')}" for m in MECH),
           font=font(13), fill=(71, 85, 105))
    d.text((26, H - 42),
           f"口径：轮次率（命中轮次 / 全部轮次）；前 {a.traces_before:,} 轮次 / 后 {a.traces_after:,} 轮次。"
           f"红=后批更高（恶化），绿=后批更低（改善）。",
           font=font(13), fill=(148, 163, 184))
    img.save(os.path.join(a.out, "compare_matrix.png"))

    leaves = [r for r in res["levels"]["leaf"]["rows"]
              if not r["low_sample"] and r["p_value"] < 0.05]
    ranked = sorted(leaves, key=lambda r: r["delta_trace_pp"])
    neg = ranked[:12]
    pos = ranked[-12:]
    sel = [r for r in ranked if r in neg or r in pos]
    if not sel:
        return
    rowh, top, left, right = 44, 120, 520, 210
    W2, H2 = 1560, top + rowh * len(sel) + 60
    img2 = Image.new("RGB", (W2, H2), "white")
    d2 = ImageDraw.Draw(img2)
    d2.text((26, 20), f"{a.model} 叶级变化 Top（p<0.05，按 Δ轮次率 排序）",
            font=font(21), fill=(15, 23, 42))
    zero = left + int((W2 - left - right) * 0.5)
    dmax2 = max(abs(r["delta_trace_pp"]) for r in sel) or 1
    half = (W2 - left - right) * 0.5
    d2.line([(zero, top - 12), (zero, H2 - 50)], fill=(203, 213, 225))
    d2.text((26, top - 34), "绿色 = 修复后下降（改善）", font=font(13), fill=(21, 128, 61))
    d2.text((26, top - 16), "红色 = 修复后上升（恶化）", font=font(13), fill=(185, 28, 28))
    for i, r in enumerate(sel):
        y = top + i * rowh
        d2.text((26, y + 6), f"{r['path']} {r['name'][:18]}", font=font(14), fill=(15, 23, 42))
        d2.text((26, y + 24), f"轮次率 {r['before_trace_pct']}% → {r['after_trace_pct']}%",
                font=font(12), fill=(100, 116, 139))
        x = zero + int(r["delta_trace_pp"] / dmax2 * (half - 30))
        col = (185, 28, 28) if r["delta_trace_pp"] > 0 else (21, 128, 61)
        d2.rectangle([min(zero, x), y + 8, max(zero, x), y + 32], fill=col)
        s = f"{r['delta_trace_pp']:+.2f}pp p={r['p_value']:.3f}"
        if r["delta_trace_pp"] < 0:
            d2.text((zero + 10, y + 10), s, font=font(12), fill=(51, 65, 85))
        else:
            d2.text((zero - 10 - d2.textlength(s, font=font(12)), y + 10), s, font=font(12), fill=(51, 65, 85))
    img2.save(os.path.join(a.out, "delta_leaf.png"))


def emit_tree_text(res, path, label_before, label_after):
    """人读版：把两棵树压成一棵，每个节点并排给前后轮次率。"""
    l1s = sorted(res["levels"]["l1"]["rows"], key=lambda r: int(r["path"][1:]) if r["path"][1:].isdigit() else 99)
    l2s = res["levels"]["l2"]["rows"]
    lfs = res["levels"]["leaf"]["rows"]
    by_l1 = {}
    for r in l2s:
        p = r["path"].split(".")
        by_l1.setdefault(p[0], []).append(r)
    for v in by_l1.values():
        v.sort(key=lambda r: r["path"].split(".")[1])
    leaf_by_l2 = {}
    for r in lfs:
        if r["before_traces"] + r["after_traces"] == 0:
            continue
        leaf_by_l2.setdefault(".".join(r["path"].split(".")[:2]), []).append(r)
    for v in leaf_by_l2.values():
        v.sort(key=lambda r: -r["before_traces"])

    out = [f"{res['model']}  修复前后对比（轮次率）\n",
           f"  前：{label_before}  {res['traces_before']:,} 轮次 / {res['sessions_before']:,} 会话"
           f"   {res['items_per_trace_before']} 条/轮次\n",
           f"  后：{label_after}  {res['traces_after']:,} 轮次 / {res['sessions_after']:,} 会话"
           f"   {res['items_per_trace_after']} 条/轮次\n\n"]

    def line(r, indent):
        pad = "  " * indent
        flag = ""
        if r["direction"] == "改善":
            flag = "  ← 改善"
        elif r["direction"] == "恶化":
            flag = "  ← 恶化"
        elif r["direction"] == "样本不足":
            flag = "  (样本不足)"
        return (f"{pad}{r['path']} {r['name']}   "
                f"{r['before_trace_pct']:.2f}% → {r['after_trace_pct']:.2f}%   "
                f"Δ{r['delta_trace_pp']:+.2f}pp  p={r['p_value']:.3f}{flag}\n")

    for l1 in l1s:
        out.append(line(l1, 0))
        for l2 in by_l1.get(l1["path"], []):
            if l2["before_traces"] + l2["after_traces"] == 0:
                continue
            out.append(line(l2, 1))
            for lf in leaf_by_l2.get(l2["path"], []):
                out.append(line(lf, 2))
    out.append("\n口径：轮次率 = 命中该节点的轮次数 / 全批轮次数；同一节点内去重，跨节点可重复命中。\n")
    open(path, "w", encoding="utf-8").write("".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sessions-before", type=int, required=True)
    ap.add_argument("--sessions-after", type=int, required=True)
    ap.add_argument("--traces-before", type=int, required=True)
    ap.add_argument("--traces-after", type=int, required=True)
    ap.add_argument("--label-before", default="修复前")
    ap.add_argument("--label-after", default="修复后")
    ap.add_argument("--model", default="")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    inv = json.load(open(os.path.join(ROOT, "taxonomy", "inventory.json"), encoding="utf-8"))
    L1_NAME = {k: v["name"] for k, v in inv["l1"].items()}
    L2_NAME = {k: v["name"] for k, v in inv["l2"].items()}

    bi = load_jsonl(os.path.join(a.before, "item_index.jsonl"))
    ai = load_jsonl(os.path.join(a.after, "item_index.jsonl"))
    leaf_names = load_leaf_names(a.before)
    leaf_names.update(load_leaf_names(a.after))

    nTB, nTA = a.traces_before, a.traces_after
    nSB, nSA = a.sessions_before, a.sessions_after
    res = {
        "model": a.model, "label_before": a.label_before, "label_after": a.label_after,
        "sessions_before": nSB, "sessions_after": nSA,
        "traces_before": nTB, "traces_after": nTA,
        "items_before": len(bi), "items_after": len(ai),
        "items_per_trace_before": round(len(bi) / nTB, 2) if nTB else 0,
        "items_per_trace_after": round(len(ai) / nTA, 2) if nTA else 0,
        "traces_with_issue_before": len({r["trace_id"] for r in bi}),
        "traces_with_issue_after": len({r["trace_id"] for r in ai}),
        "levels": {},
    }

    for level in ("l1", "l2", "leaf"):
        bt, bs, bit = count(bi, level)
        at, as_, ait = count(ai, level)
        rows = []
        for p in sorted(set(bt) | set(at) | set(bs) | set(as_)):
            if level == "l1":
                nm = L1_NAME.get(p, p)
            elif level == "l2":
                nm = L2_NAME.get(p.split(".")[1], p)
            else:
                nm = leaf_names.get(p, p)
            rows.append(make_row(a.model, level, p, nm,
                                 bt.get(p, 0), bs.get(p, 0), bit.get(p, 0),
                                 at.get(p, 0), as_.get(p, 0), ait.get(p, 0),
                                 nTB, nSB, len(bi), nTA, nSA, len(ai)))
        with open(os.path.join(a.out, f"delta_{level}.csv"), "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            for r in sorted(rows, key=lambda x: -x["before_traces"]):
                w.writerow(r)
        ranked = [r for r in rows if not r["low_sample"] and r["before_traces"] + r["after_traces"] > 0]
        res["levels"][level] = {
            "rows": rows,
            "improved": sorted([r for r in ranked if r["direction"] == "改善"], key=lambda x: x["delta_trace_pp"]),
            "regressed": sorted([r for r in ranked if r["direction"] == "恶化"], key=lambda x: -x["delta_trace_pp"]),
        }
        if level == "l1":
            res["levels"][level]["rows"].sort(key=lambda r: int(r["path"][1:]) if r["path"][1:].isdigit() else 99)

    lines = [f"# {a.model} 修复前后分布对比\n\n",
             f"- {a.label_before}：{nSB:,} 会话 / {nTB:,} 轮次 / {len(bi):,} 条"
             f"（{res['items_per_trace_before']} 条/轮次；有问题的轮次 {res['traces_with_issue_before']:,}）\n",
             f"- {a.label_after}：{nSA:,} 会话 / {nTA:,} 轮次 / {len(ai):,} 条"
             f"（{res['items_per_trace_after']} 条/轮次；有问题的轮次 {res['traces_with_issue_after']:,}）\n",
             "- 主口径 = **轮次率**（命中轮次 / 全部轮次）；副口径 = 会话率（与日常树对齐）。\n",
             "- Δ = 后 − 前（百分点 pp）；p = 两比例 z 检验（双侧）；判改善/恶化看轮次率。\n",
             "- 任一侧轮次 < 10 记「样本不足」，只展示、不排序、不给判定。\n\n"]
    lines.append(md_table(res["levels"]["l1"]["rows"], "L1 环节"))
    lines.append(md_share_table(res["levels"]["l1"]["rows"], "L1 环节 · 条目占比（对总条目数归一，抵消\u201c每轮多报几条\u201d的影响）"))
    lines.append(md_table(sorted(res["levels"]["l2"]["rows"], key=lambda r: -r["before_traces"]), "L2 机制"))
    lines.append(md_share_table(sorted(res["levels"]["l2"]["rows"], key=lambda r: -r["before_items"]), "L2 机制 · 条目占比"))
    lines.append(md_table(res["levels"]["leaf"]["improved"], "叶级 · 改善最明显（p<0.05，前 20）", limit=20))
    lines.append(md_table(res["levels"]["leaf"]["regressed"], "叶级 · 恶化最明显（p<0.05，前 20）", limit=20))
    open(os.path.join(a.out, "compare.md"), "w", encoding="utf-8").write("".join(lines))
    emit_tree_text(res, os.path.join(a.out, "tree_compare.txt"), a.label_before, a.label_after)
    json.dump(res, open(os.path.join(a.out, "compare.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("saved:", a.out)

    if a.png:
        try:
            draw_charts(a, res, os.environ.get("CLUSTER_CHART_FONT", ""))
            print("charts saved")
        except Exception as e:
            print("chart skipped:", e)


if __name__ == "__main__":
    main()
