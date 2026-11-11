#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 eval_input（trace 粒度）压成一份 case 身份索引：一行一个 trace_id。

为什么要它：eval_input 是规整后的输入（966 个文件 / 64G），每个 model 目录都很大；
聚类每轮要按 trace_id 拿 question / session_id / request_time / batch_id，逐文件扫太慢，
所以每轮先压成一份 `<CASE_INDEX_ROOT>/cases_<渠道>.jsonl`，ingest 和 badcase 生成都读它。

字段：trace_id / case_id / session_id / question / request_time / request_time_iso / batch_id / model
同一个 trace_id 出现多次（daily 与 lost 快照）时，保留字段最全的一条（question 非空优先，其次
request_time/batch_id 非空更多者）。

用法：
  python3 bin/build_cases_index.py --channel aime \
      --root /mnt/aime_eval_ssd/1_aime/eval_input --out /mnt/aime_eval_ssd
"""
import argparse, collections, glob, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import cluster as C  # noqa: E402


def iter_records(root):
    for f in sorted(glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True)):
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
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        sys.exit(f'eval_input 根不存在：{root}')
    best, files, n = {}, set(), 0
    for f, model_dir, rec in iter_records(root):
        row = pick(rec, model_dir)
        if not row:
            continue
        n += 1
        files.add(f)
        old = best.get(row['trace_id'])
        score = (bool(row['question']), bool(row['session_id']), bool(row['request_time']),
                 bool(row['batch_id']))
        if old is None or score >= old[0]:
            best[row['trace_id']] = (score, row)

    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, f'cases_{a.channel}.jsonl')
    rows = [v[1] for v in best.values()]
    rows.sort(key=lambda r: (r['request_time'], r['trace_id']))
    with open(p, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    per_model = collections.Counter(r['model'] for r in rows)
    print(f"files={len(files)} records={n} traces={len(rows)} "
          f"（{' '.join(f'{k}={v}' for k, v in sorted(per_model.items()))}） -> {p}")


if __name__ == '__main__':
    main()
