#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给已有的 run 回填 case 级字段：user_id / question / request_time / bad_case_id。

背景：这些字段是后加的，历史基线的 items.jsonl 里没有。回填不重新归类——只按
items.jsonl 里的 (src_path, line) 回读原始 attribution 记录，取 session_meta 重新算，
落回 items.jsonl，并由 items 重建 locators.jsonl（locators 本来就是 items 的投影）。

幂等：重复跑结果一致；字段已存在会被覆盖为同一值。

用法：
  python3 bin/backfill_case_fields.py --run runs/<树>/run_merged [--channel auto]
"""
import argparse, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import cluster as C  # noqa: E402

LOC_KEYS = ('seq', 'date', 'file', 'line', 'json_path', 'case_id', 'trace_id', 'session_id',
            'user_id', 'bad_case_id', 'request_time', 'problem_id', 'problem_index',
            'component', 'label', 'turn_index')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--channel', default='auto', choices=['auto', 'wencai', 'aime'])
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    items_p = os.path.join(a.run, 'items.jsonl')
    if not os.path.exists(items_p):
        sys.exit(f'缺少 {items_p}')
    items = [json.loads(l) for l in open(items_p, encoding='utf-8') if l.strip()]

    cache, changed, missing = {}, 0, 0
    for it in items:
        src = it.get('src_path') or it.get('file') or ''
        if src not in cache:
            if not os.path.exists(src):
                cache[src] = None
            else:
                cache[src] = C.read_lines(src)
        lines = cache[src]
        if lines is None:
            missing += 1
            continue
        try:
            rec = json.loads(lines[int(it['line']) - 1])
        except Exception:
            missing += 1
            continue
        sm = rec.get('session_meta') or {}
        ch = C.guess_channel(src) if a.channel == 'auto' else a.channel
        q, t = C.clean(sm.get('question')), C.clean(sm.get('request_time'))
        new = {'user_id': C.clean(sm.get('user_id')), 'question': q, 'request_time': t,
               'request_time_iso': C.iso_time(t), 'bad_case_id': C.bad_case_id(ch, q, t), 'channel': ch}
        if any(it.get(k) != v for k, v in new.items()):
            changed += 1
        it.update(new)

    empty_bid = sum(1 for it in items if not it.get('bad_case_id'))
    if a.dry:
        print(f'[dry] {len(items)} 条，会改 {changed} 条，源文件缺失/坏行 {missing} 条，缺 bad_case_id {empty_bid} 条')
        return

    with open(items_p, 'w', encoding='utf-8') as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + '\n')
    loc_p = os.path.join(a.run, 'locators.jsonl')
    with open(loc_p, 'w', encoding='utf-8') as fh:
        for it in items:
            fh.write(json.dumps({k: it.get(k, '') for k in LOC_KEYS}, ensure_ascii=False) + '\n')
    print(f'{a.run}: {len(items)} 条，改 {changed} 条，源缺失 {missing} 条，缺 bad_case_id {empty_bid} 条；locators 已重建')


if __name__ == '__main__':
    main()
