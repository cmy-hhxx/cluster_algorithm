#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按渠道生成 bad_case 索引：一个「问句实例」一行。

渠道：wencai（问财）、aime（AIME）。两个渠道各出一份文件，不跨渠道合并；bad_case_id 的
哈希里带渠道，所以跨渠道不会撞。

2026-11-03 起两个渠道分到两台机器：每台只配自己渠道的根，所以 --channel 决定建哪几份，
比自己渠道之外的根不存在就不建（不会产出空文件）。

数据来源：
  1) 源 attribution：给出全部 case（含 0 条 problem 的），以及 file/line 便于定位原文；
  2) 各棵树 cluster/*/item_index.jsonl：给出条目数、命中的叶子、所属 tree。

用法：
  # 单机单渠道（推荐）
  python3 bin/build_case_index.py --channel wencai --wencai-root <.../0_wencai/eval_output> --out <索引目录>
  python3 bin/build_case_index.py --channel aime   --aime-root   <.../1_aime/eval_output>   --out <索引目录>

  # 兼容拆分前的单根布局
  python3 bin/build_case_index.py --datasets <datasets 根>

输出：
  <out>/bad_case_index_wencai.jsonl
  <out>/bad_case_index_aime.jsonl
"""
import argparse, collections, glob, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import cluster as C  # noqa: E402  复用 bad_case_id / iso_time / 读取器

# 扫源文件时要跳过的目录名（评测流水线状态、历史归档、聚类产物、对比/判据目录都不算归因源）
SKIP = {'.runs', '.delivery', '.archive', '.git', '.staging', 'cluster',
        'comparison', 'criteria', '__pycache__'}
CH_DIR = {'wencai': '0_wencai', 'aime': '1_aime'}


def read_jsonl(p):
    with open(p, encoding='utf-8', errors='surrogatepass') as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def tree_name(ch, model):
    return f'wencai_{model}' if ch == 'wencai' else model


def skipped(path, root):
    rel = os.path.relpath(path, root).split(os.sep)
    return any(p not in ('.', '') and (p in SKIP or p.startswith('.')) for p in rel)


def scan_sources(ch, root):
    """扫源 attribution，产出 {(channel, bid): {...}} 与 sources 明细。"""
    cases = {}
    for f in sorted(glob.glob(os.path.join(root, '**', '*attribution*.jsonl'), recursive=True)):
        if skipped(os.path.dirname(f), root):
            continue
        model = os.path.basename(os.path.dirname(f))                         # 如 a_fast
        group = os.path.relpath(os.path.dirname(os.path.dirname(f)), root)   # 如 0_daily_eval
        # eval_dir 保持旧格式（<渠道目录>/eval_output/<组>），便于和历史索引对照
        eval_dir = f'{CH_DIR[ch]}/eval_output/{group}'.replace('\\', '/')
        tree = tree_name(ch, model)
        for ln, rec in enumerate(C.read_lines(f), 1):
            if not rec.strip():
                continue
            try:
                r = json.loads(rec)
            except Exception:
                continue
            sm = r.get('session_meta') or {}
            q, t = C.clean(sm.get('question')), C.clean(sm.get('request_time'))
            bid = C.bad_case_id(ch, q, t)
            if not bid:            # question/时间缺失 → 不建 case
                continue
            c = cases.setdefault(bid, {'bad_case_id': bid, 'channel': ch, 'user_id': C.clean(sm.get('user_id')),
                                       'question': q, 'request_time': t, 'request_time_iso': C.iso_time(t),
                                       '_times': [], 'sources': []})
            c['_times'].append(C.iso_time(t) or t)
            c['sources'].append({'tree': tree, 'eval_dir': eval_dir, 'model': model, 'file': f,
                                 'line': ln, 'trace_id': (r.get('turns') or [{}])[0].get('meta', {}).get('trace_id', ''),
                                 'session_id': sm.get('session_id') or '', 'tree_item_count': 0})
    return cases


def scan_trees(ch, root):
    """按 tree 汇总 item_index：{(channel, bid): {'items': n, 'leaves': Counter, 'sessions': set}}。

    item_index 固定在 <组>/cluster/<model>/ 下，所以跳过规则只作用在 <组> 这一段：
    不能把 cluster 自己也当成该跳过的目录——AIME 的 item_index 就在 <根>/cluster/<model>/ 下，
    按整条相对路径过滤会把它的条目全部丢掉（items 恒为 0）。
    """
    agg = {}
    for p in sorted(glob.glob(os.path.join(root, '**', 'cluster', '*', 'item_index.jsonl'), recursive=True)):
        group = os.path.dirname(os.path.dirname(os.path.dirname(p)))   # <根>/<组...>
        if skipped(group, root):
            continue
        model = os.path.basename(os.path.dirname(p))
        tree = tree_name(ch, model)
        for r in read_jsonl(p):
            bid = r.get('bad_case_id') or ''
            if not bid:
                continue
            a = agg.setdefault((ch, bid), {'items': 0, 'leaves': collections.Counter(),
                                           'sessions': set(), 'trees': collections.Counter()})
            a['items'] += 1
            a['trees'][tree] += 1
            a['leaves'][r['path']] += 1
            a['sessions'].add(r.get('session_id', ''))
    return agg


def leaf_names():
    leaf = {n['id']: n['name'] for n in json.load(open(os.path.join(ROOT, 'taxonomy', 'inventory.json'),
                                                       encoding='utf-8'))['nodes']}
    # L4 下钻后 path 可能是 4 段，名字要从 l4_nodes.json 补
    l4_path = os.path.join(ROOT, 'taxonomy', 'l4_nodes.json')
    if os.path.exists(l4_path):
        leaf.update({k: v['name'] for k, v in json.load(open(l4_path, encoding='utf-8')).items()})
    return leaf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--channel', default='auto', choices=['auto', 'wencai', 'aime'])
    ap.add_argument('--wencai-root', default='', help='<...>/0_wencai/eval_output')
    ap.add_argument('--aime-root', default='', help='<...>/1_aime/eval_output')
    ap.add_argument('--out', default='', help='索引输出目录')
    ap.add_argument('--datasets', default='', help='（兼容）单根布局：根下同时有 0_wencai / 1_aime')
    a = ap.parse_args()

    if a.datasets:
        ds = os.path.abspath(a.datasets)
        a.wencai_root = a.wencai_root or os.path.join(ds, '0_wencai', 'eval_output')
        a.aime_root = a.aime_root or os.path.join(ds, '1_aime', 'eval_output')
        a.out = a.out or ds
    roots = {'wencai': os.path.abspath(a.wencai_root) if a.wencai_root else '',
             'aime': os.path.abspath(a.aime_root) if a.aime_root else ''}
    chans = [c for c in ('wencai', 'aime')
             if (a.channel in ('auto', c)) and roots[c] and os.path.isdir(roots[c])]
    if not chans:
        sys.exit('没有可用的渠道根：用 --wencai-root / --aime-root 指定（或 --datasets 兼容旧布局）')
    if not a.out:
        sys.exit('缺少 --out（索引写到哪个目录）')
    out = os.path.abspath(a.out)

    cases, trees = {}, {}
    for ch in chans:
        cases.update(scan_sources(ch, roots[ch]))
        for k, v in scan_trees(ch, roots[ch]).items():
            trees.setdefault(k, v)
    names = leaf_names()

    orphans = 0
    for (ch, bid), agg in trees.items():
        c = cases.get(bid)
        if not c:                      # 产物里有、源里没有（理论上不该发生）
            orphans += 1
            continue
        c['item_count'] = agg['items']
        c['leaf_count'] = len(agg['leaves'])
        if agg['leaves']:
            top, _ = max(agg['leaves'].items(), key=lambda kv: (kv[1], kv[0]))
            c['top_leaf_path'] = top
            c['top_leaf_name'] = names.get(top, '')
        for src in c['sources']:
            src['tree_item_count'] = agg['trees'].get(src['tree'], 0)

    stats = {}
    for ch in chans:
        fname = f'bad_case_index_{ch}.jsonl'
        rows = [c for c in cases.values() if c['channel'] == ch]
        for c in rows:                 # 默认字段
            c.setdefault('item_count', 0)
            c.setdefault('leaf_count', 0)
            c.setdefault('top_leaf_path', '')
            c.setdefault('top_leaf_name', '')
            c.setdefault('status', '')
            times = [t for t in c.pop('_times', []) if t]
            c['first_seen'] = min(times) if times else c['request_time']
            c['last_seen'] = max(times) if times else c['request_time']
        rows.sort(key=lambda c: (c['request_time'], c['bad_case_id']))
        os.makedirs(out, exist_ok=True)
        p = os.path.join(out, fname)
        with open(p, 'w', encoding='utf-8') as fh:
            for c in rows:
                fh.write(json.dumps(c, ensure_ascii=False) + '\n')
        with_items = sum(1 for c in rows if c['item_count'])
        stats[ch] = {'cases': len(rows), 'with_items': with_items, 'without_items': len(rows) - with_items,
                     'items': sum(c['item_count'] for c in rows), 'file': p}
        print(f"{ch}: {len(rows)} 个 case（有归因 {with_items} / 无归因 {len(rows)-with_items}，"
              f"覆盖 {stats[ch]['items']} 条 item） -> {p}")
    if orphans:
        print(f"WARN 有 {orphans} 个 (channel, bad_case_id) 只出现在产物、源里找不到，已跳过")
    return stats


if __name__ == '__main__':
    main()
