#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 eval_input（trace 粒度）压成一份 case 身份索引：一行一个 trace_id。

为什么要它：eval_input 是规整后的输入（966 个文件 / 64G），每个 model 目录都很大；
聚类每轮要按 trace_id 拿 question / session_id / request_time / batch_id，逐文件扫太慢，
所以每轮先压成一份 `<CASE_INDEX_ROOT>/cases_<渠道>.jsonl`，ingest 和 badcase 生成都读它。

字段：trace_id / case_id / session_id / question / request_time / request_time_iso / batch_id / model
同一个 trace_id 出现多次（daily 与 lost 快照）时，保留字段最全的一条（question 非空优先，其次
request_time/batch_id 非空更多者）。

增量：结果旁边存一份 <out>/cases_<渠道>.state.json（文件 → size+mtime），下一轮只重扫变化的文件；
输入没变时整步几秒钟，不再每轮重读几百 GB（2026-11-12 实测 eval_input 148G，全量扫一次 ~20 分钟）。

用法：
  python3 bin/build_cases_index.py --channel aime \
      --root /mnt/aime_eval_ssd/1_aime/eval_input --out /mnt/aime_eval_ssd
  python3 bin/build_cases_index.py ... --full     # 忽略 state，强制全量重扫
"""
import argparse, collections, glob, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import cluster as C  # noqa: E402


def stat_of(p):
    st = os.stat(p)
    return [st.st_size, st.st_mtime_ns]


def iter_records(files):
    for f in files:
        base = os.path.basename(os.path.dirname(f))
        with open(f, encoding='utf-8', errors='surrogatepass') as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield f, base, json.loads(line)
                except Exception:
                    continue


def pick(rec, model_dir):
    meta = rec.get('meta') or {}
    trs = rec.get('traces')
    tm = (trs[0].get('meta') or {}) if isinstance(trs, list) and trs and isinstance(trs[0], dict) else {}
    tid = C.clean(rec.get('trace_id') or rec.get('case_id'))
    if not tid:
        return None
    q = C.clean(meta.get('question') or tm.get('question') or tm.get('question_zh') or '')
    sid = C.clean(meta.get('session_id') or tm.get('session_id') or '')
    rt = C.clean(meta.get('request_time') or rec.get('request_time') or '')
    return {'trace_id': tid, 'case_id': C.clean(rec.get('case_id') or tid),
            'session_id': sid, 'question': q, 'request_time': rt,
            'request_time_iso': C.iso_time(rt),
            'batch_id': C.clean(rec.get('batch_id') or ''),
            'model': C.clean(rec.get('version') or tm.get('mode') or model_dir)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--channel', required=True, choices=['wencai', 'aime'])
    ap.add_argument('--root', required=True, help='eval_input 根（trace 粒度）')
    ap.add_argument('--out', required=True, help='cases_<渠道>.jsonl 输出目录')
    ap.add_argument('--full', action='store_true', help='忽略 state，强制全量重扫')
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        sys.exit(f'eval_input 根不存在：{root}')
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, f'cases_{a.channel}.jsonl')
    sp = os.path.join(out, f'cases_{a.channel}.state.json')

    def score_of(r):
        return (bool(r.get('question')), bool(r.get('session_id')),
                bool(r.get('request_time')), bool(r.get('batch_id')))

    best, state = {}, {}
    if not a.full and os.path.isfile(p):
        for r in (json.loads(l) for l in open(p, encoding='utf-8') if l.strip()):
            best[r['trace_id']] = (score_of(r), r)
        if os.path.isfile(sp):
            try:
                state = json.load(open(sp, encoding='utf-8'))
            except Exception:
                state = {}

    all_files = sorted(glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True))
    todo, new_state = [], {}
    for f in all_files:
        st = stat_of(f)
        new_state[f] = st
        if state.get(f) != st:
            todo.append(f)

    n = 0
    for f, model_dir, rec in iter_records(todo):
        row = pick(rec, model_dir)
        if not row:
            continue
        n += 1
        cur = best.get(row['trace_id'])
        sc = score_of(row)
        if cur is None or sc >= cur[0]:
            best[row['trace_id']] = (sc, row)

    rows = [v[1] for v in best.values()]
    rows.sort(key=lambda r: (r['request_time'], r['trace_id']))
    # 原子替换：incremental 轮次可能在读这份索引，不能让它读到写了一半的文件
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, p)
    tmp2 = sp + '.tmp'
    json.dump(new_state, open(tmp2, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp2, sp)
    per_model = collections.Counter(r['model'] for r in rows)
    print(f"files={len(all_files)}（重扫 {len(todo)}）new_records={n} traces={len(rows)} "
          f"（{' '.join(f'{k}={v}' for k, v in sorted(per_model.items()))}） -> {p}")


if __name__ == '__main__':
    main()
