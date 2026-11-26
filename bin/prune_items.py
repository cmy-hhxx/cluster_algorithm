#!/usr/bin/env python3
"""按 case_id 清单 / 自证会话从一棵树的 run 目录里删除条目（上游判定脏数据后的对账清理）。

只做数据层面的删除：不动分类法、不重判、不新建 run。删完由 ./run.sh finish 重新聚合、
校验与交付（aggregate 会断言 assign 行数 == items 行数，所以三处必须一起删）。

用法：
  python3 bin/prune_items.py --work runs/aime_expert/run_merged --drop-ids dirty.txt --reason "..."
  python3 bin/prune_items.py --work runs/aime_expert/run_merged --self-session --reason "..."

--self-session  删「session_id == trace_id」的条目（ingest 对上游 session_id=null 的兜底痕迹）
--dry-run       只统计，不落盘
留痕：<work>/prune.jsonl（append）；备份：<work>/.prune-<ts>/（items/locators/assign 原件）
"""
import argparse
import collections
import datetime
import json
import os
import shutil
import sys


def read_ids(path):
    with open(path, encoding='utf-8') as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def stream(path, dropped_seqs, counter, sel):
    """按 sel(item)->(bool 保留, 原因) 过滤一行一 JSON 的文件；counter 统计删除依据。"""
    tmp = path + '.tmp'
    n_keep = n_drop = 0
    with open(path, encoding='utf-8') as src, open(tmp, 'w', encoding='utf-8') as dst:
        for line in src:
            if not line.strip():
                continue
            d = json.loads(line)
            ok, why = sel(d)
            if ok:
                dst.write(line)
                n_keep += 1
            else:
                dropped_seqs.add(d.get('seq'))
                counter[why] += 1
                n_drop += 1
    os.replace(tmp, path)
    return n_keep, n_drop


def filter_assign(work, dropped_seqs, counter):
    """删掉 assign/ 里被删条目的判定行，返回 (文件数, 总保留数, 总删除数, {文件名: 保留数})。

    L4 上线后 assign/ 不再只有一个文件：merged.out.jsonl 与 items 一一对应，
    zz_l4.out.jsonl 只覆盖 L4 涉及的条目（items 的子集）。所以"assign 行数 == items 行数"
    只对主文件成立；其余文件只需保证不残留被删的 seq。旧版把两个文件加总算 keep，
    在有 zz_l4 的树上必然报不一致（2026-11-26 r533 剔除时命中过）。
    """
    n_files = n_keep = n_drop = 0
    per_file = {}
    adir = os.path.join(work, 'assign')
    for name in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
        if not name.endswith('.out.jsonl'):
            continue
        p = os.path.join(adir, name)
        tmp = p + '.tmp'
        k = dr = 0
        with open(p, encoding='utf-8') as src, open(tmp, 'w', encoding='utf-8') as dst:
            for line in src:
                if not line.strip():
                    continue
                seq = json.loads(line).get('seq')
                if seq in dropped_seqs:
                    dr += 1
                    counter['assign_row'] += 1
                else:
                    dst.write(line)
                    k += 1
        os.replace(tmp, p)
        n_files += 1
        n_keep += k
        n_drop += dr
        per_file[name] = k
    return n_files, n_keep, n_drop, per_file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True, help='run 目录（含 items.jsonl / locators.jsonl / assign）')
    ap.add_argument('--drop-ids', help='case_id 清单文件，一行一个')
    ap.add_argument('--self-session', action='store_true', help='删 session_id == trace_id 的条目')
    ap.add_argument('--reason', default='', help='写进 prune.jsonl 的原因')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if not a.drop_ids and not a.self_session:
        sys.exit('必须给 --drop-ids 或 --self-session')

    ids = read_ids(a.drop_ids) if a.drop_ids else set()
    items_p = os.path.join(a.work, 'items.jsonl')
    loc_p = os.path.join(a.work, 'locators.jsonl')
    for p in (items_p, loc_p):
        if not os.path.exists(p):
            sys.exit(f'缺少 {p}')

    def sel(d):
        if a.self_session and d.get('session_id') and d['session_id'] == d.get('trace_id'):
            return False, 'self_session'
        if d.get('case_id') and d['case_id'] in ids:
            return False, 'case_id'
        return True, ''

    # 先统计（dry-run 也走这条路径，但不动文件）
    if a.dry_run:
        counter = collections.Counter()
        seqs = set()
        n_items = n_drop = 0
        for line in open(items_p, encoding='utf-8'):
            if not line.strip():
                continue
            d = json.loads(line)
            n_items += 1
            ok, why = sel(d)
            if not ok:
                seqs.add(d.get('seq'))
                counter[why] += 1
        print(f'[dry-run] items={n_items} 待删={sum(counter.values())} {dict(counter)}')
        return 0

    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    bdir = os.path.join(a.work, f'.prune-{ts}')
    os.makedirs(bdir, exist_ok=True)
    shutil.copy2(items_p, os.path.join(bdir, 'items.jsonl'))
    shutil.copy2(loc_p, os.path.join(bdir, 'locators.jsonl'))
    if os.path.isdir(os.path.join(a.work, 'assign')):
        shutil.copytree(os.path.join(a.work, 'assign'), os.path.join(bdir, 'assign'))

    counter = collections.Counter()
    seqs = set()
    # items 与 locators 用同一判据（locators 同样带 case_id / session_id / trace_id）
    n_items, n_drop = stream(items_p, seqs, counter, sel)
    n_loc_keep, n_loc_drop = stream(loc_p, set(), collections.Counter(), sel)
    n_files, n_as_keep, n_as_drop, per_file = filter_assign(a.work, seqs, counter)

    # 主文件（merged.out.jsonl）必须与 items 一一对应；zz_l4.out.jsonl 这类附加文件是子集，
    # 只校验"没有残留被删 seq"（filter_assign 已保证）与"不超过 items 数"。
    primary = 'merged.out.jsonl' if 'merged.out.jsonl' in per_file else (sorted(per_file)[0] if per_file else None)
    if primary and per_file[primary] != n_items:
        sys.exit(f'不一致：items 剩 {n_items}，{primary} 剩 {per_file[primary]}（备份在 {bdir}，请人工核查）')
    for name, k in per_file.items():
        if name != primary and k > n_items:
            sys.exit(f'不一致：附加文件 {name} 剩 {k} 条，多于 items {n_items}（备份在 {bdir}）')
    if n_loc_drop != n_drop:
        sys.exit(f'不一致：items 删 {n_drop}，locators 删 {n_loc_drop}（备份在 {bdir}）')

    rec = {'at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
           'work': os.path.abspath(a.work), 'reason': a.reason,
           'selectors': {'self_session': a.self_session, 'drop_ids': a.drop_ids or ''},
           'dropped_items': n_drop, 'left_items': n_items,
           'by': dict(counter), 'assign_files': n_files, 'backup': bdir}
    with open(os.path.join(a.work, 'prune.jsonl'), 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    per_txt = '，'.join(f'{k}={v}' for k, v in sorted(per_file.items())) or '无'
    print(f'已删 {n_drop} 条，剩 {n_items} 条（locators {n_loc_keep}，assign {n_files} 个文件：{per_txt}）')
    print(f'备份：{bdir}')
    print(f'留痕：{os.path.join(a.work, "prune.jsonl")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
