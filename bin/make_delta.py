#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一个全量 ingest 结果里裁出「尚未归类过」的增量条目。

增量流程的第一步：
    ingest 新导出 -> run_new（含全部条目）
    make_delta --old <既有 run> --new <run_new> --out <run_delta>
    chunk + classify 只跑 run_delta
    merge_runs --out <run_merged> <既有 run> <run_delta>
    finish <run_merged>

去重键与 merge_runs 保持同一口径：(trace_id, problem_index)——身份只认 trace，行号/绝对路径已废弃。

用法：python3 bin/make_delta.py --old runs/<t>/run --new runs/<t>/run_new --out runs/<t>/run_delta
"""
import argparse, collections, json, os, sys

KEY = ('trace_id', 'problem_index')


def load(path):
    with open(path, encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def iter_items(path):
    """流式读 items.jsonl（242k 条 / 1 GB+，全量读进内存会把 16 GiB 容器顶爆）。"""
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def keys(items):
    return {tuple(str(it.get(k, '')) for k in KEY) for it in items}


def keys(items):
    return {tuple(str(it.get(k, '')) for k in KEY) for it in items}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='已归类过的 run 目录')
    ap.add_argument('--new', required=True, help='新导出的 run 目录（全量 ingest 结果）')
    ap.add_argument('--out', required=True, help='增量 run 目录（只含新条目）')
    a = ap.parse_args()

    # 内存口径：old 只留去重键；new 边读边写；locators 也流式过滤。
    # 这样 24 万条 / 1 GB 级别的树也不会把两者同时展开在内存里。
    old_path = os.path.join(a.old, 'items.jsonl')
    new_path = os.path.join(a.new, 'items.jsonl')
    old_n = 0
    seen = set()
    for it in iter_items(old_path):
        old_n += 1
        seen.add(tuple(str(it.get(f, '')) for f in KEY))

    os.makedirs(a.out, exist_ok=True)
    delta_seqs = set()
    stage = collections.Counter()
    dup = 0
    exported = 0
    with open(os.path.join(a.out, 'items.jsonl'), 'w', encoding='utf-8') as fh:
        for it in iter_items(new_path):
            exported += 1
            k = tuple(str(it.get(f, '')) for f in KEY)
            if k in seen:
                dup += 1
                continue
            seen.add(k)                  # 同一批内部重复也压掉
            fh.write(json.dumps(it, ensure_ascii=False) + '\n')
            delta_seqs.add(it['seq'])
            stage[it.get('stage', '?')] += 1
    if delta_seqs:
        with open(os.path.join(a.out, 'locators.jsonl'), 'w', encoding='utf-8') as fh:
            for r in iter_items(os.path.join(a.new, 'locators.jsonl')):
                if r['seq'] in delta_seqs:
                    fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    else:
        open(os.path.join(a.out, 'locators.jsonl'), 'w').close()

    n_delta = len(delta_seqs)
    json.dump({'old': old_n, 'exported': exported, 'delta': n_delta,
               'already_known': dup,
               'by_stage': dict(sorted(stage.items(), key=lambda kv: int(kv[0][1:]) if kv[0][1:].isdigit() else 99))},
              open(os.path.join(a.out, 'stats.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'delta: 导出 {exported} 条，已有 {dup} 条跳过，新增 {n_delta} 条 -> {a.out}')
    if not n_delta:
        print('没有新增条目，后续 chunk/classify 可跳过')


if __name__ == '__main__':
    main()
