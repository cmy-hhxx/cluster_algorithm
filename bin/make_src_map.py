#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成交付目录里的 src_map.json：把 item_index / cluster_tree 里的相对 src_file
映射回原始文件的绝对路径。

为什么需要它：`file` 字段只保留「父目录名/文件名」，AIME 那种「日常+流失」拼出来的
run（输入根是临时目录或软链目录）光看 src_file 回不到原始文件；换机器跑后绝对路径
也会变，所以每次交付都重新生成一份。

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

    files, roots = {}, set()
    for line in open(ipath, encoding='utf-8'):
        if not line.strip():
            continue
        it = json.loads(line)
        src = os.path.abspath(it['src_path'])
        files.setdefault(it['file'], os.path.realpath(src))
        roots.add(os.path.dirname(src))

    # 输入根：多目录拼接（如 AIME 日常+流失）时取各父目录的公共前缀
    root = os.path.commonpath(sorted(roots)) if len(roots) > 1 else sorted(roots)[0]

    obj = {
        'note': 'item_index.jsonl / cluster_tree.json 里的 src_file 是相对 run 输入根的路径，这里给出其原始文件绝对路径。',
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
