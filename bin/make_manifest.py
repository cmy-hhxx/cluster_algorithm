#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write MANIFEST.json for a delivered bundle."""
import argparse, hashlib, json, os, datetime

ap = argparse.ArgumentParser()
ap.add_argument('--out', required=True)
ap.add_argument('--run', required=True)
a = ap.parse_args()

def sha16(p):
    h = hashlib.sha256()
    with open(p, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()[:16]

tree = json.load(open(os.path.join(a.out, 'cluster_tree.json'), encoding='utf-8'))
ver = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'VERSION.json'), encoding='utf-8'))
files = ['cluster_tree.json', 'cluster_tree.txt', 'cluster_summary.md', 'cluster_matrix.png',
         'item_index.jsonl', 'syndrome.md', 'syndrome.json', 'leaves.tsv']
man = {
    'bundle': tree['meta']['title'],
    'pipeline_version': ver,
    'generated_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
    'dataset': {k: tree['meta'][k] for k in ('total_items', 'total_traces', 'total_sessions', 'total_cases')},
    'tree': {'depth': tree['meta']['depth'],
             'level1': len(tree['tree']['children']),
             'level2': sum(len(c['children']) for c in tree['tree']['children']),
             'level3': sum(len(l2['children']) for l1 in tree['tree']['children'] for l2 in l1['children']),
             'level4': sum(len(l3.get('children', [])) for l1 in tree['tree']['children'] for l2 in l1['children'] for l3 in l2['children'])},
    'quality': {'edge_flagged': tree['meta']['edge_flagged'], 'edge_rate': tree['meta']['edge_rate']},
    'files': [{'name': f, 'bytes': os.path.getsize(os.path.join(a.out, f)), 'sha256_16': sha16(os.path.join(a.out, f))}
              for f in files if os.path.exists(os.path.join(a.out, f))],
    'notes': ['主口径为会话数，条目数作附列。',
              '每个叶子内嵌 samples，含 case_id/session_id/trace_id/src_file/src_line/json_path。',
              'item_index.jsonl 为全量索引，可用 bin/cluster.py recall 召回。'],
}
json.dump(man, open(os.path.join(a.out, 'MANIFEST.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('manifest written:', len(man['files']), 'files')
