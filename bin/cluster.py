#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Attribution clustering toolkit.

Subcommands:
  ingest     attribution.jsonl -> items.jsonl + locators.jsonl + stats.json
  chunk      items.jsonl -> assign/*.jsonl + manifest.json (grouped by L1)
  aggregate  assignments + taxonomy -> cluster_tree.json + item_index.jsonl + leaves.tsv
  verify     integrity checks on the produced bundle
  report     cluster_tree.txt + cluster_summary.md
  chart      cluster_matrix.png
  syndrome   syndrome.json / syndrome.md  (session-level failure combinations)
  recall     query items by node id

Design rules live in README.md; do not change counting rules here without updating it.
"""
import argparse, collections, datetime, glob, hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOW_SAMPLE = 10
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------- helpers
def jload(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, encoding='utf-8') as fh:
        return json.load(fh)


def jdump(obj, p):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def read_lines(path):
    """Read a jsonl file tolerating CESU-8/BOM/surrogate escapes and stray blank lines."""
    with open(path, encoding='utf-8', errors='surrogatepass') as fh:
        raw = fh.read()
    # split on '\n' only: str.splitlines() would also split on U+2028/U+2029
    return raw.split('\n')


def clean(s):
    if s is None:
        return ''
    if not isinstance(s, str):
        return str(s)
    if any('\ud800' <= c <= '\udfff' for c in s):
        return s.encode('utf-8', 'ignore').decode('utf-8')
    return s


def as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except Exception:
            return []
    return []


def sha16(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()[:16]


def component_map():
    return jload(os.path.join(ROOT, 'taxonomy', 'components.json'), {})


_PATTERNS = None


def component_patterns():
    """模式登记表：一次性脚本路径 / 「X 请求生成」这类写法按规则归类，避免逐条登记。"""
    global _PATTERNS
    if _PATTERNS is None:
        d = jload(os.path.join(ROOT, 'taxonomy', 'component_patterns.json'), {}) or {}
        _PATTERNS = [(r['id'], re.compile(r['match']), r) for r in d.get('rules', [])]
    return _PATTERNS


def match_pattern(raw):
    for rid, rx, rule in component_patterns():
        if rx.search(raw):
            return rule
    return None


def canon_component(raw):
    """raw component -> (tool, label, stage)。先精确匹配，再走模式表，都没命中返回 raw。"""
    m = component_map().get(raw)
    if m:
        return (m['tool'], m['label'], m.get('stage', ''))
    rule = match_pattern(raw)
    if rule:
        return (rule['tool'], rule.get('label', ''), rule.get('stage', ''))
    return (raw, '', '')


CHANNEL_PREFIX = {'wencai': 'bc_wc_', 'aime': 'bc_ai_'}


def norm_question(q):
    return re.sub(r'\s+', ' ', (q or '').strip())


def norm_minute(ts):
    """'2026-10-01 15:26:55' 与 '2026-10-22T01:10:07.510Z' 都归一到 'YYYY-MM-DD HH:MM'；解析不了返回 ''。"""
    m = re.match(r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})', (ts or '').strip())
    return f'{m.group(1)} {m.group(2)}' if m else ''


def iso_time(ts):
    s = (ts or '').strip()
    if not s:
        return ''
    if re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', s):
        return s
    m = re.match(r'^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$', s)
    return f'{m.group(1)}T{m.group(2)}+08:00' if m else ''


def bad_case_id(channel, question, request_time):
    """一个「问句实例」的稳定 id：渠道 + 规范化问句 + 截断到分钟的时间。
    时间截断到分钟是为了吸收跨树跑批的 1-2 秒漂移；渠道进哈希是为了让问财/AIME 不互相撞。"""
    q, t = norm_question(question), norm_minute(request_time)
    if not q or not t:
        return ''
    h = hashlib.sha256(f'{channel}\x1f{q}\x1f{t}'.encode('utf-8')).hexdigest()[:16]
    return CHANNEL_PREFIX.get(channel, 'bc_') + h


def guess_channel(path):
    p = path.replace('\\', '/')
    return 'aime' if '/1_aime/' in p or p.startswith('1_aime/') else 'wencai'


def taxonomy(dir_=None):
    d = dir_ or os.path.join(ROOT, 'taxonomy')
    inv = jload(os.path.join(d, 'inventory.json'))
    tags = jload(os.path.join(d, 'tags.json'), {})
    l4 = jload(os.path.join(d, 'l4_nodes.json'), {})
    return inv, tags, l4


# ---------------------------------------------------------------- ingest
def cmd_ingest(a):
    files = []
    if os.path.isdir(a.input):
        files = sorted(glob.glob(os.path.join(a.input, '**', '*.jsonl'), recursive=True))
    else:
        files = [a.input]
    if not files:
        sys.exit(f'no jsonl under {a.input}')

    cases = {}
    for cf in (sorted(glob.glob(os.path.join(a.cases, '*.jsonl'))) if a.cases else []):
        for line in read_lines(cf):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            tid = r.get('trace_id') or r.get('case_id')
            cases[tid] = {'case_id': r.get('case_id'), 'question': clean((r.get('meta') or {}).get('question', ''))}

    items, loc, bad = [], [], []
    seq = 0
    for f in files:
        base = os.path.basename(f)
        channel = guess_channel(f) if a.channel == 'auto' else a.channel
        for ln, line in enumerate(read_lines(f), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                bad.append({'file': base, 'line': ln, 'error': str(e)[:120]})
                continue
            smeta = rec.get('session_meta') or {}
            sid = smeta.get('session_id') or ''
            uid = clean(smeta.get('user_id'))
            question = clean(smeta.get('question'))
            rtime = clean(smeta.get('request_time'))
            bid = bad_case_id(channel, question, rtime)
            for ti, t in enumerate(rec.get('turns') or []):
                meta = t.get('meta') or {}
                tid = meta.get('trace_id') or ''
                ic = cases.get(tid) or {}
                for pi, p in enumerate(as_list(t.get('problem'))):
                    if not isinstance(p, dict):
                        continue
                    seq += 1
                    tool, comp_label, stage = canon_component(clean(p.get('component')))
                    cent = component_map().get(clean(p.get('component'))) or {}
                    it = {
                        'seq': seq, 'date': (smeta.get('request_time') or '')[:10],
                        'file': f'{os.path.basename(os.path.dirname(f))}/{base}' if os.path.dirname(f) else base,
                        'src_path': f, 'line': ln, 'json_path': f'turns[{ti}].problem[{pi}]',
                        'case_id': ic.get('case_id') or tid, 'trace_id': tid, 'session_id': sid,
                        'user_id': uid, 'question': question, 'request_time': rtime,
                        'request_time_iso': iso_time(rtime), 'bad_case_id': bid, 'channel': channel,
                        'turn_index': ti, 'problem_index': pi,
                        'problem_id': clean(p.get('problem_id')),
                        'component': tool,
                        'component_label': comp_label,
                        'component_raw': clean(p.get('component')),
                        'component_role': cent.get('role', ''),
                        'component_family': clean(p.get('component_family')),
                        'stage': stage,
                        'label': clean(p.get('label')), 'definition': clean(p.get('definition')),
                        'phenomena': clean(p.get('problem_phenomena')), 'reason': clean(p.get('reason')),
                        'evidence': clean(p.get('evidence')),
                        'task': clean(meta.get('task')), 'difficulty': clean(meta.get('difficulty')),
                    }
                    items.append(it)
                    loc.append({k: it[k] for k in ('seq', 'date', 'file', 'line', 'json_path', 'case_id',
                                                   'trace_id', 'session_id', 'user_id', 'bad_case_id',
                                                   'request_time', 'problem_id', 'problem_index',
                                                   'component', 'label', 'turn_index')})
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, 'items.jsonl'), 'w', encoding='utf-8') as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + '\n')
    with open(os.path.join(a.out, 'locators.jsonl'), 'w', encoding='utf-8') as fh:
        for r in loc:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    stats = {
        'files': [os.path.basename(f) for f in files],
        'records': len({i['trace_id'] for i in items}),
        'sessions': len({i['session_id'] for i in items}),
        'cases': len({i['case_id'] for i in items}),
        'items': len(items),
        'bad_lines': bad[:20], 'bad_line_count': len(bad),
        'components': dict(collections.Counter(i['component'] for i in items).most_common()),
    }
    jdump(stats, os.path.join(a.out, 'stats.json'))
    cmap = jload(os.path.join(ROOT, 'taxonomy', 'components.json'), {})
    known = {v['tool'] for v in cmap.values()}
    unknown = sorted({i['component'] for i in items
                      if i['component'] not in known and not i.get('stage')})
    if unknown:
        jdump({'unregistered': unknown}, os.path.join(a.out, 'unregistered.json'))
        sys.exit('未登记 component（已写入 unregistered.json）：' + ', '.join(unknown) +
                 '\n请先登记到 taxonomy/components.json 并指定所属段，再重跑 ingest。')
    print(f"items={len(items)} traces={stats['records']} sessions={stats['sessions']} "
          f"components={len(stats['components'])} bad_lines={len(bad)}")


# ---------------------------------------------------------------- chunk
def load_comp2l1():
    """Component -> L1 must be explicit: leaf `components` lists overlap across L1s."""
    m = jload(os.path.join(ROOT, 'taxonomy', 'comp2l1.json'))
    if not m:
        sys.exit('taxonomy/comp2l1.json missing')
    return m


def cmd_chunk(a):
    items = [json.loads(l) for l in open(os.path.join(a.work, 'items.jsonl'), encoding='utf-8')]
    c2l = load_comp2l1()
    by = collections.defaultdict(list)
    unknown = collections.Counter()
    for it in items:
        # v4：条目自带 stage（由 component+role 在 ingest 定死），老 run 退回 comp2l1
        l1 = it.get('stage') or c2l.get(it['component'])
        if l1 and '+' in str(l1):
            l1 = None
        if not l1:
            unknown[it['component']] += 1
            sys.exit(f'未登记 component：{it["component"]!r}。请先写入 taxonomy/components.json 并指定所属段，再重跑。')
        it['l1'] = l1
        by[l1].append(it)
    outdir = os.path.join(a.work, 'assign')
    os.makedirs(outdir, exist_ok=True)
    manifest, k = [], 0
    for l1 in sorted(by):
        rows = sorted(by[l1], key=lambda x: x['seq'])
        for i in range(0, len(rows), a.size):
            part = rows[i:i + a.size]
            cid = f'{l1}{i // a.size + 1:02d}'
            with open(os.path.join(outdir, f'{cid}.jsonl'), 'w', encoding='utf-8') as fh:
                for it in part:
                    fh.write(json.dumps({k2: it[k2] for k2 in ('seq', 'component', 'label', 'definition', 'reason')},
                                        ensure_ascii=False) + '\n')
            manifest.append({'chunk': cid, 'l1': l1, 'n': len(part), 'seqs': [x['seq'] for x in part]})
    jdump(manifest, os.path.join(outdir, 'manifest.json'))
    print(f'chunks={len(manifest)} ' + ' '.join(f'{k}:{v}' for k, v in sorted(collections.Counter(x["l1"] for x in manifest).items())))
    if unknown:
        print('ERROR 未登记 component，必须在 taxonomy/components.json 中登记后才能分片：')
        for c, n in unknown.most_common():
            print(f'    {n:5d}  {c}')


# ---------------------------------------------------------------- aggregate
def cmd_aggregate(a):
    inv, tagdef, l4nodes = taxonomy()
    nodes = {n['id']: n for n in inv['nodes']}
    items = {r['seq']: r for r in (json.loads(l) for l in open(os.path.join(a.work, 'items.jsonl'), encoding='utf-8'))}
    loc = {r['seq']: r for r in (json.loads(l) for l in open(os.path.join(a.work, 'locators.jsonl'), encoding='utf-8'))}
    leaf_tags = jload(os.path.join(ROOT, 'taxonomy', 'leaf_tags.json'), {})

    assign, edge = {}, set()
    for f in sorted(glob.glob(os.path.join(a.work, 'assign', '*.out.jsonl'))):
        for line in open(f, encoding='utf-8'):
            if not line.strip():
                continue
            r = json.loads(line)
            assign[r['seq']] = r['path']
            if r.get('edge'):
                edge.add(r['seq'])
    total = len(assign)
    assert total == len(items), f'assigned {total} items {len(items)}'

    by_path = collections.defaultdict(list)
    for s, p in assign.items():
        by_path[p].append(s)

    def ref(s, k=160):
        L, it = loc[s], items[s]
        return {'seq': s, 'case_id': L['case_id'], 'bad_case_id': L.get('bad_case_id', ''),
                'user_id': L.get('user_id', ''), 'session_id': L['session_id'], 'trace_id': L['trace_id'],
                'date': L['date'], 'src_file': L['file'], 'src_line': L['line'], 'json_path': L['json_path'],
                'problem_id': L['problem_id'], 'problem_index': L['problem_index'],
                'component': it['component'], 'label': it['label'], 'definition': it['definition'][:k]}

    def sample(seqs, n=5):
        out, seen = [], set()
        for s in sorted(seqs):
            if len(out) < n and items[s]['component'] in seen:
                continue
            seen.add(items[s]['component'])
            out.append(ref(s))
            if len(out) >= n:
                break
        have = {o['seq'] for o in out}
        for s in sorted(seqs):
            if len(out) >= n:
                break
            if s not in have:
                out.append(ref(s))
        return out

    def node_stat(seqs):
        return {'count': len(seqs),
                'sessions': len({items[s]['session_id'] for s in seqs}),
                'traces': len({items[s]['trace_id'] for s in seqs})}

    def meta_for(nid):
        if nid in nodes:
            n = nodes[nid]
            return {'name': n['name'], 'plain': n.get('plain', ''), 'definition': n.get('definition', ''),
                    'include': n.get('include', ''), 'exclude': n.get('exclude', ''),
                    'components': n.get('components', [])}
        if nid in l4nodes:
            n = l4nodes[nid]
            return {'name': n['name'], 'plain': n.get('plain', ''), 'definition': n.get('definition', ''),
                    'include': n.get('include', ''), 'exclude': n.get('exclude', ''), 'components': []}
        return {'name': nid.split('.')[-1], 'plain': '', 'definition': '', 'include': '', 'exclude': '', 'components': []}

    # group by 4 segments
    tree_paths = collections.defaultdict(list)
    for p, seqs in by_path.items():
        tree_paths[p] = seqs

    def build_l3(l1, l2, l3):
        kids = {}
        for p, seqs in by_path.items():
            parts = p.split('.')
            if len(parts) == 4 and parts[0] == l1 and parts[1] == l2 and f'{parts[0]}.{parts[1]}.{parts[2]}' == l3:
                kids[p] = seqs
        base = by_path.get(l3, [])
        node = {'id': l3, 'level': 3, **meta_for(l3), **node_stat(base + [s for v in kids.values() for s in v])}
        node['share'] = round(node['count'] / total, 4)
        node['tags'] = leaf_tags.get(l3, [])
        node['low_sample'] = node['count'] < LOW_SAMPLE
        node['recall'] = f'python3 bin/cluster.py recall --node {l3} --out .'
        node['samples'] = sample(base + [s for v in kids.values() for s in v])
        if kids:
            children = []
            for p, seqs in sorted(kids.items(), key=lambda kv: -len(kv[1])):
                c = {'id': p, 'level': 4, **meta_for(p), **node_stat(seqs)}
                c['share'] = round(c['count'] / total, 4)
                c['share_within_l3'] = round(c['count'] / max(1, node['count']), 3)
                c['samples'] = sample(seqs, 3)
                children.append(c)
            node['children'] = children
        return node

    l1_children = []
    for l1 in sorted({p.split('.')[0] for p in assign.values()}):
        l1_seqs = [s for s, p in assign.items() if p.startswith(l1 + '.')]
        l2_children = []
        for l2 in ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7']:
            l3ids = sorted({'.'.join(p.split('.')[:3]) for p in assign.values() if p.startswith(f'{l1}.{l2}.')})
            if not l3ids:
                continue
            kids = [build_l3(l1, l2, i) for i in l3ids]
            c = sum(k['count'] for k in kids)
            l2_children.append({'id': f'{l1}.{l2}', 'level': 2, 'name': inv['l2'][l2]['name'],
                                'mechanism': l2, 'definition': inv['l2'][l2]['definition'],
                                'count': c, 'share': round(c / total, 4),
                                'share_within_l1': round(c / max(1, len(l1_seqs)), 4),
                                'sessions': len({items[s]['session_id'] for s in l1_seqs if assign[s].startswith(f'{l1}.{l2}.')}),
                                'children': kids})
        l2_children.sort(key=lambda x: -x['count'])
        c = sum(k['count'] for k in l2_children)
        l1_children.append({'id': l1, 'level': 1, 'name': inv['l1'][l1]['name'],
                            'definition': inv['l1'][l1]['definition'],
                            'count': c, 'share': round(c / total, 4),
                            'sessions': len({items[s]['session_id'] for s in l1_seqs}),
                            'components': sorted({items[s]['component'] for s in l1_seqs}),
                            'children': l2_children})
    l1_children.sort(key=lambda x: -x['count'])

    # tags rollup
    tag_roll = collections.defaultdict(lambda: {'count': 0, 'sessions': set(), 'by_l1': collections.Counter()})
    for s, p in assign.items():
        for t in leaf_tags.get('.'.join(p.split('.')[:3]), []):
            tag_roll[t]['count'] += 1
            tag_roll[t]['sessions'].add(items[s]['session_id'])
            tag_roll[t]['by_l1'][p.split('.')[0]] += 1
    tags_out = [{'tag': t, 'name': tagdef.get(t, {}).get('name', t),
                 'definition': tagdef.get(t, {}).get('definition', ''),
                 'count': v['count'], 'sessions': len(v['sessions']),
                 'share': round(v['count'] / total, 4), 'by_l1': dict(v['by_l1'])}
                for t, v in sorted(tag_roll.items(), key=lambda kv: -kv[1]['count'])]

    mech = []
    for m in ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7']:
        sel = [p for p in assign.values() if p.split('.')[1] == m]
        if not sel:
            continue
        mech.append({'mechanism': m, 'name': inv['l2'][m]['name'], 'definition': inv['l2'][m].get('definition', ''), 'count': len(sel),
                     'share': round(len(sel) / total, 4),
                     'sessions': len({items[s]['session_id'] for s, p in assign.items() if p.split('.')[1] == m}),
                     'by_l1': {k: sum(1 for p in sel if p.startswith(k + '.')) for k in ['S1','S2','S3','S4','S5','S6','S7','S8','S9'] if any(p.startswith(k + '.') for p in sel)}})

    sessions = {i['session_id'] for i in items.values()}
    tree = {
        'meta': {
            'title': '归因问题聚类树',
            'generated_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'pipeline_version': jload(os.path.join(ROOT, 'VERSION.json'), {}).get('version', 'unknown'),
            'unit': '原子归因问题条目（每条只归属一个链路 component）',
            'total_items': total, 'total_traces': len({i['trace_id'] for i in items.values()}),
            'total_sessions': len(sessions), 'total_cases': len({i['case_id'] for i in items.values()}),
            'sessions_all': a.sessions_all,
            'depth': 4 if any(len(p.split('.')) == 4 for p in assign.values()) else 3,
            'edge_flagged': len(edge), 'edge_rate': round(len(edge) / max(1, total), 4),
            'axes': {'level_1': '环节：失败发生在链路哪一段（component 决定）',
                     'level_2': '失效机制：动作错在哪一层（7 类统一口径，按 M1→M7 取最早失效点）',
                     'level_3': '失效模式：可派工的动作级模式（互斥、穷尽）',
                     'level_4': '细分变体：仅在满足三条准入时下钻'},
            'counting': '主口径 = 会话数（同一会话同一叶子计 1）；附列 = 条目数',
            'low_sample_threshold': LOW_SAMPLE,
            'low_sample_rule': '条目数低于阈值的叶子保留在树中用于定位，但不参与排名比较（统计不稳定）',
            'tags': '横切标签，一条可多标签，不改变树的互斥性',
            'traceability': {'principle': '每个叶子带 samples，每条带 原文件+行号+JSON 路径',
                             'item_index': 'item_index.jsonl',
                             'recall_tool': 'python3 bin/cluster.py recall --work <work> --node <id>'},
        },
        'tree': {'id': 'root', 'name': '归因聚类', 'level': 0, 'count': total, 'share': 1.0,
                 'sessions': len(sessions), 'children': l1_children},
        'mechanism_rollup': mech,
        'tag_rollup': tags_out,
    }
    jdump(tree, os.path.join(a.out, 'cluster_tree.json'))

    with open(os.path.join(a.out, 'item_index.jsonl'), 'w', encoding='utf-8') as fh:
        for s in sorted(assign):
            r = dict(loc[s]); r['path'] = assign[s]; r['edge'] = s in edge
            r['definition'] = items[s]['definition']; r['reason'] = items[s]['reason']
            r['tags'] = leaf_tags.get('.'.join(assign[s].split('.')[:3]), [])
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')

    with open(os.path.join(a.out, 'leaves.tsv'), 'w', encoding='utf-8') as fh:
        fh.write('path\tl1\tl2\tname\titems\tsessions\tshare\ttags\n')
        for l1 in l1_children:
            for l2 in l1['children']:
                for l3 in l2['children']:
                    fh.write(f"{l3['id']}\t{l1['name']}\t{l2['name']}\t{l3['name']}\t{l3['count']}\t"
                             f"{l3['sessions']}\t{l3['share']:.4f}\t{','.join(l3.get('tags', []))}\n")
    print(f"items={total} sessions={len(sessions)} L1={len(l1_children)} "
          f"L3={sum(len(l2['children']) for l1 in l1_children for l2 in l1['children'])} "
          f"L4={sum(len(l3.get('children', [])) for l1 in l1_children for l2 in l1['children'] for l3 in l2['children'])} "
          f"edge={len(edge)}")


# ---------------------------------------------------------------- verify
def cmd_verify(a):
    tree = jload(os.path.join(a.out, 'cluster_tree.json'))
    errs = []
    total = tree['tree']['count']
    seen = collections.Counter()
    for l1 in tree['tree']['children']:
        if sum(x['count'] for x in l1['children']) != l1['count']:
            errs.append(f"{l1['id']} sum mismatch")
        for l2 in l1['children']:
            if sum(x['count'] for x in l2['children']) != l2['count']:
                errs.append(f"{l2['id']} sum mismatch")
            for l3 in l2['children']:
                if 'children' in l3 and sum(x['count'] for x in l3['children']) != l3['count']:
                    errs.append(f"{l3['id']} sum mismatch")
                seen[l3['id']] += l3['count']
                if not l3['samples']:
                    errs.append(f"{l3['id']} no samples")
    if sum(x['count'] for x in tree['tree']['children']) != total:
        errs.append('root sum mismatch')
    idx = [json.loads(l) for l in open(os.path.join(a.out, 'item_index.jsonl'), encoding='utf-8')]
    if len(idx) != total:
        errs.append(f'index {len(idx)} != {total}')
    if len({r['seq'] for r in idx}) != len(idx):
        errs.append('duplicate seq')
    if collections.Counter(r['path'] for r in idx) != seen:
        errs.append('index path counts != tree counts')
    # locator replay
    cache, mism = {}, 0
    for r in idx[:0]:
        pass
    for l3 in [l3 for l1 in tree['tree']['children'] for l2 in l1['children'] for l3 in l2['children']]:
        for s in l3['samples']:
            f = s['src_file'] if os.path.isabs(s.get('src_file', '')) else os.path.join(a.src, s['src_file'])
            if not os.path.exists(f):
                cand = glob.glob(os.path.join(a.src, '**', os.path.basename(s['src_file'])), recursive=True)
                f = cand[0] if cand else f
            if not os.path.exists(f):
                errs.append(f'src not found {s["src_file"]}')
                continue
            if f not in cache:
                cache[f] = read_lines(f)
            try:
                rec = json.loads(cache[f][s['src_line'] - 1])
                turn = rec['turns'][int(s['json_path'].split('[')[1].split(']')[0])]
                probs = as_list(turn.get('problem'))
                p = probs[s['problem_index']]
                if p['label'] != s['label']:
                    mism += 1
            except Exception as e:
                errs.append(f'locator broken seq={s["seq"]}: {e}')
    jdump({'leaves': len(seen), 'total': total, 'locator_mismatch': mism,
           'errors': len(errs), 'items': errs[:50]}, os.path.join(a.out, 'verify.json'))
    print(f'leaves={len(seen)} total={total} locator_mismatch={mism} errors={len(errs)}')
    for e in errs[:20]:
        print(' -', e)
    sys.exit(1 if errs else 0)


# ---------------------------------------------------------------- report

def write_brief(t, out):
    """One-page, plain-language brief. No L1/L2 codes, no internal jargon."""
    m, root = t['meta'], t['tree']
    inv, _, _ = taxonomy()
    leaves = [l3 for l1 in root['children'] for l2 in l1['children'] for l3 in l2['children']]
    ranked = sorted([x for x in leaves if not x.get('low_sample')], key=lambda x: -x['sessions'])[:3]
    low = [x for x in leaves if x.get('low_sample')]
    L = ['# 这批问题，最该先修什么', '',
         f"> 数据：{m['total_items']:,} 条问题，来自 {m['total_sessions']:,} 个会话"
         + (f"（全量 {m['sessions_all']:,} 个）" if m.get('sessions_all') else '') + '。',
         '> 一条问题只归一个类别；排名按会话数，不按条目数，避免“问题多的会话”放大。', '',
         '## 结论', '']
    for i, l3 in enumerate(ranked, 1):
        l1 = next(x for x in root['children'] if any(l3 in l2['children'] for l2 in x['children']))
        owner = l1['name']
        sample = (l3.get('samples') or [{}])[0]
        L += [f"### {i}. {l3['name']}（{l3['sessions']:,} 个会话，{l3['count']:,} 条）", '',
              f"- **用户会遇到什么**：{l3.get('plain', '') or l3.get('definition', '')}",
              f"- **一个真实例子**：{sample.get('label', '')}",
              f"- **该谁修**：{owner}",
              f"- **占全部条目**：{l3['share']:.1%}；同一会话只计 1 次。", '']
    mech = sorted([dict(r, definition=r.get('definition') or inv['l2'].get(r['mechanism'], {}).get('definition', ''))
                   for r in t['mechanism_rollup']], key=lambda r: -r.get('sessions', 0))
    L += ['## 失效停在哪一层', '',
          '| 机制 | 条目 | 会话 | 占比 | 用大白话说 |', '|---|---|---|---|---|']
    for r in mech[:4]:
        L.append(f"| {r['name']} | {r['count']:,} | {r.get('sessions', 0):,} | {r['share']:.1%} | {r.get('definition', '')} |")
    top = mech[0] if mech else None
    if top:
        L += ['', f"最多的问题落在「{top['name']}」：{top.get('definition','')}"]
    v = jload(os.path.join(out, 'verify.json'), {})
    if m['edge_rate'] < 0.01:
        edge_line = (f"- 边界标记：{m['edge_flagged']:,} 条（{m['edge_rate']:.1%}）。默认快速模式不跑双遍，"
                     "这个数偏低；需要可靠的边界率请加 `--consensus 2` 重跑。")
    else:
        edge_line = f"- 边界不清：{m['edge_flagged']:,} 条（{m['edge_rate']:.1%}）标了 `edge`，优先复核这些。"
    L += ['', '## 这份结果怎么读', '',
          f"- 归类覆盖：{m['total_items']:,} 条全部有且只有一个类别（100%）。",
          edge_line,
          f"- 稀有类别：{len(low)} 个类别少于 {m.get('low_sample_threshold', 10)} 条，保留用于定位，不参与排名。",
          (f"- 自动校验：{v.get('leaves', 0)} 个类别，抽样回原文比对 {v.get('locator_mismatch', 0)} 条不一致，"
           f"结构错误 {v.get('errors', 0)} 个。" if v else "- 自动校验：见 `verify.json`。"),
          '', '完整明细看 `cluster_summary.md`（分布）和 `leaves.tsv`（每个类别的条目数/会话数）。', '']
    open(os.path.join(out, 'cluster_brief.md'), 'w', encoding='utf-8').write('\n'.join(L))


def cmd_report(a):
    t = jload(os.path.join(a.out, 'cluster_tree.json'))
    m, root = t['meta'], t['tree']
    L = [f"# {m['title']}\n",
         f"- 单元：{m['unit']}",
         f"- 规模：{m['total_items']:,} 条问题 / {m['total_traces']:,} trace / {m['total_sessions']:,} session"
         + (f"（全量 {m['sessions_all']:,}）" if m.get('sessions_all') else ''),
         f"- 结构：{len(root['children'])} 环节 · "
         f"{sum(len(c['children']) for c in root['children'])} 机制节点 · "
         f"{sum(len(l2['children']) for l1 in root['children'] for l2 in l1['children'])} 模式"
         + (f" · {sum(len(l3.get('children', [])) for l1 in root['children'] for l2 in l1['children'] for l3 in l2['children'])} 细分"
            if m['depth'] >= 4 else ''),
         f"- 边界标记：{m['edge_flagged']:,} 条（{m['edge_rate']:.1%}）",
         f"- 计数口径：{m['counting']}\n"]

    L += ['## 环节分布\n', '| L1 | 名称 | 条目 | 会话 | 占比 | 主导机制 |', '|---|---|---|---|---|---|']
    for l1 in root['children']:
        top = l1['children'][0]
        L.append(f"| {l1['id']} | {l1['name']} | {l1['count']:,} | {l1['sessions']:,} | {l1['share']:.1%} | "
                 f"{top['name']} {top['share_within_l1']:.0%} |")

    L += ['\n## 机制分布（跨环节）\n', '| 机制 | 名称 | 条目 | 会话 | 占比 |', '|---|---|---|---|---|']
    for r in t['mechanism_rollup']:
        L.append(f"| {r['mechanism']} | {r['name']} | {r['count']:,} | {r.get('sessions',0):,} | {r['share']:.1%} |")

    L += ['\n## 横切标签\n', '| 标签 | 条目 | 会话 | 占比 | 分布 |', '|---|---|---|---|---|']
    for r in t.get('tag_rollup', []):
        dist = ' '.join(f'{k}:{v}' for k, v in sorted(r['by_l1'].items(), key=lambda kv: -kv[1]))
        L.append(f"| {r['name']} | {r['count']:,} | {r['sessions']:,} | {r['share']:.1%} | {dist} |")

    low = [l3 for l1 in root['children'] for l2 in l1['children'] for l3 in l2['children'] if l3.get('low_sample')]
    L += ['\n## 低样本叶子（不参与排名）\n',
          f"条目数 < {m.get('low_sample_threshold', 10)} 的叶子共 **{len(low)}** 个，覆盖 "
          f"**{sum(x['count'] for x in low):,}** 条（{sum(x['count'] for x in low)/m['total_items']:.1%}）。",
          '它们保留在树里用于定位，单独看排名没有统计意义；比较请看 L2 机制层。\n']
    L += ['\n## 各环节 Top 模式（按会话数，已排除低样本叶）\n']
    for l1 in root['children']:
        L.append(f"### {l1['id']} {l1['name']}（{l1['count']:,} 条 / {l1['sessions']:,} 会话）\n")
        L += ['| 叶子 | 模式 | 条目 | 会话 | 一句话 |', '|---|---|---|---|---|']
        rows = sorted([x for l2 in l1['children'] for x in l2['children'] if not x.get('low_sample')],
                      key=lambda x: -x['sessions'])[:8]
        for l3 in rows:
            L.append(f"| {l3['id']} | {l3['name']} | {l3['count']:,} | {l3['sessions']:,} | {l3.get('plain','')} |")
        L += ['']
    open(os.path.join(a.out, 'cluster_summary.md'), 'w', encoding='utf-8').write('\n'.join(L))

    # ascii tree
    out = [f"{m['title']}  {m['total_items']:,} 条 / {m['total_sessions']:,} 会话"]

    def pad(s, w=34):
        return s + ' ' * max(1, w - sum(2 if ord(c) > 127 else 1 for c in s))
    for i, l1 in enumerate(root['children']):
        last1 = i == len(root['children']) - 1
        out.append('')
        out.append(f"{'└─' if last1 else '├─'} {l1['id']} {l1['name']}  {l1['count']:,} 条 / {l1['sessions']:,} 会话")
        p1 = '   ' if last1 else '│  '
        for j, l2 in enumerate(l1['children']):
            last2 = j == len(l1['children']) - 1
            out.append(f"{p1}{'└─' if last2 else '├─'} {l2['mechanism']} {l2['name']}  {l2['count']:,}")
            p2 = p1 + ('   ' if last2 else '│  ')
            for k, l3 in enumerate(l2['children']):
                last3 = k == len(l2['children']) - 1
                out.append(f"{p2}{'└─' if last3 else '├─'} {pad(l3['id'] + ' ' + l3['name'])} "
                           f"{l3['count']:>5} 条 / {l3['sessions']:>4} 会话")
                if l3.get('children'):
                    p3 = p2 + ('   ' if last3 else '│  ')
                    for n, l4 in enumerate(l3['children']):
                        out.append(f"{p3}{'└─' if n == len(l3['children']) - 1 else '├─'} "
                                   f"{pad(l4['name'], 30)} {l4['count']:>5} 条 / {l4['sessions']:>4} 会话")
    open(os.path.join(a.out, 'cluster_tree.txt'), 'w', encoding='utf-8').write('\n'.join(out))
    write_brief(t, a.out)
    print('report written')


# ---------------------------------------------------------------- chart
def cmd_chart(a):
    from PIL import Image, ImageDraw, ImageFont
    t = jload(os.path.join(a.out, 'cluster_tree.json'))
    L1S, MECH = t['tree']['children'], ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7']
    MNAME = {k: f"{k} {v['name']}" for k, v in jload(os.path.join(ROOT, 'taxonomy', 'inventory.json'))['l2'].items()}
    mat = {(l1['id'], l2['mechanism']): l2['count'] for l1 in L1S for l2 in l1['children']}
    vmax = max(mat.values()) or 1
    import math
    F = a.font
    f_t, f_s, f_c, f_r = (ImageFont.truetype(F, s) for s in (26, 15, 16, 15))
    W, H = 1500, 130 + len(L1S) * 68 + 60
    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img)
    d.text((40, 26), f"A 环节 × 失效机制 交叉矩阵（{t['meta']['total_items']:,} 条）", font=f_t, fill=(15, 23, 42))
    x0, y0, cw, ch = 210, 110, 168, 68
    for j, m in enumerate(MECH):
        d.text((x0 + j * cw + 12, y0 - 28), MNAME[m], font=f_r, fill=(51, 65, 85))
    for i, l1 in enumerate(L1S):
        y = y0 + i * ch
        d.text((40, y + 16), f"{l1['id']} {l1['name']}", font=f_r, fill=(15, 23, 42))
        d.text((40, y + 36), f"{l1['count']:,} 条 / {l1['sessions']:,} 会话", font=f_s, fill=(100, 116, 139))
        for j, m in enumerate(MECH):
            v = mat.get((l1['id'], m), 0)
            x = x0 + j * cw
            if v:
                al = math.sqrt(v / vmax)
                col = tuple(int(b + (dp - b) * al) for b, dp in zip((232, 240, 254), (30, 64, 175)))
                d.rectangle([x, y, x + cw - 6, y + ch - 6], fill=col)
                s = f'{v:,}'
                bb = d.textbbox((0, 0), s, font=f_c)
                d.text((x + (cw - 6 - bb[2]) / 2, y + (ch - 6 - bb[3]) / 2 - 3), s, font=f_c,
                       fill='white' if al > 0.55 else (30, 41, 59))
            else:
                d.rectangle([x, y, x + cw - 6, y + ch - 6], outline=(226, 232, 240))
    d.text((40, H - 40), '口径：条目数；会话数见各叶子。', font=f_s, fill=(148, 163, 184))
    img.save(os.path.join(a.out, 'cluster_matrix.png'))
    print('chart saved')


# ---------------------------------------------------------------- syndrome
def cmd_syndrome(a):
    inv, _, _ = taxonomy()
    tree = jload(os.path.join(a.out, 'cluster_tree.json'))
    idx = [json.loads(l) for l in open(os.path.join(a.out, 'item_index.jsonl'), encoding='utf-8')]
    per = collections.defaultdict(list)
    for r in idx:
        per[r['session_id']].append(r)
    l2name = {k: v['name'] for k, v in inv['l2'].items()}
    combos = collections.Counter()
    for sid, rows in per.items():
        ms = tuple(sorted({'/'.join(x['path'].split('.')[1:2]) for x in rows}))
        combos[ms] += 1
    pairs = collections.Counter()
    for sid, rows in per.items():
        leaves = sorted({'.'.join(x['path'].split('.')[:3]) for x in rows})
        for i in range(len(leaves)):
            for j in range(i + 1, len(leaves)):
                pairs[(leaves[i], leaves[j])] += 1
    L = ['# 会话级失效组合（syndrome）\n',
         f"- 会话总数：{len(per):,}；平均每会话 {sum(len(v) for v in per.values()) / max(1, len(per)):.1f} 条问题",
         '- 组合 = 同一会话内出现的失效机制集合（L2）\n',
         '## 最常见机制组合\n', '| 组合 | 会话数 | 占比 |', '|---|---|---|']
    for ms, n in combos.most_common(15):
        L.append(f"| {' + '.join(l2name.get(m, m) for m in ms)} | {n} | {n/len(per):.0%} |")
    L += ['\n## 最常同时出现的模式对\n', '| 模式 A | 模式 B | 共同会话 |', '|---|---|---|']
    name = {}
    for l1 in tree['tree']['children']:
        for l2 in l1['children']:
            for l3 in l2['children']:
                name[l3['id']] = l3['name']
    for (p1, p2), n in pairs.most_common(15):
        L.append(f"| {p1} {name.get(p1,'')} | {p2} {name.get(p2,'')} | {n} |")
    open(os.path.join(a.out, 'syndrome.md'), 'w', encoding='utf-8').write('\n'.join(L))
    jdump({'combos': [{'mechanisms': list(ms), 'sessions': n} for ms, n in combos.most_common(50)],
           'pairs': [{'a': p1, 'b': p2, 'sessions': n} for (p1, p2), n in pairs.most_common(50)]},
          os.path.join(a.out, 'syndrome.json'))
    print('syndrome written')


# ---------------------------------------------------------------- recall
def cmd_recall(a):
    idx = [json.loads(l) for l in open(os.path.join(a.out, 'item_index.jsonl'), encoding='utf-8')]
    hits = [r for r in idx if a.node == 'root' or r['path'] == a.node or r['path'].startswith(a.node + '.')]
    hits.sort(key=lambda r: r['seq'])
    print(f"node={a.node} matched={len(hits)} sessions={len({h['session_id'] for h in hits})}")
    cache = {}
    for r in hits[:a.limit]:
        print('-' * 96)
        print(f"[{r['seq']}] {r['date']}  {r['file']}:{r['line']}  {r['json_path']}")
        print(f"      case={r['case_id']} trace={r['trace_id']} session={r['session_id']}")
        print(f"      component={r['component']}  path={r['path']}  tags={','.join(r.get('tags', []))}")
        print(f"      label: {r['label']}")
        if a.full:
            print(f"      definition: {r['definition'][:1000]}")
            print(f"      reason: {r['reason'][:1000]}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('ingest'); p.add_argument('--input', required=True); p.add_argument('--cases')
    p.add_argument('--channel', default='auto', choices=['auto', 'wencai', 'aime'])
    p.add_argument('--out', required=True); p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser('chunk'); p.add_argument('--work', required=True); p.add_argument('--size', type=int, default=200)
    p.set_defaults(fn=cmd_chunk)
    p = sub.add_parser('aggregate'); p.add_argument('--work', required=True); p.add_argument('--out', required=True)
    p.add_argument('--sessions-all', type=int, default=0, dest='sessions_all'); p.set_defaults(fn=cmd_aggregate)
    p = sub.add_parser('verify'); p.add_argument('--work', required=True); p.add_argument('--out', required=True)
    p.add_argument('--src', required=True); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser('report'); p.add_argument('--out', required=True); p.set_defaults(fn=cmd_report)
    p = sub.add_parser('chart'); p.add_argument('--out', required=True)
    p.add_argument('--font', default=os.environ.get('CLUSTER_CHART_FONT', '/System/Library/Fonts/Hiragino Sans GB.ttc'))
    p.set_defaults(fn=cmd_chart)
    p = sub.add_parser('syndrome'); p.add_argument('--out', required=True); p.set_defaults(fn=cmd_syndrome)
    p = sub.add_parser('recall'); p.add_argument('--work'); p.add_argument('--out'); p.add_argument('--node', required=True)
    p.add_argument('--limit', type=int, default=5); p.add_argument('--full', action='store_true')
    p.set_defaults(fn=cmd_recall)

    a = ap.parse_args()
    if a.cmd == 'recall' and not a.out:
        a.out = a.work or '.'
    a.fn(a)


if __name__ == '__main__':
    main()
