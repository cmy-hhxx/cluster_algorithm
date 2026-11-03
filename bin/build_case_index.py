#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按渠道生成 bad_case 索引：一个「问句实例」一行。

渠道：wencai（问财 4 棵树）、aime（AIME 2 棵树）。两个渠道各出一份文件，
不跨渠道合并；bad_case_id 的哈希里带渠道，所以跨渠道不会撞。

数据来源：
  1) 源 attribution：给出全部 case（含 0 条 problem 的），以及 file/line 便于定位原文；
  2) 各棵树 cluster/*/item_index.jsonl：给出条目数、命中的叶子、所属 tree。

用法：
  python3 bin/build_case_index.py --datasets /mnt/fin_bench/sync_datasets/datasets
输出：
  <datasets>/bad_case_index_wencai.jsonl
  <datasets>/bad_case_index_aime.jsonl
"""
import argparse, collections, glob, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import cluster as C  # noqa: E402  复用 bad_case_id / iso_time / 读取器


def read_jsonl(p):
    with open(p, encoding='utf-8', errors='surrogatepass') as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def scan_sources(ds):
    """扫源 attribution，产出 {(channel, bid): {...}} 与 sources 明细。"""
    cases = {}
    for ch, pat in (('wencai', '0_wencai/eval_output/*/*/*attribution*.jsonl'),
                    ('aime', '1_aime/eval_output/*/*/*attribution*.jsonl')):
        for f in sorted(glob.glob(os.path.join(ds, pat))):
            if '/.runs/' in f or '/.archive/' in f:
                continue
            eval_dir = os.path.relpath(os.path.dirname(os.path.dirname(f)), ds)   # 如 0_wencai/eval_output/0_daily_eval
            model = os.path.basename(os.path.dirname(f))                          # 如 a_fast
            tree = (f'wencai_{model}' if ch == 'wencai' else model)
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
                if not bid:            # question/时间缺失 → 不建 case（Q13：跳过）
                    continue
                c = cases.setdefault(bid, {'bad_case_id': bid, 'channel': ch, 'user_id': C.clean(sm.get('user_id')),
                                           'question': q, 'request_time': t, 'request_time_iso': C.iso_time(t),
                                           '_times': [], 'sources': []})
                c['_times'].append(C.iso_time(t) or t)
                c['sources'].append({'tree': tree, 'eval_dir': eval_dir, 'model': model, 'file': f,
                                     'line': ln, 'trace_id': (r.get('turns') or [{}])[0].get('meta', {}).get('trace_id', ''),
                                     'session_id': sm.get('session_id') or '', 'tree_item_count': 0})
    return cases


def scan_trees(ds):
    """按 tree 汇总 item_index：{(channel, bid): {'items': n, 'leaves': Counter, 'sessions': set}}。"""
    agg = {}
    for p in sorted(glob.glob(os.path.join(ds, '**', 'cluster', '*', 'item_index.jsonl'), recursive=True)):
        rel = os.path.relpath(p, ds).split('/')
        model = rel[-2]
        channel = 'wencai' if rel[0].startswith('0_wencai') else 'aime'
        tree = f'wencai_{model}' if channel == 'wencai' else model
        for r in read_jsonl(p):
            bid = r.get('bad_case_id') or ''
            if not bid:
                continue
            a = agg.setdefault((channel, bid), {'items': 0, 'leaves': collections.Counter(),
                                                'sessions': set(), 'trees': collections.Counter()})
            a['items'] += 1
            a['trees'][tree] += 1
            a['leaves'][r['path']] += 1
            a['sessions'].add(r.get('session_id', ''))
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--datasets', required=True)
    ap.add_argument('--out', default='', help='输出目录，默认与 datasets 相同')
    a = ap.parse_args()
    ds, out = os.path.abspath(a.datasets), os.path.abspath(a.out or a.datasets)

    cases = scan_sources(ds)
    trees = scan_trees(ds)
    leaf_name = {n['id']: n['name'] for n in json.load(open(os.path.join(ROOT, 'taxonomy', 'inventory.json'),
                                                            encoding='utf-8'))['nodes']}
    # L4 下钻后 path 可能是 4 段，名字要从 l4_nodes.json 补
    l4_path = os.path.join(ROOT, 'taxonomy', 'l4_nodes.json')
    if os.path.exists(l4_path):
        leaf_name.update({k: v['name'] for k, v in json.load(open(l4_path, encoding='utf-8')).items()})

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
            c['top_leaf_name'] = leaf_name.get(top, '')
        for src in c['sources']:
            src['tree_item_count'] = agg['trees'].get(src['tree'], 0)

    stats = {}
    for ch, fname in (('wencai', 'bad_case_index_wencai.jsonl'), ('aime', 'bad_case_index_aime.jsonl')):
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
