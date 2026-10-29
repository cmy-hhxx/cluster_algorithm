#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Migrate L1 from "环节" (P/Q/S/A/T) to "7 段" (S1..S7) as defined in taxonomy/stages.json.

Because every item carries `component` and component->stage is exact, no item needs
re-classification: we rewrite the L1 prefix and split leaves whose items fall in
more than one stage into one copy per stage.

Usage:
  python3 bin/migrate_l1.py --work <run> --out <run7> --bundle <old bundle>
"""
import argparse, collections, json, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ap = argparse.ArgumentParser()
ap.add_argument('--work', required=True, help='现有 run 目录（含 items/locators/assign）')
ap.add_argument('--out', required=True, help='迁移后的 run 目录')
ap.add_argument('--bundle', required=True, help='现有交付目录（取 item_index 判定每个叶子的段分布）')
ap.add_argument('--dry-run', action='store_true')
a = ap.parse_args()

stages = json.load(open(os.path.join(ROOT, 'taxonomy', 'stages.json'), encoding='utf-8'))
comp2stage = {c: s for s, v in stages.items() for c in v['components']}
inv = json.load(open(os.path.join(ROOT, 'taxonomy', 'inventory.json'), encoding='utf-8'))
tags = json.load(open(os.path.join(ROOT, 'taxonomy', 'tags.json'), encoding='utf-8'))
leaf_tags = json.load(open(os.path.join(ROOT, 'taxonomy', 'leaf_tags.json'), encoding='utf-8'))
disc = json.load(open(os.path.join(ROOT, 'taxonomy', 'discriminators.json'), encoding='utf-8'))

idx = [json.loads(l) for l in open(os.path.join(a.bundle, 'item_index.jsonl'), encoding='utf-8')]
by_leaf = collections.defaultdict(list)
for r in idx:
    by_leaf['.'.join(r['path'].split('.')[:3])].append(r)

# ---- build new node set
new_nodes, new_tags, new_disc, idmap = [], {}, {}, {}
multi = 0
for n in inv['nodes']:
    rows = by_leaf.get(n['id'], [])
    st = sorted({comp2stage[r['component']] for r in rows})
    if not st:
        st = sorted({comp2stage[c] for c in n['components'] if c in comp2stage})
    if not st:
        raise SystemExit(f'{n["id"]} 找不到对应 stage')
    if len(st) > 1:
        multi += 1
    suffix = '.'.join(n['id'].split('.')[1:])
    comps_by_stage = collections.defaultdict(set)
    for r in rows:
        comps_by_stage[comp2stage[r['component']]].add(r['component'])
    for s in st:
        nid = f'{s}.{suffix}'
        comps = sorted(comps_by_stage.get(s) or [c for c in n['components'] if comp2stage.get(c) == s])
        node = dict(n)
        node['id'] = nid
        node['l1'] = s
        node['components'] = comps
        if len(st) > 1:
            node['definition'] = f"{n['definition']}（本叶仅收 {'、'.join(comps)} 产生的缺陷）"
        new_nodes.append(node)
        new_tags[nid] = leaf_tags.get(n['id'], [])
        d = disc.get(n['id'])
        if d:
            near = d['near']
            near_stage = s if f'{s}.{".".join(near.split(".")[1:])}' in {f'{x}.{".".join(near.split(".")[1:])}' for x in st} else None
            new_disc[nid] = {'sig': d['sig'], 'near': f'{near_stage or st[0]}.{".".join(near.split(".")[1:])}', 'split': d['split']}
        idmap[(n['id'], s)] = nid

if a.dry_run:
    print(f'leaves {len(inv["nodes"])} -> {len(new_nodes)} (multi-stage leaves: {multi})')
    raise SystemExit(0)

# ---- write taxonomy v2
bak = os.path.join(ROOT, 'taxonomy_v1_backup')
if not os.path.exists(bak):
    shutil.copytree(os.path.join(ROOT, 'taxonomy'), bak)
inv2 = {
    'l1': {s: {'name': v['name'], 'definition': v['definition']} for s, v in stages.items()},
    'l2': inv['l2'],
    'nodes': new_nodes,
}
json.dump(inv2, open(os.path.join(ROOT, 'taxonomy', 'inventory.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(comp2stage, open(os.path.join(ROOT, 'taxonomy', 'comp2l1.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(new_tags, open(os.path.join(ROOT, 'taxonomy', 'leaf_tags.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(new_disc, open(os.path.join(ROOT, 'taxonomy', 'discriminators.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ---- rewrite assignments
os.makedirs(os.path.join(a.out, 'assign'), exist_ok=True)
for f in ('items.jsonl', 'locators.jsonl', 'stats.json'):
    shutil.copy(os.path.join(a.work, f), os.path.join(a.out, f))
seq2comp = {r['seq']: r['component'] for r in idx}
n = 0
for f in sorted(os.listdir(os.path.join(a.work, 'assign'))):
    if not f.endswith('.out.jsonl'):
        continue
    rows = [json.loads(l) for l in open(os.path.join(a.work, 'assign', f), encoding='utf-8') if l.strip()]
    with open(os.path.join(a.out, 'assign', f), 'w', encoding='utf-8') as fh:
        for r in rows:
            r['path'] = f'{comp2stage[seq2comp[r["seq"]]]}.{".".join(r["path"].split(".")[1:])}'
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
            n += 1
print(f'leaves {len(inv["nodes"])} -> {len(new_nodes)} (multi-stage {multi}); assignments rewritten {n}')
