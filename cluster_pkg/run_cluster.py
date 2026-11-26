#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 入口：外部业务方只需执行 `python run_cluster.py -c config.yaml`。"""
import argparse
import json
import os
import sys

# 无论在 cluster_pkg 目录内执行 `python run_cluster.py -c ...`，
# 还是从任意外部目录执行 `python /path/to/cluster_pkg/run_cluster.py -c ...`，都能稳定导入包。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

from cluster_pkg.cfg import ClusterConfig                   # noqa: E402
from cluster_pkg.engine import ClusterEngine, run_cluster    # noqa: E402
from cluster_pkg.pruner import prune_tree                    # noqa: E402
from cluster_pkg.yaml_lite import load_config_file           # noqa: E402

__all__ = ["ClusterConfig", "ClusterEngine", "run_cluster", "prune_tree", "main"]


def _cmd_recall(tree_dir: str, node: str, limit: int = 5):
    idx_path = os.path.join(tree_dir, "item_index.jsonl")
    if not os.path.isfile(idx_path):
        sys.exit(f"未找到索引文件: {idx_path}")
    hits = []
    with open(idx_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            p = r.get("path", "")
            if node == "root" or p == node or p.startswith(node + "."):
                hits.append(r)
    hits.sort(key=lambda r: r["seq"])
    print(f"node={node} matched={len(hits)} sessions={len({h['session_id'] for h in hits})}")
    for r in hits[:limit]:
        print("-" * 88)
        print(f"[{r['seq']}] trace={r['trace_id']} session={r['session_id']} severity={r.get('severity', '')}")
        if r.get("question"):
            print(f"  question : {r['question'][:160]}")
        print(f"  component: {r.get('component', '')} | path={r['path']} | label={r.get('label', '')}")
        print(f"  definition: {(r.get('definition') or '')[:300]}")


def _resolve_tree_dir_from_args(args) -> str:
    if args.tree_dir:
        return os.path.abspath(os.path.expanduser(args.tree_dir))
    if args.config:
        cfg_path = os.path.abspath(os.path.expanduser(args.config))
        raw = load_config_file(cfg_path)
        bdir = os.path.dirname(cfg_path)
        td = raw.get("output_dir") or raw.get("out") or raw.get("tree_dir") or raw.get("cluster_tree_dir") or ""
        if td:
            p = os.path.expanduser(str(td).strip())
            return os.path.abspath(p if os.path.isabs(p) else os.path.join(bdir, p))
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_cluster.py",
        description="归因聚类独立可执行模块（OpenAI 双协议 + 既有树增量 + 自适应建叶 + 脏数据清理）",
    )
    ap.add_argument("-c", "--config", help="YAML 或 JSON 配置文件路径（常规聚类必填；--prune/--recall 可改传 --tree-dir）")
    ap.add_argument("--tree-dir", default="", help="目标聚类树目录（配合 --prune 或 --recall 使用，可免配置文件）")
    ap.add_argument("--mode", choices=["auto", "incremental", "full", "rebuild"],
                    help="可选覆盖运行模式（默认读取配置文件中的 mode，缺省为 auto）")
    ap.add_argument("--dry-run", action="store_true", help="仅解析配置与统计输入条目，不发起 API 调用 / 不修改文件")
    ap.add_argument("--recall", metavar="NODE_ID", help="按节点 ID（如 S9.M6.4）从聚类树目录召回样本并退出")
    ap.add_argument("--limit", type=int, default=5, help="配合 --recall 控制输出条数（默认 5）")
    ap.add_argument("--prune", action="store_true", help="按清单或 self-session 从既有聚类树清理脏数据并零 API 重聚合")
    ap.add_argument("--drop-ids", default="", help="配合 --prune 使用：待删除 case_id / trace_id 清单文件（每行一个）")
    ap.add_argument("--self-session", action="store_true", help="配合 --prune 使用：删除 session_id == trace_id 的兜底条目")
    ap.add_argument("--reason", default="", help="配合 --prune 使用：写入 prune.jsonl 的清理原因")
    args = ap.parse_args(argv)

    if args.prune:
        tree_dir = _resolve_tree_dir_from_args(args)
        if not tree_dir:
            ap.error("--prune 需要提供 --tree-dir 或包含 tree_dir 的 -c/--config")
        res = prune_tree(
            tree_dir=tree_dir,
            drop_ids_file=args.drop_ids,
            self_session=args.self_session,
            reason=args.reason,
            dry_run=args.dry_run,
        )
        print(json.dumps({
            "status": res["status"],
            "output_dir": res["output_dir"],
            "dropped_items": res["dropped_items"],
            "left_items": res["left_items"],
            "by": res["by"],
            "elapsed_sec": res["elapsed_sec"],
        }, ensure_ascii=False))
        return 0

    if args.recall:
        tree_dir = _resolve_tree_dir_from_args(args)
        if not tree_dir:
            ap.error("--recall 需要提供 --tree-dir 或包含 tree_dir 的 -c/--config")
        _cmd_recall(tree_dir, args.recall, args.limit)
        return 0

    if not args.config:
        ap.error("执行聚类必须提供 -c/--config 配置文件路径")

    # 相对路径统一相对于调用者当前工作目录解析（若配置内写相对路径，默认按配置文件所在目录解析）
    cfg = ClusterConfig.from_source(args.config)
    if args.mode:
        cfg.mode = args.mode
    if args.dry_run:
        cfg.dry_run = True

    res = ClusterEngine(cfg).run()
    print(json.dumps({
        "status": res["status"],
        "mode": res["mode"],
        "output_dir": res["output_dir"],
        "total_items": res.get("total_items", res.get("ingested_items", 0)),
        "new_items": res.get("new_items", 0),
        "created_components": len(res.get("created_components", [])),
        "created_leaves": [x["id"] for x in res.get("created_leaves", [])],
        "elapsed_sec": res["elapsed_sec"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
