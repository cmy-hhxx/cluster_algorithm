#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对已达 L4 准入（≥300 条）的叶子做下钻归类。

流程：读 run 的 items + 现有 assign（3 段 path）→ 挑出命中目标叶子的条目 →
按 prompts/03_l4_assign.md 的子类清单分批送网关 → 写 assign/zz_l4.out.jsonl
（4 段 path，aggregate 按文件名排序、后写覆盖先写，因此只覆盖这些条目，其余不动）。

子类来源：taxonomy/l4_nodes.json（唯一合法来源，脚本不许自造 id）。

用法：
  python3 bin/l4_apply.py --run runs/aime_fast/run_merged \
      --leaves S1.M2.1,S3.M3.2 --concurrency 16 [--size 60] [--dry]
"""
import argparse, concurrent.futures, glob, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'bin'))
import classify as CL  # noqa: E402  复用网关调用与并发逻辑


def read_jsonl(p):
    with open(p, encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def load_assign(run):
    a = {}
    for f in sorted(glob.glob(os.path.join(run, 'assign', '*.out.jsonl'))):
        for r in read_jsonl(f):
            a[r['seq']] = r['path']
    return a


def plan_text(leaf, l4):
    subs = sorted([k for k in l4 if k.startswith(leaf + '.')])
    lines = []
    for sid in subs:
        n = l4[sid]
        lines.append(f"| `{sid}` | {n['name']} | {n['definition']} | 收：{n['include']} | 不收：{n['exclude']} |")
    return subs, "\n".join(lines)


def build_prompt(leaf, subs_table, rows):
    body = open(os.path.join(ROOT, 'prompts', '03_l4_assign.md'), encoding='utf-8').read()
    body = (body.replace('{{ROOT}}', ROOT).replace('{{LEAF}}', leaf)
                .replace('{{RUN}}', '<run>').replace('{{CHUNK}}', '<chunk>')
                .replace('{{N}}', str(len(rows))))
    payload = {r['seq']: {k: r.get(k, '')[:400] for k in ('component', 'label', 'definition', 'reason')}
               for r in rows}
    return (body + f"\n\n## 本次子类清单（只能从这里选 {leaf}.N）\n\n"
            f"| 类别 | 名称 | 定义 | include | exclude |\n|---|---|---|---|---|\n{subs_table}\n\n"
            f"## 待归类条目（{len(rows)} 条）\n\n```jsonl\n"
            + "\n".join(json.dumps({'seq': s, **v}, ensure_ascii=False) for s, v in payload.items())
            + "\n```\n\n只输出 JSONL，每行 {\"seq\": <int>, \"path\": \"%s.N\", \"edge\": false}。" % leaf)


def judge_chunk(gateways, leaf, subs, rows):
    prompt = build_prompt(leaf, subs[1], rows)
    want = {r['seq'] for r in rows}
    last = None
    for _ in range(2):
        try:
            resp = CL.call(gateways, prompt)
            out = [r for r in CL.parse_rows(resp) if 'seq' in r and 'path' in r]
            got = {r['seq'] for r in out}
            bad = [r for r in out if r['path'] not in subs[0]]
            if got == want and not bad:
                return out
            last = f'rows {len(out)}/{len(want)}, badpath {len(bad)}'
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
    raise RuntimeError(f'{leaf} chunk failed: {last}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--leaves', required=True)
    ap.add_argument('--concurrency', type=int, default=16)
    ap.add_argument('--size', type=int, default=60)
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    l4 = json.load(open(os.path.join(ROOT, 'taxonomy', 'l4_nodes.json'), encoding='utf-8'))
    items = {r['seq']: r for r in read_jsonl(os.path.join(a.run, 'items.jsonl'))}
    assign = load_assign(a.run)
    gateways = CL.healthy_gateways()
    if not gateways:
        sys.exit('没有可用网关')

    out_path = os.path.join(a.run, 'assign', 'zz_l4.out.jsonl')
    results, jobs = [], []
    for leaf in [x.strip() for x in a.leaves.split(',') if x.strip()]:
        subs = plan_text(leaf, l4)
        if len(subs[0]) < 2:
            print(f'跳过 {leaf}：l4_nodes.json 里子类不足 2 个')
            continue
        seqs = [s for s, p in assign.items() if p == leaf]
        if len(seqs) < 300:
            print(f'跳过 {leaf}：只有 {len(seqs)} 条（未达 L4 准入 300）')
            continue
        for i in range(0, len(seqs), a.size):
            part = [items[s] for s in seqs[i:i + a.size]]
            jobs.append((leaf, subs, part))
        print(f'  {leaf}: {len(seqs)} 条 -> {len(subs[0])} 个子类，{(len(seqs)+a.size-1)//a.size} 批')

    if a.dry:
        print(f'[dry] 共 {len(jobs)} 批，未调用网关')
        return

    def work(job):
        leaf, subs, rows = job
        return judge_chunk(gateways, leaf, subs, rows)

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(work, j): j for j in jobs}
        done = 0
        for f in concurrent.futures.as_completed(futs):
            leaf, subs, rows = futs[f]
            try:
                results.extend(f.result())
            except Exception as e:  # noqa: BLE001
                print(f'  FAIL {leaf} ({len(rows)} 条): {e}')
            done += 1
            if done % 10 == 0:
                print(f'  进度 {done}/{len(jobs)} 批')

    with open(out_path, 'w', encoding='utf-8') as fh:
        for r in sorted(results, key=lambda x: x['seq']):
            fh.write(json.dumps({'seq': r['seq'], 'path': r['path'], 'edge': bool(r.get('edge'))},
                                ensure_ascii=False) + '\n')
    covered = len({r['seq'] for r in results})
    print(f'写入 {out_path}：{covered} 条覆盖（{len(results)} 行）')


if __name__ == '__main__':
    main()
