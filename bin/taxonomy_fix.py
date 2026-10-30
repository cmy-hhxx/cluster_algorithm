#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot repair of taxonomy references and known naming defects.

- rewrite pre-v2 stage refs (P/Q/A/T.Mx.y) to current S* ids
- repair dangling `near` pointers in discriminators.json
- rename S8.M4.1 (its old name said "记忆", but the leaf owns skill/file text)

Run `export_inventory.py` afterwards to rebuild INV_<L1>.md.
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREF = {"P": ["S1", "S2"], "Q": ["S3", "S4"], "A": ["S9"], "T": ["S6", "S7", "S8"]}
OLD = re.compile(r"\b([PQA])\.(M[1-7])\.(\d+)\b")

NEAR = {
    "S3.M1.1": "S3.M3.1",
    "S3.M3.6": "S3.M3.4",
    "S4.M4.4": "S4.M4.5",
    "S4.M4.6": "S4.M4.5",
    "S4.M5.1": "S4.M5.3",
    "S4.M5.4": "S4.M5.3",
    "S7.M3.2": "S7.M3.3",
    "S8.M4.1": "S8.M3.3",
}

PATCH = {
    "S3.M3.6": {
        "exclude": "参数为空导致根本没有发起取数，不属本叶。",
    },
    "S8.M4.1": {
        "name": "技能/文件正文被截断",
        "definition": "调用 view_skill/read_file 取回的技能文件或正文在句中断开、字段缺失、语义不完整，无法支撑答复。",
        "include": "含被长度上限截断、尾部丢失、文件只读到前半段。",
        "exclude": "技能加载失败 → S8.M3.3；图像理解结果错误 → S8.M5.1。",
        "plain": "技能/文件内容在句中断开，接不上。",
    },
}


def main():
    inv_path = os.path.join(ROOT, "taxonomy", "inventory.json")
    disc_path = os.path.join(ROOT, "taxonomy", "discriminators.json")
    inv = json.load(open(inv_path, encoding="utf-8"))
    disc = json.load(open(disc_path, encoding="utf-8"))
    ids = {n["id"] for n in inv["nodes"]}
    changed = 0

    def repl_for(node):
        def repl(m):
            pre, mm, num = m.groups()
            cands = [f"{s}.{mm}.{num}" for s in PREF[pre]]
            valid = [c for c in cands if c in ids]
            if not valid:
                return m.group(0)
            same = [c for c in valid if c.startswith(node["l1"] + ".")]
            if same:
                return same[0]
            # Q (old finquery 取数) maps to S3 unless the leaf itself is S4.
            if pre == "Q":
                return "S4." + mm + "." + num if node["l1"] == "S4" and "S4." + mm + "." + num in ids else "S3." + mm + "." + num
            return valid[0]
        return repl

    for node in inv["nodes"]:
        repl = repl_for(node)
        for k in ("definition", "include", "exclude", "plain"):
            if not node.get(k):
                continue
            new = OLD.sub(repl, node[k])
            if new != node[k]:
                node[k] = new
                changed += 1
        if node["id"] in PATCH:
            node.update(PATCH[node["id"]])
            changed += 1

    for nid, near in NEAR.items():
        if nid in disc:
            disc[nid]["near"] = near

    json.dump(inv, open(inv_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(disc, open(disc_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"patched fields={changed} near={len(NEAR)} leaves={len(inv['nodes'])}")


if __name__ == "__main__":
    main()
