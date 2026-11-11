#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复用上一版聚类结果：给新 run 生成 seq -> path 映射，只让新/变更条目走模型。

为什么需要：2026-11-11 的输入规整只改了「身份与定位」（trace_id 取代行号/绝对路径），
归因条目本身没变。全量重跑如果重新归类 20 万条，既慢又会让同一批数据出现判定漂移。
所以重建 = 身份重算 + **按 (trace_id, problem_index) 复用旧判定**，只对三种条目重新归类：
  1) 旧版没有的 trace/条目；
  2) 同一 (trace_id, problem_index) 但 label 或 component 变了（上游重标过）；
  3) 旧版标记为 edge 的（保留旧判定也行，这里选择复用并保留 edge 标记，不再重跑）。

旧数据来源（按优先级）：
  <old>/item_index.jsonl                    交付目录（含 trace_id/problem_index/path/label/component/edge）
  <old>/assign/merged.out.jsonl + items.jsonl   本地基线 run_merged

用法：
  python3 bin/build_reuse_map.py --old <旧交付或 run_merged> --work <新 run> --out <map.jsonl>
  python3 bin/classify.py --work <新 run> --reuse <map.jsonl>
"""
import argparse, glob, json, os, sys


def read_jsonl(p):
    with open(p, encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def load_old(old):
    """返回 (trace_id, problem_index) -> {path,label,component,edge}。"""
    idx = os.path.join(old, 'item_index.jsonl')
    if os.path.isfile(idx):
        rows = list(read_jsonl(idx))
        return {(r['trace_id'], str(r['problem_index'])): r for r in rows if r.get('trace_id')}
    ipath = os.path.join(old, 'items.jsonl')
    if not os.path.isfile(ipath):
        sys.exit(f'旧数据既不是交付目录也不是 run：{old}')
    items = {r['seq']: r for r in read_jsonl(ipath)}
    assign = {}
    for f in glob.glob(os.path.join(old, 'assign', '*.out.jsonl')):
        for r in read_jsonl(f):
            assign[r['seq']] = r
    out = {}
    for s, it in items.items():
        a = assign.get(s)
        if not a or not it.get('trace_id'):
            continue
        out[(it['trace_id'], str(it.get('problem_index', 0)))] = {
            'path': a['path'], 'label': it.get('label', ''), 'component': it.get('component', ''),
            'edge': bool(a.get('edge'))}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='旧交付目录（含 item_index.jsonl）或 run_merged')
    ap.add_argument('--work', required=True, help='新 run 目录（含 items.jsonl）')
    ap.add_argument('--out', required=True)
    ap.add_argument('--require-label', dest='require_label', action='store_true', default=True,
                    help='label/component 变了就不复用（默认开）')
    a = ap.parse_args()

    old = load_old(a.old)
    new = list(read_jsonl(os.path.join(a.work, 'items.jsonl')))
    rows, stale = [], 0
    for it in new:
        o = old.get((it.get('trace_id', ''), str(it.get('problem_index', 0))))
        if not o:
            continue
        if a.require_label and (o['label'] != it.get('label', '') or o['component'] != it.get('component', '')):
            stale += 1
            continue
        rows.append({'seq': it['seq'], 'path': o['path'], 'edge': o['edge']})
    rows.sort(key=lambda r: r['seq'])
    with open(a.out, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f"reuse: {len(rows)}/{len(new)} 条（旧库 {len(old)} 条；label/component 变化不复用 {stale} 条）-> {a.out}")


if __name__ == '__main__':
    main()
