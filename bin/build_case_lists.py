#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 badcase / goldencase 清单（按渠道，一行一个 case）。

口径（2026-11-11 与用户确认）：
  - case 的身份 = trace_id（eval_input 一行一 trace）；另带 session_id 便于还原现场。
  - badcase    ：该 case 有任意 problem 的 severity ∈ {P0, P1}。
  - goldencase ：该 case 的 problem 为空（上游没判出任何缺陷）。
  - P2–P4 的 case 不进任何一份（只留聚类树里做分布）。
  - 不写行号、不写绝对路径：只留 question + session_id + trace_id（+ batch_id/severity 便于筛选），
    拿 trace_id 回 eval_input 即可还原原始 messages。

数据来源：
  - 归因：--attr-root 下的 *attribution*.jsonl（problem + severity），按 trace_id 聚合；
  - 身份：--cases-root（eval_input）里的 question/session_id/request_time/batch_id。

用法：
  python3 bin/build_case_lists.py --channel aime \
      --attr-root  /mnt/aime_eval_ssd/1_aime/eval_output \
      --cases-root /mnt/aime_eval_ssd/1_aime/eval_input \
      --out        /mnt/aime_eval_ssd

输出：
  <out>/badcase_<渠道>.jsonl
  <out>/goldencase_<渠道>.jsonl
"""
import argparse, collections, glob, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import cluster as C  # noqa: E402  复用 clean / bad_case_id / iso_time

SKIP = {'.runs', '.delivery', '.archive', '.git', '.staging', 'cluster',
        'comparison', 'criteria', '__pycache__'}
BAD_SEV = {'P0', 'P1'}
SEV_ORDER = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3, 'P4': 4, '': 9}


def read_jsonl(path):
    with open(path, encoding='utf-8', errors='surrogatepass') as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def skipped(path, root):
    rel = os.path.relpath(path, root).split(os.sep)
    return any(p not in ('.', '') and (p in SKIP or p.startswith('.')) for p in rel)


def load_cases(root):
    """eval_input → trace_id 身份表（与 cluster.load_cases 同口径）。"""
    return C.load_cases(root) if root else {}


def scan(channel, attr_root, cases, stats):
    """按 (model, trace_id) 聚合成 case。"""
    rows = {}
    for f in sorted(glob.glob(os.path.join(attr_root, '**', '*attribution*.jsonl'), recursive=True)):
        if skipped(os.path.dirname(f), attr_root):
            continue
        model = os.path.basename(os.path.dirname(f))
        for rec in read_jsonl(f):
            smeta = rec.get('session_meta') or {}
            for turn in (rec.get('turns') or []):
                tid = C.clean((turn.get('meta') or {}).get('trace_id'))
                if not tid:
                    stats['no_trace_id'] += 1
                    continue
                c = cases.get(tid) or {}
                key = (model, tid)
                r = rows.get(key)
                if r is None:
                    r = rows[key] = {'trace_id': tid, 'model': model,
                                     'question': c.get('question') or C.clean(smeta.get('question')),
                                     'session_id': c.get('session_id') or C.clean(smeta.get('session_id')),
                                     'request_time': c.get('request_time') or C.clean(smeta.get('request_time')),
                                     'batch_id': c.get('batch_id') or C.clean(rec.get('batch_id')),
                                     'severity_counts': collections.Counter(), 'problem_count': 0,
                                     '_times': set()}
                    if not c:
                        stats['no_case'] += 1
                    if c.get('request_time'):
                        r['_times'].add(C.iso_time(c['request_time']))
                for p in (turn.get('problem') or []):
                    if not isinstance(p, dict):
                        continue
                    r['problem_count'] += 1
                    r['severity_counts'][C.clean(p.get('severity')) or 'NA'] += 1
                if c.get('request_time'):
                    r['_times'].add(C.iso_time(c['request_time']))
    return rows


def emit(channel, rows, out):
    stats = {}
    for name, sel in (('badcase', lambda r: r['problem_count'] and
                       min(r['severity_counts'], key=lambda s: SEV_ORDER.get(s, 9)) in BAD_SEV),
                      ('goldencase', lambda r: r['problem_count'] == 0)):
        picked = [r for r in rows.values() if sel(r)]
        for r in picked:
            sev = r['severity_counts']
            worst = min(sev, key=lambda s: SEV_ORDER.get(s, 9)) if sev else ''
            times = sorted(t for t in r.pop('_times', set()) if t)
            r['bad_case_id'] = C.bad_case_id(channel, r['question'], r['request_time'])
            r['channel'] = channel
            r['severity'] = worst if r['problem_count'] else ''
            r['severity_counts'] = dict(sorted(sev.items(), key=lambda kv: SEV_ORDER.get(kv[0], 9)))
            r['first_seen'] = times[0] if times else C.iso_time(r['request_time'])
            r['last_seen'] = times[-1] if times else C.iso_time(r['request_time'])
        picked.sort(key=lambda r: (r['request_time'], r['trace_id']))
        p = os.path.join(out, f'{name}_{channel}.jsonl')
        with open(p, 'w', encoding='utf-8') as fh:
            for r in picked:
                row = {k: r[k] for k in ('bad_case_id', 'channel', 'model', 'question', 'session_id',
                                         'trace_id', 'batch_id', 'request_time', 'severity',
                                         'severity_counts', 'problem_count', 'first_seen', 'last_seen')}
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        stats[name] = {'cases': len(picked), 'file': p,
                       'by_model': dict(collections.Counter(r['model'] for r in picked))}
        print(f"{channel} {name}: {len(picked)} 个 case"
              f"（{'/'.join(f'{k}:{v}' for k, v in stats[name]['by_model'].items())}） -> {p}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--channel', required=True, choices=['wencai', 'aime'])
    ap.add_argument('--attr-root', required=True, help='归因根（含 *attribution*.jsonl，如 .../eval_output）')
    ap.add_argument('--cases-root', default='', help='eval_input 根（trace 粒度，提供 question/session_id）')
    ap.add_argument('--out', required=True, help='清单输出目录')
    a = ap.parse_args()

    attr_root = os.path.abspath(a.attr_root)
    if not os.path.isdir(attr_root):
        sys.exit(f'归因根不存在：{attr_root}')
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)

    cases = load_cases(a.cases_root)
    stats = collections.Counter()
    rows = scan(a.channel, attr_root, cases, stats)
    have = emit(a.channel, rows, out)
    for k, v in have.items():
        stats[f'{k}_cases'] = v['cases']
    print(f"cases={len(rows)} traces_missing_case={stats['no_case']} "
          f"turn_without_trace_id={stats['no_trace_id']} "
          f"badcase={stats['badcase_cases']} goldencase={stats['goldencase_cases']}")
    if stats['no_case']:
        print('WARN 有 trace 在 eval_input 里找不到身份（仍按归因自带 question/session 落盘），'
              '检查 --cases-root 是否覆盖全量日期')


if __name__ == '__main__':
    main()
