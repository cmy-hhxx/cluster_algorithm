#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Krippendorff's alpha (nominal) for two label files: {"seq":..,"path":..} per line."""
import json, sys, collections, itertools

def load(p):
    d = {}
    for line in open(p, encoding='utf-8'):
        if line.strip():
            r = json.loads(line)
            d[r['seq']] = r['path']
    return d

a, b = load(sys.argv[1]), load(sys.argv[2])
keys = sorted(set(a) & set(b))
if not keys:
    sys.exit('no overlapping seq')
n = len(keys)
labels = sorted({a[k] for k in keys} | {b[k] for k in keys})
idx = {l: i for i, l in enumerate(labels)}
# coincidence matrix
o = [[0.0] * len(labels) for _ in labels]
for k in keys:
    o[idx[a[k]]][idx[b[k]]] += 1
    o[idx[b[k]]][idx[a[k]]] += 1
total = sum(sum(r) for r in o)
margin = [sum(r) for r in o]
De = sum(margin[i] * margin[j] for i in range(len(labels)) for j in range(len(labels)) if i != j) / (total - 1)
Do = sum(o[i][j] for i in range(len(labels)) for j in range(len(labels)) if i != j) / (total - 1)
alpha = 1 - Do / De if De else float('nan')
agree = sum(1 for k in keys if a[k] == b[k]) / n
print(f'n={n} observed_agreement={agree:.3f} alpha={alpha:.3f}')
by = collections.defaultdict(lambda: [0, 0])
for k in keys:
    by[a[k]][1] += 1
    if a[k] == b[k]:
        by[a[k]][0] += 1
worst = sorted(((v[0] / v[1], k, v[1]) for k, v in by.items()))[:10]
print('lowest-agreement leaves:')
for r, k, c in worst:
    print(f'  {k} {r:.0%} (n={c})')
