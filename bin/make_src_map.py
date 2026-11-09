#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成交付目录里的 src_map.json：把 item_index / cluster_tree 里的相对 src_file
映射回原始文件的绝对路径。

为什么需要它：`file` 字段只保留「父目录名/文件名」（如 daily/xxx.jsonl），AIME 那种
「日常+流失」拼出来的 run（输入根是软链目录）光看 src_file 回不到原始文件；而绝对路径
会随机器/挂载点变，所以交付物里不写死绝对路径，每次交付用本文件重新生成映射。
`src_root` 取所有原始文件所在目录的公共根（AIME 就是 <AIME_ROOT>，问财是组目录），
换机器后按 src_root ⟶ 实际数据根平移即可。

用法：python3 bin/make_src_map.py --run <run_dir> --out <out_dir> [--version 4.1.1]
"""
import argparse, json, os, sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True, help='run 目录（含 items.jsonl）')
    ap.add_argument('--out', required=True, help='交付目录（写 src_map.json）')
    ap.add_argument('--version', default='')
    a = ap.parse_args()

    ipath = os.path.join(a.run, 'items.jsonl')
    if not os.path.exists(ipath):
        sys.exit(f'缺少 {ipath}')

    files, real_dirs = {}, set()
    for line in open(ipath, encoding='utf-8'):
        if not line.strip():
            continue
        it = json.loads(line)
        if not it.get('src_path'):
            continue
        rp = os.path.realpath(it['src_path'])
        files.setdefault(it['file'], rp)
        real_dirs.add(os.path.dirname(rp))
    if not files:
        sys.exit(f'{ipath} 里没有 src_path，无法生成 src_map')

    # 数据根：所有原始文件所在目录的公共前缀（AIME 日常+流失合并后即 <AIME_ROOT>）
    root = os.path.commonpath(sorted(real_dirs)) if len(real_dirs) > 1 else sorted(real_dirs)[0]

    obj = {
        'note': 'item_index.jsonl / cluster_tree.json 里的 src_file/file 是相对路径（保留 daily/lost 一级）；这里把每个 src_file 映射到原始文件绝对路径，src_root 是这些原始文件的公共根（换机器时按新数据根平移）。',
        'src_root': root,
        'files': {k: files[k] for k in sorted(files)},
    }
    if a.version:
        obj['taxonomy_version'] = a.version

    os.makedirs(a.out, exist_ok=True)
    json.dump(obj, open(os.path.join(a.out, 'src_map.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'src_map written: {len(obj["files"])} files -> {a.out}/src_map.json')


if __name__ == '__main__':
    main()
