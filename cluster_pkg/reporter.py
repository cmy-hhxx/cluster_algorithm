# -*- coding: utf-8 -*-
"""聚类树聚合、完整性校验、报告与无依赖跨平台可视化生成。

交付目录文件（与主仓 11 件交付标准完全对齐，额外附带零依赖矢量图 cluster_matrix.svg）：
  - cluster_tree.json / cluster_tree.txt
  - cluster_brief.md / cluster_summary.md
  - item_index.jsonl / leaves.tsv
  - syndrome.json / syndrome.md
  - cluster_matrix.svg (+ cluster_matrix.png 当系统具备 Pillow 与中文字体时自动生成)
  - verify.json / MANIFEST.json
"""
import collections
import datetime
import hashlib
import html
import json
import math
import os

from .tax_store import MECHS, TaxonomyStore, jdump

LOW_SAMPLE = 10

FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def _sha16(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def aggregate_and_deliver(
    items_list: list,
    assignments: dict,
    tax: TaxonomyStore,
    out_dir: str,
    version: str,
    sessions_all: int = 0,
    font_path: str = "",
):
    """在 out_dir 生成完整交付物并执行 verify 强校验（失败直接抛异常）。"""
    os.makedirs(out_dir, exist_ok=True)
    items = {it["seq"]: it for it in items_list}
    total = len(items)
    if len(assignments) != total:
        raise RuntimeError(f"归类条数 ({len(assignments)}) 与总条目数 ({total}) 不一致")

    nodes = tax.node_map()
    inv = tax.inv
    l4nodes = tax.l4_nodes
    leaf_tags = tax.leaf_tags
    tagdef = tax.tags

    edge_seqs = {s for s, (_p, ed) in assignments.items() if ed}
    by_path = collections.defaultdict(list)
    for s, (p, _ed) in assignments.items():
        by_path[p].append(s)

    cutoffs = [it.get("request_time") or it.get("date") or "" for it in items.values()]
    data_cutoff = max(cutoffs) if cutoffs else ""

    def ref(s, k=160):
        it = items[s]
        return {
            "seq": s,
            "case_id": it["case_id"],
            "bad_case_id": it.get("bad_case_id", ""),
            "user_id": it.get("user_id", ""),
            "session_id": it["session_id"],
            "trace_id": it["trace_id"],
            "batch_id": it.get("batch_id", ""),
            "model": it.get("model", ""),
            "src_rel": it.get("src_rel", ""),
            "question": (it.get("question") or "")[:120],
            "problem_id": it.get("problem_id", ""),
            "problem_index": it["problem_index"],
            "severity": it.get("severity", ""),
            "component": it.get("component", ""),
            "label": it.get("label", ""),
            "definition": (it.get("definition") or "")[:k],
        }

    def sample(seqs, n=5):
        out, seen_comp = [], set()
        for s in sorted(seqs):
            if len(out) < n and items[s].get("component") in seen_comp:
                continue
            seen_comp.add(items[s].get("component"))
            out.append(ref(s))
            if len(out) >= n:
                break
        have = {o["seq"] for o in out}
        for s in sorted(seqs):
            if len(out) >= n:
                break
            if s not in have:
                out.append(ref(s))
        return out

    def node_stat(seqs):
        return {
            "count": len(seqs),
            "sessions": len({items[s]["session_id"] for s in seqs}),
            "traces": len({items[s]["trace_id"] for s in seqs}),
            "severity": dict(collections.Counter((items[s].get("severity") or "?") for s in seqs)),
        }

    def meta_for(nid):
        if nid in nodes:
            n = nodes[nid]
            return {
                "name": n["name"],
                "plain": n.get("plain", ""),
                "definition": n.get("definition", ""),
                "include": n.get("include", ""),
                "exclude": n.get("exclude", ""),
                "components": n.get("components", []),
            }
        if nid in l4nodes:
            n = l4nodes[nid]
            return {
                "name": n["name"],
                "plain": n.get("plain", ""),
                "definition": n.get("definition", ""),
                "include": n.get("include", ""),
                "exclude": n.get("exclude", ""),
                "components": [],
            }
        return {"name": nid.split(".")[-1], "plain": "", "definition": "", "include": "", "exclude": "", "components": []}

    def build_l3(l1, l2, l3):
        kids = {}
        for p, seqs in by_path.items():
            parts = p.split(".")
            if len(parts) == 4 and parts[0] == l1 and parts[1] == l2 and f"{parts[0]}.{parts[1]}.{parts[2]}" == l3:
                kids[p] = seqs
        base = by_path.get(l3, [])
        all_seqs = base + [s for v in kids.values() for s in v]
        node = {"id": l3, "level": 3, **meta_for(l3), **node_stat(all_seqs)}
        node["share"] = round(node["count"] / max(1, total), 4)
        node["tags"] = leaf_tags.get(l3, [])
        node["low_sample"] = node["count"] < LOW_SAMPLE
        node["samples"] = sample(all_seqs)
        if kids:
            children = []
            for p, seqs in sorted(kids.items(), key=lambda kv: -len(kv[1])):
                c = {"id": p, "level": 4, **meta_for(p), **node_stat(seqs)}
                c["share"] = round(c["count"] / max(1, total), 4)
                c["share_within_l3"] = round(c["count"] / max(1, node["count"]), 3)
                c["low_sample"] = c["count"] < LOW_SAMPLE
                c["samples"] = sample(seqs, 3)
                children.append(c)
            node["children"] = children
        return node

    l1_children = []
    present_l1s = sorted({p.split(".")[0] for p, _ in assignments.values()},
                         key=lambda s: int(s[1:]) if s[1:].isdigit() else 99)
    for l1 in present_l1s:
        l1_seqs = [s for s, (p, _) in assignments.items() if p.startswith(l1 + ".")]
        l2_children = []
        for l2 in MECHS:
            l3ids = sorted(
                {".".join(p.split(".")[:3]) for p, _ in assignments.values() if p.startswith(f"{l1}.{l2}.")},
                key=tax._id_sort_key,
            )
            if not l3ids:
                continue
            kids = [build_l3(l1, l2, i) for i in l3ids]
            c = sum(k["count"] for k in kids)
            l2_children.append({
                "id": f"{l1}.{l2}",
                "level": 2,
                "name": inv["l2"][l2]["name"],
                "mechanism": l2,
                "definition": inv["l2"][l2]["definition"],
                "count": c,
                "share": round(c / max(1, total), 4),
                "share_within_l1": round(c / max(1, len(l1_seqs)), 4),
                "sessions": len({items[s]["session_id"] for s in l1_seqs if assignments[s][0].startswith(f"{l1}.{l2}.")}),
                "children": kids,
            })
        l2_children.sort(key=lambda x: -x["count"])
        c = sum(k["count"] for k in l2_children)
        l1_children.append({
            "id": l1,
            "level": 1,
            "name": inv["l1"][l1]["name"],
            "definition": inv["l1"][l1]["definition"],
            "count": c,
            "share": round(c / max(1, total), 4),
            "sessions": len({items[s]["session_id"] for s in l1_seqs}),
            "components": sorted({items[s]["component"] for s in l1_seqs if items[s].get("component")}),
            "children": l2_children,
        })
    l1_children.sort(key=lambda x: -x["count"])

    tag_roll = collections.defaultdict(lambda: {"count": 0, "sessions": set(), "by_l1": collections.Counter()})
    for s, (p, _) in assignments.items():
        for t in leaf_tags.get(".".join(p.split(".")[:3]), []):
            tag_roll[t]["count"] += 1
            tag_roll[t]["sessions"].add(items[s]["session_id"])
            tag_roll[t]["by_l1"][p.split(".")[0]] += 1
    tags_out = [
        {
            "tag": t,
            "name": tagdef.get(t, {}).get("name", t),
            "definition": tagdef.get(t, {}).get("definition", ""),
            "count": v["count"],
            "sessions": len(v["sessions"]),
            "share": round(v["count"] / max(1, total), 4),
            "by_l1": dict(v["by_l1"]),
        }
        for t, v in sorted(tag_roll.items(), key=lambda kv: -kv[1]["count"])
    ]

    all_stages = tax.stage_ids()
    mech = []
    for m in MECHS:
        sel = [p for p, _ in assignments.values() if p.split(".")[1] == m]
        if not sel:
            continue
        mech.append({
            "mechanism": m,
            "name": inv["l2"][m]["name"],
            "definition": inv["l2"][m].get("definition", ""),
            "count": len(sel),
            "share": round(len(sel) / max(1, total), 4),
            "sessions": len({items[s]["session_id"] for s, (p, _) in assignments.items() if p.split(".")[1] == m}),
            "by_l1": {k: sum(1 for p in sel if p.startswith(k + "."))
                      for k in all_stages if any(p.startswith(k + ".") for p in sel)},
        })

    sessions = {i["session_id"] for i in items.values()}
    tree = {
        "meta": {
            "title": "归因问题聚类树",
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "pipeline_version": version,
            "unit": "原子归因问题条目（每条只归属一个链路 component）",
            "total_items": total,
            "total_traces": len({i["trace_id"] for i in items.values()}),
            "total_sessions": len(sessions),
            "total_cases": len({i["case_id"] for i in items.values()}),
            "sessions_all": sessions_all,
            "depth": 4 if any(len(p.split(".")) == 4 for p, _ in assignments.values()) else 3,
            "data_cutoff": data_cutoff,
            "edge_flagged": len(edge_seqs),
            "edge_rate": round(len(edge_seqs) / max(1, total), 4),
            "axes": {
                "level_1": "环节：失败发生在链路哪一段（component 决定）",
                "level_2": "失效机制：动作错在哪一层（7 类统一口径，按 M1→M7 取最早失效点）",
                "level_3": "失效模式：可派工的动作级模式（互斥、穷尽，支持自适应扩充）",
                "level_4": "细分变体：高影响大叶子下钻",
            },
            "counting": "主口径 = 会话数（同一会话同一叶子计 1）；附列 = 条目数",
            "low_sample_threshold": LOW_SAMPLE,
            "low_sample_rule": "条目数低于阈值的叶子保留在树中用于定位，但不参与排名比较（统计不稳定）",
            "tags": "横切标签，一条可多标签，不改变树的互斥性",
            "traceability": {
                "principle": "每个叶子带 samples，每条带 trace_id + session_id（可回原始输入还原上下文），不含机器绝对路径",
                "item_index": "item_index.jsonl",
            },
        },
        "tree": {
            "id": "root",
            "name": "归因聚类",
            "level": 0,
            "count": total,
            "share": 1.0 if total else 0.0,
            "sessions": len(sessions),
            "children": l1_children,
        },
        "mechanism_rollup": mech,
        "tag_rollup": tags_out,
    }
    jdump(tree, os.path.join(out_dir, "cluster_tree.json"))

    with open(os.path.join(out_dir, "item_index.jsonl"), "w", encoding="utf-8") as fh:
        for s in sorted(assignments):
            it = items[s]
            path, ed = assignments[s]
            row = {
                "seq": s,
                "date": it.get("date", ""),
                "request_time": it.get("request_time", ""),
                "case_id": it["case_id"],
                "bad_case_id": it.get("bad_case_id", ""),
                "user_id": it.get("user_id", ""),
                "session_id": it["session_id"],
                "trace_id": it["trace_id"],
                "batch_id": it.get("batch_id", ""),
                "model": it.get("model", ""),
                "channel": it.get("channel", ""),
                "src_rel": it.get("src_rel", ""),
                "question": it.get("question", ""),
                "turn_index": it.get("turn_index", 0),
                "problem_id": it.get("problem_id", ""),
                "problem_index": it["problem_index"],
                "severity": it.get("severity", ""),
                "component": it.get("component", ""),
                "component_raw": it.get("component_raw", ""),
                "label": it.get("label", ""),
                "path": path,
                "edge": bool(ed),
                "definition": it.get("definition", ""),
                "reason": it.get("reason", ""),
                "tags": leaf_tags.get(".".join(path.split(".")[:3]), []),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    with open(os.path.join(out_dir, "leaves.tsv"), "w", encoding="utf-8") as fh:
        fh.write("path\tl1\tl2\tname\titems\tsessions\tshare\ttags\n")
        for l1 in l1_children:
            for l2 in l1["children"]:
                for l3 in l2["children"]:
                    fh.write(
                        f"{l3['id']}\t{l1['name']}\t{l2['name']}\t{l3['name']}\t{l3['count']}\t"
                        f"{l3['sessions']}\t{l3['share']:.4f}\t{','.join(l3.get('tags', []))}\n"
                    )

    ver = verify_bundle(out_dir)
    _write_reports(tree, ver, inv, out_dir)
    _write_charts(tree, inv, out_dir, font_path)
    _write_syndrome(tree, inv, out_dir)
    _write_manifest(tree, version, out_dir)
    return ver


def verify_bundle(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "cluster_tree.json"), encoding="utf-8") as fh:
        tree = json.load(fh)
    errs = []
    total = tree["tree"]["count"]
    seen = collections.Counter()
    for l1 in tree["tree"]["children"]:
        if sum(x["count"] for x in l1["children"]) != l1["count"]:
            errs.append(f"{l1['id']} sum mismatch")
        for l2 in l1["children"]:
            if sum(x["count"] for x in l2["children"]) != l2["count"]:
                errs.append(f"{l2['id']} sum mismatch")
            for l3 in l2["children"]:
                kids = l3.get("children") or []
                if kids:
                    ksum = sum(x["count"] for x in kids)
                    if ksum > l3["count"]:
                        errs.append(f"{l3['id']} children exceed parent ({ksum} > {l3['count']})")
                    for c in kids:
                        seen[c["id"]] += c["count"]
                    if l3["count"] - ksum:
                        seen[l3["id"]] += l3["count"] - ksum
                else:
                    seen[l3["id"]] += l3["count"]
                if not l3["samples"]:
                    errs.append(f"{l3['id']} no samples")
    if sum(x["count"] for x in tree["tree"]["children"]) != total:
        errs.append("root sum mismatch")

    with open(os.path.join(out_dir, "item_index.jsonl"), encoding="utf-8") as fh:
        idx = [json.loads(l) for l in fh if l.strip()]
    if len(idx) != total:
        errs.append(f"index {len(idx)} != {total}")
    if len({r["seq"] for r in idx}) != len(idx):
        errs.append("duplicate seq")
    if collections.Counter(r["path"] for r in idx) != seen:
        errs.append("index path counts != tree counts")

    idx_tids = {r["trace_id"] for r in idx if r.get("trace_id")}
    mism = 0
    for l1 in tree["tree"]["children"]:
        for l2 in l1["children"]:
            for l3 in l2["children"]:
                for s in l3["samples"]:
                    if not s.get("trace_id") or not s.get("session_id") or s.get("trace_id") not in idx_tids:
                        mism += 1
                        errs.append(f"sample 缺可追溯信息: seq={s.get('seq')}")
    for r in idx:
        if not r.get("trace_id") or not r.get("session_id"):
            errs.append(f"item_index 缺 trace_id/session_id: seq={r.get('seq')}")
            break

    ver = {"leaves": len(seen), "total": total, "locator_mismatch": mism, "errors": len(errs), "items": errs[:50]}
    jdump(ver, os.path.join(out_dir, "verify.json"))
    if errs:
        raise RuntimeError(f"verify 校验未通过 ({len(errs)} 项错误): {errs[:5]}")
    return ver


def _write_reports(t: dict, v: dict, inv: dict, out_dir: str):
    m, root = t["meta"], t["tree"]
    leaves = [l3 for l1 in root["children"] for l2 in l1["children"] for l3 in l2["children"]]
    ranked = sorted([x for x in leaves if not x.get("low_sample")], key=lambda x: -x["sessions"])[:3]
    if not ranked:
        ranked = sorted(leaves, key=lambda x: -x["sessions"])[:3]
    low = [x for x in leaves if x.get("low_sample")]

    brief = [
        "# 这批问题，最该先修什么", "",
        f"> 数据：{m['total_items']:,} 条问题，来自 {m['total_sessions']:,} 个会话"
        + (f"（全量 {m['sessions_all']:,} 个）" if m.get("sessions_all") else "")
        + f"；数据截止 {m.get('data_cutoff') or '未记录'}。",
        "> 一条问题只归一个类别；排名按会话数，不按条目数，避免“问题多的会话”放大。", "",
        "## 结论", "",
    ]
    for i, l3 in enumerate(ranked, 1):
        l1 = next(x for x in root["children"] if any(l3 in l2["children"] for l2 in x["children"]))
        sample = (l3.get("samples") or [{}])[0]
        brief += [
            f"### {i}. {l3['name']}（{l3['sessions']:,} 个会话，{l3['count']:,} 条）", "",
            f"- **用户会遇到什么**：{l3.get('plain', '') or l3.get('definition', '')}",
            f"- **一个真实例子**：{sample.get('label', '')}",
            f"- **该谁修**：{l1['name']}",
            f"- **占全部条目**：{l3['share']:.1%}；同一会话只计 1 次。", "",
        ]

    parent = {l3["id"]: l1["name"] for l1 in root["children"] for l2 in l1["children"] for l3 in l2["children"]}
    subs = [(l3, l4) for l3 in leaves for l4 in l3.get("children", [])]
    if subs:
        def _p01(x):
            sv = x.get("severity") or {}
            return sv.get("P0", 0) + sv.get("P1", 0)
        brief += ["## 已下钻的子类（按 P0/P1 排序）", "",
                  "> P0/P1 = 最高两档严重度；只列已下钻的叶子，没拆的条目仍计在父叶上。", ""]
        for l3, l4 in sorted(subs, key=lambda x: (-_p01(x[1]), -x[1]["sessions"]))[:3]:
            brief += [
                f"### {l4['name']}（{_p01(l4):,} 个 P0/P1，{l4['sessions']:,} 个会话）", "",
                f"- **属于**：{l3['name']}",
                f"- **用户会遇到什么**：{l4.get('plain', '') or l4.get('definition', '')}",
                f"- **该谁修**：{parent.get(l3['id'], '')}",
                f"- **占父叶**：{l4.get('share_within_l3', 0):.1%}", "",
            ]

    mech = sorted(
        [dict(r, definition=r.get("definition") or inv["l2"].get(r["mechanism"], {}).get("definition", ""))
         for r in t["mechanism_rollup"]],
        key=lambda r: -r.get("sessions", 0),
    )
    brief += ["## 失效停在哪一层", "", "| 机制 | 条目 | 会话 | 占比 | 用大白话说 |", "|---|---|---|---|---|"]
    for r in mech[:4]:
        brief.append(f"| {r['name']} | {r['count']:,} | {r.get('sessions', 0):,} | {r['share']:.1%} | {r.get('definition', '')} |")
    if mech:
        brief += ["", f"最多的问题落在「{mech[0]['name']}」：{mech[0].get('definition', '')}"]

    edge_line = (
        f"- 边界标记：{m['edge_flagged']:,} 条（{m['edge_rate']:.1%}）。"
        if m["edge_rate"] >= 0.01
        else f"- 边界标记：{m['edge_flagged']:,} 条（{m['edge_rate']:.1%}）。如需更高精度的分歧边界可设 `cluster.consensus: 2`。"
    )
    brief += [
        "", "## 这份结果怎么读", "",
        f"- 归类覆盖：{m['total_items']:,} 条全部有且只有一个类别（100%）。",
        edge_line,
        f"- 稀有类别：{len(low)} 个类别少于 {m.get('low_sample_threshold', 10)} 条，保留用于定位，不参与排名。",
        f"- 自动校验：{v.get('leaves', 0)} 个类别，可追溯不一致 {v.get('locator_mismatch', 0)} 条，结构错误 {v.get('errors', 0)} 个。",
        "", "完整明细看 `cluster_summary.md`（分布）和 `leaves.tsv`（每个类别的条目数/会话数）。", "",
    ]
    with open(os.path.join(out_dir, "cluster_brief.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(brief))

    # cluster_summary.md
    s_lines = [
        f"# {m['title']}\n",
        f"- 单元：{m['unit']}",
        f"- 规模：{m['total_items']:,} 条问题 / {m['total_traces']:,} trace / {m['total_sessions']:,} session"
        + (f"（全量 {m['sessions_all']:,}）" if m.get("sessions_all") else ""),
        f"- 结构：{len(root['children'])} 环节 · "
        f"{sum(len(c['children']) for c in root['children'])} 机制节点 · "
        f"{sum(len(l2['children']) for l1 in root['children'] for l2 in l1['children'])} 模式"
        + (f" · {len(subs)} 细分" if m["depth"] >= 4 else ""),
        f"- 边界标记：{m['edge_flagged']:,} 条（{m['edge_rate']:.1%}）",
        f"- 计数口径：{m['counting']}\n",
        "## 环节分布\n", "| L1 | 名称 | 条目 | 会话 | 占比 | 主导机制 |", "|---|---|---|---|---|---|",
    ]
    for l1 in root["children"]:
        top = l1["children"][0]
        s_lines.append(
            f"| {l1['id']} | {l1['name']} | {l1['count']:,} | {l1['sessions']:,} | {l1['share']:.1%} | "
            f"{top['name']} {top['share_within_l1']:.0%} |"
        )
    s_lines += ["\n## 机制分布（跨环节）\n", "| 机制 | 名称 | 条目 | 会话 | 占比 |", "|---|---|---|---|---|"]
    for r in t["mechanism_rollup"]:
        s_lines.append(f"| {r['mechanism']} | {r['name']} | {r['count']:,} | {r.get('sessions', 0):,} | {r['share']:.1%} |")
    s_lines += ["\n## 横切标签\n", "| 标签 | 条目 | 会话 | 占比 | 分布 |", "|---|---|---|---|---|"]
    for r in t.get("tag_rollup", []):
        dist = " ".join(f"{k}:{val}" for k, val in sorted(r["by_l1"].items(), key=lambda kv: -kv[1]))
        s_lines.append(f"| {r['name']} | {r['count']:,} | {r['sessions']:,} | {r['share']:.1%} | {dist} |")
    s_lines += [
        "\n## 低样本叶子（不参与排名）\n",
        f"条目数 < {m.get('low_sample_threshold', 10)} 的叶子共 **{len(low)}** 个，"
        f"覆盖 **{sum(x['count'] for x in low):,}** 条（{sum(x['count'] for x in low) / max(1, m['total_items']):.1%}）。\n",
        "\n## 各环节 Top 模式（按会话数，已排除低样本叶）\n",
    ]
    for l1 in root["children"]:
        s_lines.append(f"### {l1['id']} {l1['name']}（{l1['count']:,} 条 / {l1['sessions']:,} 会话）\n")
        s_lines += ["| 叶子 | 模式 | 条目 | 会话 | 一句话 |", "|---|---|---|---|---|"]
        rows = sorted([x for l2 in l1["children"] for x in l2["children"] if not x.get("low_sample")],
                      key=lambda x: -x["sessions"])[:8]
        for l3 in rows:
            s_lines.append(f"| {l3['id']} | {l3['name']} | {l3['count']:,} | {l3['sessions']:,} | {l3.get('plain', '')} |")
        s_lines.append("")
    if subs:
        def _p01(x):
            sv = x.get("severity") or {}
            return sv.get("P0", 0) + sv.get("P1", 0)
        s_lines += [
            "\n## L4 细分（已下钻；未拆条目仍计在父叶上）\n",
            "| 父叶 | 子类 | 条目 | 会话 | 占父叶 | P0+P1 | 一句话 |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for l3, l4 in sorted(subs, key=lambda x: (-_p01(x[1]), -x[1]["sessions"])):
            s_lines.append(
                f"| {l3['id']} {l3['name']} | {l4['id']} {l4['name']} | {l4['count']:,} | "
                f"{l4['sessions']:,} | {l4.get('share_within_l3', 0):.1%} | {_p01(l4):,} | {l4.get('plain', '')} |"
            )
        s_lines.append("")
    with open(os.path.join(out_dir, "cluster_summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(s_lines))

    # cluster_tree.txt
    def pad(s, w=34):
        return s + " " * max(1, w - sum(2 if ord(c) > 127 else 1 for c in s))

    txt = [f"{m['title']}  {m['total_items']:,} 条 / {m['total_sessions']:,} 会话"]
    for i, l1 in enumerate(root["children"]):
        last1 = i == len(root["children"]) - 1
        txt += ["", f"{'└─' if last1 else '├─'} {l1['id']} {l1['name']}  {l1['count']:,} 条 / {l1['sessions']:,} 会话"]
        p1 = "   " if last1 else "│  "
        for j, l2 in enumerate(l1["children"]):
            last2 = j == len(l1["children"]) - 1
            txt.append(f"{p1}{'└─' if last2 else '├─'} {l2['mechanism']} {l2['name']}  {l2['count']:,}")
            p2 = p1 + ("   " if last2 else "│  ")
            for k, l3 in enumerate(l2["children"]):
                last3 = k == len(l2["children"]) - 1
                txt.append(
                    f"{p2}{'└─' if last3 else '├─'} {pad(l3['id'] + ' ' + l3['name'])} "
                    f"{l3['count']:>5} 条 / {l3['sessions']:>4} 会话"
                )
                if l3.get("children"):
                    p3 = p2 + ("   " if last3 else "│  ")
                    for n_idx, l4 in enumerate(l3["children"]):
                        txt.append(
                            f"{p3}{'└─' if n_idx == len(l3['children']) - 1 else '├─'} "
                            f"{pad(l4['name'], 30)} {l4['count']:>5} 条 / {l4['sessions']:>4} 会话"
                        )
    with open(os.path.join(out_dir, "cluster_tree.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(txt))


def _write_charts(t: dict, inv: dict, out_dir: str, font_pref: str = ""):
    """始终生成纯标准库矢量热力图 cluster_matrix.svg；若环境具备 Pillow 与中文字体则同步生成 cluster_matrix.png。"""
    l1s = t["tree"]["children"]
    mname = {k: f"{k} {v['name']}" for k, v in inv["l2"].items()}
    mat = {(l1["id"], l2["mechanism"]): l2["count"] for l1 in l1s for l2 in l1["children"]}
    vmax = max(mat.values() or [1]) or 1

    # 1) 零依赖 SVG 热力图（任何平台、无 Pillow、无本地字体包均可正常渲染中文）
    x0, y0, cw, ch = 220, 110, 168, 68
    W, H = 1440, 130 + max(1, len(l1s)) * ch + 60
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="40" y="52" font-size="24" font-weight="600" fill="#0f172a" '
        f'font-family="system-ui,-apple-system,PingFang SC,Microsoft YaHei,sans-serif">'
        f'环节 × 失效机制 交叉矩阵（{t["meta"]["total_items"]:,} 条）</text>',
    ]
    for j, m in enumerate(MECHS):
        svg.append(
            f'<text x="{x0 + j * cw + 10}" y="{y0 - 16}" font-size="14" fill="#334155" '
            f'font-family="system-ui,-apple-system,PingFang SC,sans-serif">{html.escape(mname[m])}</text>'
        )
    for i, l1 in enumerate(l1s):
        y = y0 + i * ch
        svg.append(
            f'<text x="40" y="{y + 28}" font-size="15" font-weight="500" fill="#0f172a" '
            f'font-family="system-ui,-apple-system,PingFang SC,sans-serif">'
            f'{html.escape(l1["id"] + " " + l1["name"])}</text>'
        )
        svg.append(
            f'<text x="40" y="{y + 48}" font-size="13" fill="#64748b" '
            f'font-family="system-ui,-apple-system,PingFang SC,sans-serif">'
            f'{l1["count"]:,} 条 / {l1["sessions"]:,} 会话</text>'
        )
        for j, m in enumerate(MECHS):
            v = mat.get((l1["id"], m), 0)
            x = x0 + j * cw
            if v:
                al = math.sqrt(v / vmax)
                r, g, b = (int(bg + (fg - bg) * al) for bg, fg in zip((232, 240, 254), (30, 64, 175)))
                txt_col = "#ffffff" if al > 0.55 else "#1e293b"
                svg.append(f'<rect x="{x}" y="{y}" width="{cw - 6}" height="{ch - 6}" rx="4" fill="rgb({r},{g},{b})"/>')
                svg.append(
                    f'<text x="{x + (cw - 6) / 2}" y="{y + (ch - 6) / 2 + 5}" text-anchor="middle" '
                    f'font-size="15" font-weight="500" fill="{txt_col}" '
                    f'font-family="system-ui,-apple-system,sans-serif">{v:,}</text>'
                )
            else:
                svg.append(f'<rect x="{x}" y="{y}" width="{cw - 6}" height="{ch - 6}" rx="4" fill="none" stroke="#e2e8f0"/>')
    svg.append(
        f'<text x="40" y="{H - 22}" font-size="13" fill="#94a3b8" '
        f'font-family="system-ui,-apple-system,PingFang SC,sans-serif">口径：条目数；会话数见各叶子。</text>'
    )
    svg.append("</svg>")
    with open(os.path.join(out_dir, "cluster_matrix.svg"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(svg))

    # 2) 可选 PNG（有 Pillow + 可加载字体时同步产出，保证与旧版 11 件文件名兼容；缺失不报错）
    try:
        from PIL import Image, ImageDraw, ImageFont
        chosen_font = None
        for cand in ([font_pref] if font_pref else []) + list(FONT_CANDIDATES):
            if cand and os.path.isfile(cand):
                try:
                    ImageFont.truetype(cand, 16)
                    chosen_font = cand
                    break
                except Exception:
                    continue
        if not chosen_font:
            return
        f_t, f_s, f_c, f_r = (ImageFont.truetype(chosen_font, s) for s in (26, 15, 16, 15))
        img = Image.new("RGB", (1500, H), "white")
        d = ImageDraw.Draw(img)
        d.text((40, 26), f"环节 × 失效机制 交叉矩阵（{t['meta']['total_items']:,} 条）", font=f_t, fill=(15, 23, 42))
        for j, m in enumerate(MECHS):
            d.text((x0 + j * cw + 12, y0 - 28), mname[m], font=f_r, fill=(51, 65, 85))
        for i, l1 in enumerate(l1s):
            y = y0 + i * ch
            d.text((40, y + 16), f"{l1['id']} {l1['name']}", font=f_r, fill=(15, 23, 42))
            d.text((40, y + 36), f"{l1['count']:,} 条 / {l1['sessions']:,} 会话", font=f_s, fill=(100, 116, 139))
            for j, m in enumerate(MECHS):
                v = mat.get((l1["id"], m), 0)
                x = x0 + j * cw
                if v:
                    al = math.sqrt(v / vmax)
                    col = tuple(int(bg + (fg - bg) * al) for bg, fg in zip((232, 240, 254), (30, 64, 175)))
                    d.rectangle([x, y, x + cw - 6, y + ch - 6], fill=col)
                    s_txt = f"{v:,}"
                    bb = d.textbbox((0, 0), s_txt, font=f_c)
                    d.text(
                        (x + (cw - 6 - bb[2]) / 2, y + (ch - 6 - bb[3]) / 2 - 3),
                        s_txt, font=f_c, fill="white" if al > 0.55 else (30, 41, 59),
                    )
                else:
                    d.rectangle([x, y, x + cw - 6, y + ch - 6], outline=(226, 232, 240))
        d.text((40, H - 40), "口径：条目数；会话数见各叶子。", font=f_s, fill=(148, 163, 184))
        img.save(os.path.join(out_dir, "cluster_matrix.png"))
    except Exception:
        pass


def _write_syndrome(tree: dict, inv: dict, out_dir: str):
    with open(os.path.join(out_dir, "item_index.jsonl"), encoding="utf-8") as fh:
        idx = [json.loads(l) for l in fh if l.strip()]
    per = collections.defaultdict(list)
    for r in idx:
        per[r["session_id"]].append(r)
    l2name = {k: v["name"] for k, v in inv["l2"].items()}
    combos = collections.Counter()
    pairs = collections.Counter()
    for _sid, rows in per.items():
        ms = tuple(sorted({x["path"].split(".")[1] for x in rows if "." in x["path"]}))
        combos[ms] += 1
        leaves = sorted({".".join(x["path"].split(".")[:3]) for x in rows})
        for i in range(len(leaves)):
            for j in range(i + 1, len(leaves)):
                pairs[(leaves[i], leaves[j])] += 1

    lines = [
        "# 会话级失效组合（syndrome）\n",
        f"- 会话总数：{len(per):,}；平均每会话 {sum(len(v) for v in per.values()) / max(1, len(per)):.1f} 条问题",
        "- 组合 = 同一会话内出现的失效机制集合（L2）\n",
        "## 最常见机制组合\n", "| 组合 | 会话数 | 占比 |", "|---|---|---|",
    ]
    for ms, n in combos.most_common(15):
        lines.append(f"| {' + '.join(l2name.get(m, m) for m in ms)} | {n} | {n / max(1, len(per)):.0%} |")
    lines += ["\n## 最常同时出现的模式对\n", "| 模式 A | 模式 B | 共同会话 |", "|---|---|---|"]
    name_map = {
        l3["id"]: l3["name"]
        for l1 in tree["tree"]["children"] for l2 in l1["children"] for l3 in l2["children"]
    }
    for (p1, p2), n in pairs.most_common(15):
        lines.append(f"| {p1} {name_map.get(p1, '')} | {p2} {name_map.get(p2, '')} | {n} |")
    with open(os.path.join(out_dir, "syndrome.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    jdump(
        {
            "combos": [{"mechanisms": list(ms), "sessions": n} for ms, n in combos.most_common(50)],
            "pairs": [{"a": p1, "b": p2, "sessions": n} for (p1, p2), n in pairs.most_common(50)],
        },
        os.path.join(out_dir, "syndrome.json"),
    )


def _write_manifest(tree: dict, version: str, out_dir: str):
    tracked = [
        "cluster_tree.json", "cluster_tree.txt", "cluster_summary.md", "cluster_brief.md",
        "cluster_matrix.png", "cluster_matrix.svg", "item_index.jsonl",
        "syndrome.md", "syndrome.json", "leaves.tsv",
    ]
    man = {
        "bundle": tree["meta"]["title"],
        "module": "cluster_pkg",
        "pipeline_version": version,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset": {k: tree["meta"][k] for k in ("total_items", "total_traces", "total_sessions", "total_cases")},
        "tree": {
            "depth": tree["meta"]["depth"],
            "level1": len(tree["tree"]["children"]),
            "level2": sum(len(c["children"]) for c in tree["tree"]["children"]),
            "level3": sum(len(l2["children"]) for l1 in tree["tree"]["children"] for l2 in l1["children"]),
            "level4": sum(
                len(l3.get("children", []))
                for l1 in tree["tree"]["children"] for l2 in l1["children"] for l3 in l2["children"]
            ),
        },
        "quality": {"edge_flagged": tree["meta"]["edge_flagged"], "edge_rate": tree["meta"]["edge_rate"]},
        "files": [
            {
                "name": fn,
                "bytes": os.path.getsize(os.path.join(out_dir, fn)),
                "sha256_16": _sha16(os.path.join(out_dir, fn)),
            }
            for fn in tracked if os.path.isfile(os.path.join(out_dir, fn))
        ],
    }
    jdump(man, os.path.join(out_dir, "MANIFEST.json"))
