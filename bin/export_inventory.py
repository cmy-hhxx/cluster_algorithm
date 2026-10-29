#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export per-L1 decision tables (taxonomy/INV_<L1>.md).

Each leaf gets a decision card: definition, observable signal, include/exclude,
nearest-neighbour leaf + one-line split rule, real positive examples and a real
negative example taken from that neighbour.

Optional --bundle <dir> adds counts and real examples from a delivered bundle.
"""
import argparse, collections, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = {
    'M1': '这一环该做的动作，一点都没做？',
    'M2': '动作做了，但对象/指标/时段/范围不是用户要的？',
    'M3': '调用报错/超时/零行/空返回/被截断？',
    'M4': '有返回，但内容撑不起任务（不相关/样板/过期/缺字段）？',
    'M5': '数据对，但换算/聚合/口径/比较错，或自相矛盾？',
    'M6': '结论没证据，或与已有证据相反？',
    'M7': '形式与完整性不达标（缺项/格式/语言/答非所问）？',
}

ap = argparse.ArgumentParser()
ap.add_argument('--bundle', help='交付目录，用于补充条目数与真实样例')
a = ap.parse_args()

inv = json.load(open(os.path.join(ROOT, 'taxonomy', 'inventory.json'), encoding='utf-8'))
tags = json.load(open(os.path.join(ROOT, 'taxonomy', 'tags.json'), encoding='utf-8'))
leaf_tags = json.load(open(os.path.join(ROOT, 'taxonomy', 'leaf_tags.json'), encoding='utf-8'))
disc = json.load(open(os.path.join(ROOT, 'taxonomy', 'discriminators.json'), encoding='utf-8'))

rows = []
if a.bundle:
    for line in open(os.path.join(a.bundle, 'item_index.jsonl'), encoding='utf-8'):
        if line.strip():
            rows.append(json.loads(line))
by_leaf = collections.defaultdict(list)
for r in rows:
    by_leaf['.'.join(r['path'].split('.')[:3])].append(r)


def bigrams(s):
    s = ''.join(ch for ch in s if '\u4e00' <= ch <= '\u9fff' or ch.isalnum())
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def pick(seqs, profile, k=2):
    """Pick the most representative examples: max bigram overlap with the leaf profile."""
    p = bigrams(profile)
    scored = sorted(seqs, key=lambda r: -len(p & bigrams(r['label'] + r['definition'][:80])) / (len(p) + 1))
    return scored[:k]
n_items = len(rows)

for l1, meta in inv['l1'].items():
    leaves = sorted([n for n in inv['nodes'] if n['l1'] == l1], key=lambda n: (n['l2'], n['id']))
    if not leaves:
        continue
    out = [f"# L1={l1} {meta['name']}", '', meta['definition'], '']

    # 0. gate questions
    out += ['## 0. 判定骨架（按顺序问，命中即停）', '']
    for m in ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7']:
        ms = [n for n in leaves if n['l2'] == m]
        if not ms:
            continue
        out.append(f"{int(m[1])}. **{GATE[m]}** → " + ' / '.join(f"`{n['id']}` {n['name']}" for n in ms))
    out += ['', '> 同一环节内，先定机制再定叶子。跨机制的兄弟不互斥，靠本节顺序裁决。', '']

    # 1. quick table
    out += ['## 1. 速查表', '', '| 叶子 | 名称 | 机制 | 条目 | 会话 | 标签 | 最近邻 |', '|---|---|---|---|---|---|---|']
    for n in leaves:
        seqs = by_leaf.get(n['id'], [])
        cnt = len(seqs)
        sess = len({r['session_id'] for r in seqs})
        d = disc.get(n['id'], {})
        out.append(f"| `{n['id']}` | {n['name']} | {n['l2']} | {cnt or '–'} | {sess or '–'} | "
                   f"{','.join(leaf_tags.get(n['id'], []))} | `{d.get('near','–')}` |")

    # 2. decision cards
    out += ['', '## 2. 判定卡（互斥用）', '']
    for n in leaves:
        d = disc.get(n['id'], {})
        seqs = by_leaf.get(n['id'], [])
        head = f"### `{n['id']}` {n['name']}"
        if seqs:
            head += f"  ·  {len(seqs)} 条 / {len({r['session_id'] for r in seqs})} 会话"
        out += [head, '']
        out.append(f"- **定义**：{n['definition']}")
        out.append(f"- **可观测信号**：{d.get('sig','–')}")
        out.append(f"- **纳入**：{n['include']}")
        out.append(f"- **排除**：{n['exclude']}")
        out.append(f"- **最近邻**：`{d.get('near','–')}` — 区分点：{d.get('split','–')}")
        out.append(f"- **标签**：{','.join(leaf_tags.get(n['id'], []))} ｜ **组件**：{'、'.join(n['components']) or '全部'}")
        if seqs:
            prof = n['name'] + n['definition']
            out.append(f"- **正例**：" + '；'.join(
                f"seq={r['seq']}「{r['label'][:28]}」" for r in pick(seqs, prof)))
            nb = by_leaf.get(d.get('near', ''), [])
            nbnode = next((x for x in inv['nodes'] if x['id'] == d.get('near')), None)
            if nb and nbnode:
                r = pick(nb, nbnode['name'] + nbnode['definition'], 1)[0]
                out.append(f"- **反例（属 `{d.get('near')}`，不属本叶）**：seq={r['seq']}「{r['label'][:30]}」")
        out += ['']

    # 3. pair table
    seen, pairs = set(), []
    for n in leaves:
        d = disc.get(n['id'], {})
        pair = tuple(sorted([n['id'], d.get('near', '')]))
        if d.get('near') and pair not in seen:
            seen.add(pair)
            pairs.append((n['id'], d['near'], d.get('split', '')))
    out += ['## 3. 互斥对照表（最近邻对）', '', '| A | B | 一句话区分 |', '|---|---|---|']
    for a_, b_, s in pairs:
        out.append(f"| `{a_}` {next(x['name'] for x in leaves if x['id']==a_)} | "
                   f"`{b_}` {next((x['name'] for x in leaves if x['id']==b_), '')} | {s} |")
    out += ['']

    open(os.path.join(ROOT, 'taxonomy', f'INV_{l1}.md'), 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    print(f"INV_{l1}.md  leaves={len(leaves)} cards={len(leaves)} pairs={len(pairs)}")
