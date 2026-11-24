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

__all__ = ["ClusterConfig", "ClusterEngine", "run_cluster", "main"]


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_cluster.py",
        description="归因聚类独立可执行模块（OpenAI-Responses 协议 + 既有树增量 + 自适应建叶）",
    )
    ap.add_argument("-c", "--config", required=True, help="YAML 或 JSON 配置文件路径（如 config.yaml）")
    ap.add_argument("--mode", choices=["auto", "incremental", "full", "rebuild"],
                    help="可选覆盖运行模式（默认读取配置文件中的 mode，缺省为 auto）")
    ap.add_argument("--dry-run", action="store_true", help="仅解析配置与统计输入条目，不发起 API 调用")
    ap.add_argument("--recall", metavar="NODE_ID", help="按节点 ID（如 S9.M6.4）从聚类树目录召回样本并退出")
    ap.add_argument("--limit", type=int, default=5, help="配合 --recall 控制输出条数（默认 5）")
    args = ap.parse_args(argv)

    # 相对路径统一相对于调用者当前工作目录解析（若配置内写相对路径，默认按配置文件所在目录解析）
    cfg = ClusterConfig.from_source(args.config)
    if args.mode:
        cfg.mode = args.mode
    if args.dry_run:
        cfg.dry_run = True

    if args.recall:
        _cmd_recall(cfg.output_dir or cfg.tree_dir, args.recall, args.limit)
        return 0

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
