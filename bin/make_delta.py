#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一个全量 ingest 结果里裁出「尚未归类过」的增量条目。

增量流程的第一步：
    ingest 新导出 -> run_new（含全部条目）
    make_delta --old <既有 run> --new <run_new> --out <run_delta>
    chunk + classify 只跑 run_delta
    merge_runs --out <run_merged> <既有 run> <run_delta>
    finish <run_merged>

去重键与 merge_runs 保持同一口径：(session_id, trace_id, json_path)。

用法：python3 bin/make_delta.py --old runs/<t>/run --new runs/<t>/run_new --out runs/<t>/run_delta
"""
import argparse, collections, json, os, sys

KEY = ('session_id', 'trace_id', 'json_path')


def load(path):
    with open(path, encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def keys(items):
    return {tuple(str(it.get(k, '')) for k in KEY) for it in items}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='已归类过的 run 目录')
    ap.add_argument('--new', required=True, help='新导出的 run 目录（全量 ingest 结果）')
    ap.add_argument('--out', required=True, help='增量 run 目录（只含新条目）')
    a = ap.parse_args()

    old_items = load(os.path.join(a.old, 'items.jsonl'))
    new_items = load(os.path.join(a.new, 'items.jsonl'))
    locs = {r['seq']: r for r in load(os.path.join(a.new, 'locators.jsonl'))}

    seen = keys(old_items)
    delta, dup = [], 0
    for it in new_items:
        k = tuple(str(it.get(f, '')) for f in KEY)
        if k in seen:
            dup += 1
            continue
        seen.add(k)                      # 同一批内部重复也压掉
        delta.append(it)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, 'items.jsonl'), 'w', encoding='utf-8') as fh:
        for it in delta:
            fh.write(json.dumps(it, ensure_ascii=False) + '\n')
    with open(os.path.join(a.out, 'locators.jsonl'), 'w', encoding='utf-8') as fh:
        for it in delta:
            fh.write(json.dumps(locs[it['seq']], ensure_ascii=False) + '\n')

    stage = collections.Counter(it.get('stage', '?') for it in delta)
    json.dump({'old': len(old_items), 'exported': len(new_items), 'delta': len(delta),
               'already_known': dup,
               'by_stage': dict(sorted(stage.items(), key=lambda kv: int(kv[0][1:]) if kv[0][1:].isdigit() else 99))},
              open(os.path.join(a.out, 'stats.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'delta: 导出 {len(new_items)} 条，已有 {dup} 条跳过，新增 {len(delta)} 条 -> {a.out}')
    if not delta:
        print('没有新增条目，后续 chunk/classify 可跳过')


if __name__ == '__main__':
    main()
