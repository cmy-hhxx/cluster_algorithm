#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Taxonomy consistency check. Exit 1 if any error; warnings never fail.

Usage:
  python3 bin/taxonomy_lint.py [--bundle <delivered dir>] [--json <path>]
"""
import argparse, collections, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_REF = re.compile(r"\b([PQA])\.(M[1-7])\.(\d+)\b")
PATH_REF = re.compile(r"\b(S[1-9])\.(M[1-7])\.(\d+)\b")
VAGUE = ("其他", "杂项", "未知", " miscellaneous", "其它")


def load(name):
    return json.load(open(os.path.join(ROOT, "taxonomy", name), encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle")
    ap.add_argument("--json")
    a = ap.parse_args()

    inv, tags = load("inventory.json"), load("tags.json")
    comp2l1, leaf_tags = load("comp2l1.json"), load("leaf_tags.json")
    disc = load("discriminators.json")
    nodes = inv["nodes"]
    ids = [n["id"] for n in nodes]
    idset = set(ids)
    counts = collections.Counter()
    if a.bundle:
        for line in open(os.path.join(a.bundle, "item_index.jsonl"), encoding="utf-8"):
            if line.strip():
                counts[json.loads(line)["path"]] += 1

    errors, warnings = [], []

    def err(code, msg):
        errors.append({"code": code, "msg": msg})

    def warn(code, msg):
        warnings.append({"code": code, "msg": msg})

    # 1. ids / axes / components
    for x in [i for i, c in collections.Counter(ids).items() if c > 1]:
        err("dup_id", f"重复叶子 id: {x}")
    for n in nodes:
        nid = n["id"]
        if not re.fullmatch(r"S[1-9]\.M[1-7]\.\d+", nid):
            err("bad_id", f"{nid}: id 不符合 S<段>.M<机制>.<序号>")
            continue
        l1, l2 = nid.split(".")[:2]
        if n.get("l1") != l1 or n.get("l2") != l2:
            err("axis_mismatch", f"{nid}: l1/l2 字段与 id 不一致")
        if l1 not in inv["l1"]:
            err("bad_l1", f"{nid}: 段 {l1} 未定义")
        if l2 not in inv["l2"]:
            err("bad_l2", f"{nid}: 机制 {l2} 未定义")
        if not n.get("components"):
            err("no_component", f"{nid}: components 为空")
        for c in n.get("components", []):
            if c not in comp2l1:
                err("unregistered_component", f"{nid}: component {c!r} 未登记")
            elif comp2l1[c] != l1:
                err("component_stage", f"{nid}: component {c!r} 属于 {comp2l1[c]}，叶子在 {l1}")
        for k in ("name", "definition", "include", "exclude"):
            if not str(n.get(k, "")).strip():
                err("empty_field", f"{nid}: {k} 为空")
        if any(v in n.get("name", "") for v in VAGUE):
            err("fallback_bucket", f"{nid}: 名称含兜底词「{n['name']}」")
        if re.search(r"[与或]", n.get("name", "")):
            warn("joined_name", f"{nid}: 名称含「与/或」：{n['name']}")
        if len(n.get("name", "")) > 18:
            warn("long_name", f"{nid}: 名称 {len(n['name'])} 字，可能不够具体/过长：{n['name']}")

    # 2. references
    for n in nodes:
        nid = n["id"]
        own = nid.rsplit(".", 1)[0]
        for k in ("definition", "include", "exclude", "plain"):
            text = str(n.get(k, "") or "")
            for pre, m, num in OLD_REF.findall(text):
                err("stale_ref", f"{nid}.{k}: 旧口径引用 {pre}.{m}.{num}")
            for s, m, num in PATH_REF.findall(text):
                ref = f"{s}.{m}.{num}"
                if ref not in idset:
                    err("dangling_ref", f"{nid}.{k}: 引用了不存在的叶子 {ref}")
            if re.search(rf"\b{re.escape(nid)}\b", text):
                err("self_ref", f"{nid}.{k}: 排他项引用了自己")
            if k == "exclude" and "其他" in text:
                err("fallback_exclude", f"{nid}.exclude: 用「其他」当排他项")
    for nid, d in disc.items():
        if nid not in idset:
            err("orphan_disc", f"discriminators 有孤儿条目 {nid}")
        near = d.get("near", "")
        if near and near not in idset:
            err("dangling_near", f"{nid}: 最近邻 {near} 不存在")

    # 3. duplicate names / definitions
    byname = collections.defaultdict(list)
    for n in nodes:
        byname[n["name"].strip()].append(n["id"])
    for name, xs in byname.items():
        if len(xs) > 1:
            err("dup_name", f"叶子同名「{name}」：{xs}")
    for a_ in nodes:
        for b_ in nodes:
            if a_["id"] >= b_["id"]:
                continue
            da = re.sub(r"（本叶仅收.*?）", "", a_.get("definition", ""))
            db = re.sub(r"（本叶仅收.*?）", "", b_.get("definition", ""))
            if da and da == db and a_["l2"] == b_["l2"]:
                warn("dup_definition", f"{a_['id']} 与 {b_['id']} 定义相同（仅组件不同）")

    # 4. tags coverage
    for nid in idset:
        if nid not in leaf_tags:
            err("missing_tags", f"{nid}: leaf_tags 缺条目")
    for nid in leaf_tags:
        if nid not in idset:
            err("orphan_tags", f"leaf_tags 有孤儿条目 {nid}")

    # 5. sample size
    if counts:
        thin = [(n["id"], counts.get(n["id"], 0)) for n in nodes if counts.get(n["id"], 0) < 10]
        for nid, c in thin:
            warn("low_sample", f"{nid}: 仅 {c} 条（<10），不参与排名")
        used = sum(1 for n in nodes if counts.get(n["id"], 0))
        warn("coverage", f"有样本叶子 {used}/{len(nodes)}；样本 {sum(counts.values())} 条")

    out = {"errors": len(errors), "warnings": len(warnings),
           "error_codes": dict(collections.Counter(e["code"] for e in errors)),
           "warning_codes": dict(collections.Counter(w["code"] for w in warnings)),
           "items": errors + warnings}
    if a.json:
        json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"taxonomy: leaves={len(nodes)} errors={len(errors)} warnings={len(warnings)}")
    if out["error_codes"]:
        print("errors:", json.dumps(out["error_codes"], ensure_ascii=False))
    if out["warning_codes"]:
        print("warnings:", json.dumps(out["warning_codes"], ensure_ascii=False))
    for e in errors[:40]:
        print(f"  E {e['code']}: {e['msg']}")
    for w in warnings[:12]:
        print(f"  W {w['code']}: {w['msg']}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
