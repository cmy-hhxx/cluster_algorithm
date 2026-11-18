#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出「环节 × 机制」覆盖矩阵（taxonomy/MATRIX.md）。

12 个环节 × 7 个机制 = 84 格。每格要么有叶子，要么在 matrix_na.json 里
登记「为什么不需要叶子、缺陷落到哪」。两者都不满足就报错退出，
避免再出现「不知道这一格该不该有」的状态。

用法：python3 bin/export_matrix.py [--check]
"""
import argparse, collections, datetime, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "taxonomy")
MECHS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]
# S0 是「未登记工具」的哨兵段，没有叶子也不进矩阵（靠组件登记流程消化）
STAGES = lambda inv: [s for s in inv["l1"] if s != "S0"]


def load(name):
    return json.load(open(os.path.join(T, name), encoding="utf-8"))


def build():
    inv, na = load("inventory.json"), load("matrix_na.json")
    cells = collections.Counter(".".join(n["id"].split(".")[:2]) for n in inv["nodes"])
    empty = {f"{s}.{m}" for s in STAGES(inv) for m in MECHS if cells[f"{s}.{m}"] == 0}
    bad = []
    for c in sorted(empty - set(na["cells"])):
        bad.append(f"空格未登记理由：{c}")
    for c in sorted(set(na["cells"]) - empty):
        bad.append(f"登记了理由但该格有叶子：{c}")
    for c in sorted(set(na["cells"])):
        if not str(na["cells"][c]).strip():
            bad.append(f"理由为空：{c}")
    return inv, cells, empty, bad


def render(inv, cells, empty):
    out = ["# 环节 × 机制 覆盖矩阵", "",
           f"更新于 {datetime.date.today().isoformat()}；"
           f"共 {len(STAGES(inv))} 环节 × {len(MECHS)} 机制 = {len(STAGES(inv)) * len(MECHS)} 格，"
           f"有叶 {sum(1 for s in STAGES(inv) for m in MECHS if cells[f'{s}.{m}'])} 格，"
           f"空 {len(empty)} 格（均已登记不适用理由）。", "",
           "> 数字是该格叶子数；`·` 表示该格不设叶子，理由见下表。",
           "> 新增/删除叶子后重跑 `python3 bin/export_inventory.py && python3 bin/export_matrix.py`。", ""]
    out.append("| 环节 | " + " | ".join(MECHS) + " |")
    out.append("|---|" + "---|" * len(MECHS))
    for s in STAGES(inv):
        meta = inv["l1"][s]
        row = [f"{s} {meta['name']}"]
        for m in MECHS:
            n = cells[f"{s}.{m}"]
            row.append(str(n) if n else "·")
        out.append("| " + " | ".join(row) + " |")
    out += ["", "## 空格位（不设叶子）及归属", "",
            "| 格子 | 为什么不需要 |", "|---|---|"]
    for c in sorted(empty):
        out.append(f"| `{c}` | {load('matrix_na.json')['cells'][c]} |")
    out += ["", "## 维护约定", "",
            "1. 一格里要不要有叶子，看**这一环节能不能独立承接该机制的缺陷**，不是看矩阵好不好看。",
            "2. 已观察到模型在真实数据里索要某格（日志 `bad paths ['Sx.My.z']`）→ 优先补叶，别急着登记不适用。",
            "3. 登记不适用必须写明「落到哪个叶」，否则缺陷会变成没人认领的悬挂项。"]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    a = ap.parse_args()
    inv, cells, empty, bad = build()
    for b in bad:
        print("E", b)
    if a.check:
        sys.exit(1 if bad else 0)
    open(os.path.join(T, "MATRIX.md"), "w", encoding="utf-8").write(render(inv, cells, empty))
    print(f"MATRIX.md 写出：{len(STAGES(inv))}x{len(MECHS)}，空格 {len(empty)}，errors={len(bad)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
