#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 run 基线里的绝对路径迁移到另一台机器。

为什么需要：merge_runs 会把 items/locators 的 src_path 与 file 统一写成
跑批机器上的绝对路径。基线搬到服务器后这些路径不存在，增量合并出来的树在
verify 阶段会找不到源文件（locator_mismatch / src not found）。

本工具只改路径，不动任何判定与 seq，可重复执行（路径已对就是 no-op）。

用法：
  python3 bin/rebase_paths.py --run runs \\
      --datasets /mnt/fin_bench/sync_datasets/datasets \\
      --repo /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm

规则：
  .../<任意前缀>/datasets/<尾>          -> <datasets>/<尾>
  .../<任意前缀>/runs/_src/<尾>         -> <repo>/runs/_src/<尾>
"""
import argparse, glob, json, os, re, sys

DS_MARK = '/datasets/'
SRC_MARK = '/runs/_src/'


def remap(path, datasets, repo):
    if not path or not os.path.isabs(path):
        return path
    if DS_MARK in path:
        tail = path.split(DS_MARK, 1)[1]
        return os.path.join(datasets, tail)
    if SRC_MARK in path:
        tail = path.split(SRC_MARK, 1)[1]
        return os.path.join(repo, 'runs', '_src', tail)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True, help='run 根目录（含 <树>/run_merged/）或单个 run 目录')
    ap.add_argument('--datasets', required=True, help='目标机器上的 datasets 根')
    ap.add_argument('--repo', required=True, help='目标机器上的仓库根')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    dirs = sorted({os.path.dirname(p) for pat in ('items.jsonl', '*/items.jsonl', '*/run*/items.jsonl')
                   for p in glob.glob(os.path.join(a.run, pat))})
    if not dirs:
        sys.exit(f'没找到 items.jsonl：{a.run}')

    total = changed = 0
    for d in dirs:
        for name in ('items.jsonl', 'locators.jsonl'):
            p = os.path.join(d, name)
            if not os.path.exists(p):
                continue
            rows, k = [], 0
            for line in open(p, encoding='utf-8'):
                if not line.strip():
                    continue
                r = json.loads(line)
                for f in ('src_path', 'file'):
                    if f in r and r[f]:
                        new = remap(r[f], a.datasets, a.repo)
                        if new != r[f]:
                            r[f] = new
                            k += 1
                rows.append(r)
            total += len(rows)
            if k and not a.dry:
                with open(p, 'w', encoding='utf-8') as fh:
                    for r in rows:
                        fh.write(json.dumps(r, ensure_ascii=False) + '\n')
            changed += k
            if k:
                print(f'{p}: 改 {k} 处')
    print(f'{"[dry] " if a.dry else ""}扫描 {len(dirs)} 个 run，{total} 条记录，改路径 {changed} 处')
    if changed == 0:
        print('已经是目标机器的路径，无需修改')


if __name__ == '__main__':
    main()
