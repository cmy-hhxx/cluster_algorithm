#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发现要聚类的树（渠道 × 输入目录），并给 AIME 做「日常 + 流失」staging。

为什么需要它：以前树表写死在 bin/run_trees.sh 里（问财 4 棵 + AIME 2 棵）。两个渠道拆到
两台机器后树表必须按渠道生成；更关键的是 2026-11-03 `1_task_eval/0_search_eval` 下新增的
6 个 aa-* 模型因为写死而被整批漏掉——所以改成扫目录，新增模型自动进树。

用法：
  python3 bin/discover_trees.py --channel wencai --wencai-root <.../0_wencai/eval_output>
  python3 bin/discover_trees.py --channel aime   --aime-root   <.../1_aime/eval_output> \
                                [--staging <repo>/runs/_src]

输出 TSV（一行一棵树，字段用 TAB 分隔，调用方别用空格切）：
  tree  channel  src  deliver  src_root

树名规则：
  问财  wencai_<model>（a_fast → wencai_a_fast；aa-fast-base-search → wencai_aa-fast-base-search）
  AIME  <model>       （aime_fast / aime_expert；日常 + 流失合成一棵）

src_root 是 verify 的回放根：ingest 把 file 记成「父目录名/文件名」，所以 src_root 恒为 src 的
父目录；AIME 例外——src 是 staging 目录，src_root 就是 staging 目录本身。
"""
import argparse, glob, os, shutil, sys

# 这些目录不是「模型目录」，扫到就跳过：评测流水线自己的状态目录、历史归档、以及聚类产物本身
SKIP = {'.runs', '.delivery', '.archive', '.git', '.staging', 'cluster',
        'comparison', 'criteria', 'logs', '__pycache__'}

AIME_GROUPS = ('0_daily_eval', '1_lost_user_eval')


def attribution_files(d):
    return sorted(glob.glob(os.path.join(d, '*attribution*.jsonl')))


def models_in(group):
    """group 下直接含 attribution 的子目录就是模型目录。"""
    out = []
    try:
        names = sorted(os.listdir(group))
    except OSError:
        return out
    for name in names:
        if name.startswith('.') or name in SKIP:
            continue
        d = os.path.join(group, name)
        if os.path.isdir(d) and attribution_files(d):
            out.append((name, d))
    return out


def wencai_groups(root):
    """问财的「组」可能是一层（0_daily_eval）也可能是两层（1_task_eval/0_search_eval）。"""
    groups = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return groups
    for name in names:
        if name.startswith('.') or name in SKIP:
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        # 先当一层组
        if models_in(d):
            groups.append(d)
            continue
        # 再往下探一层
        try:
            subs = sorted(os.listdir(d))
        except OSError:
            continue
        for s in subs:
            if s.startswith('.') or s in SKIP:
                continue
            sd = os.path.join(d, s)
            if os.path.isdir(sd) and models_in(sd):
                groups.append(sd)
    return groups


def stage_aime(root, staging):
    """把日常 + 流失两处 attribution 软链到同一个 staging 目录（ingest 只吃单目录）。"""
    rows, seen = [], {}
    for g in AIME_GROUPS:
        gd = os.path.join(root, g)
        if not os.path.isdir(gd):
            continue
        for name, _ in models_in(gd):
            seen.setdefault(name, []).append(g)
    for model in sorted(seen):
        tree = model
        d = os.path.join(staging, tree)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(os.path.join(d, 'daily'), exist_ok=True)
        os.makedirs(os.path.join(d, 'lost'), exist_ok=True)
        n = 0
        for tag, g in (('daily', '0_daily_eval'), ('lost', '1_lost_user_eval')):
            for f in attribution_files(os.path.join(root, g, model)):
                # 必须用绝对路径：软链存的是相对路径时按「软链所在目录」解析，多了 daily/ 一级就断了
                os.symlink(os.path.abspath(f), os.path.join(d, tag, os.path.basename(f)))
                n += 1
        if not n:
            continue
        rows.append((tree, 'aime', d, os.path.join(root, 'cluster', model), d))
    return rows


def discover(channel, wencai_root, aime_root, staging):
    rows = []
    if channel in ('auto', 'wencai') and wencai_root:
        for group in wencai_groups(wencai_root):
            for model, src in models_in(group):
                rows.append((f'wencai_{model}', 'wencai', src,
                             os.path.join(group, 'cluster', model), group))
    if channel in ('auto', 'aime') and aime_root:
        rows += stage_aime(aime_root, staging)
    # 同名树会让 runs/<树> 互相覆盖，宁可报错也不要悄悄少跑一棵
    dup = {r[0] for r in rows if [x[0] for x in rows].count(r[0]) > 1}
    if dup:
        sys.exit(f'树名重复，请先改名：{", ".join(sorted(dup))}')
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--channel', default='auto', choices=['auto', 'wencai', 'aime'])
    ap.add_argument('--wencai-root', default='', help='<...>/0_wencai/eval_output')
    ap.add_argument('--aime-root', default='', help='<...>/1_aime/eval_output')
    ap.add_argument('--staging', default='runs/_src', help='AIME 日常+流失软链目录（默认 runs/_src）')
    a = ap.parse_args()
    if a.channel in ('auto', 'wencai') and not a.wencai_root:
        sys.exit('缺少 --wencai-root')
    if a.channel in ('auto', 'aime') and not a.aime_root:
        sys.exit('缺少 --aime-root')
    os.makedirs(a.staging, exist_ok=True)
    rows = discover(a.channel, a.wencai_root, a.aime_root, a.staging)
    for r in rows:
        print('\t'.join(r))
    if not rows:
        print(f'# 未发现任何树（channel={a.channel}）', file=sys.stderr)
    return 0 if rows else 3


if __name__ == '__main__':
    sys.exit(main())
