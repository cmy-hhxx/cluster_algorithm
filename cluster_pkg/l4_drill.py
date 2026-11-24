# -*- coding: utf-8 -*-
"""L4 第四层子类下钻器：对命中 l4_nodes.json 已登记父叶的条目执行细粒度归类。"""
import concurrent.futures
import json

from .cfg import L4Config
from .llm_client import ResponsesClient, parse_jsonl_rows
from .tax_store import TaxonomyStore, load_prompt_file


def _subs_table(leaf: str, l4_nodes: dict):
    subs = sorted(
        [k for k in l4_nodes if k.startswith(leaf + ".")],
        key=lambda s: int(s.rsplit(".", 1)[1]) if s.rsplit(".", 1)[1].isdigit() else 999,
    )
    lines = []
    for sid in subs:
        n = l4_nodes[sid]
        lines.append(
            f"| `{sid}` | {n.get('name', '')} | {n.get('definition', '')} | "
            f"收：{n.get('include', '')} | 不收：{n.get('exclude', '')} |"
        )
    return subs, "\n".join(lines)


def _build_l4_prompt(leaf: str, subs_table: str, rows) -> str:
    head = load_prompt_file("03_l4_assign.md").replace("{{LEAF}}", leaf).replace("{{N}}", str(len(rows)))
    body = "\n".join(
        json.dumps(
            {"seq": r["seq"], **{k: (r.get(k) or "")[:400] for k in ("component", "label", "definition", "reason")}},
            ensure_ascii=False,
        )
        for r in rows
    )
    return (
        f"{head}\n\n## 本次子类清单（只能从这里选 {leaf}.N）\n\n"
        f"| 类别 | 名称 | 定义 | include | exclude |\n|---|---|---|---|---|\n{subs_table}\n\n"
        f"## 待归类条目（{len(rows)} 条）\n\n{body}\n\n"
        f'只输出恰好 {len(rows)} 行 JSONL，每行 {{"seq": <int>, "path": "{leaf}.N", "edge": <true|false>}}。'
    )


def apply_l4_drilldown(
    items_by_seq: dict,
    assignments: dict,
    newly_classified_seqs: set,
    client: ResponsesClient,
    tax: TaxonomyStore,
    cfg: L4Config,
    concurrency: int,
    logger,
):
    """原地把命中目标父叶的 3 段 path 下钻为 4 段 path（失败分片安全保留原 3 段父叶）。"""
    if not cfg.enabled or not cfg.leaves or not tax.l4_nodes:
        return 0

    parent_leaves_in_tax = sorted({k.rsplit(".", 1)[0] for k in tax.l4_nodes if k.count(".") == 3})
    if cfg.leaves == "auto":
        target_leaves = parent_leaves_in_tax
    else:
        target_leaves = [x.strip() for x in cfg.leaves.split(",") if x.strip()]

    jobs = []
    for leaf in target_leaves:
        subs, table = _subs_table(leaf, tax.l4_nodes)
        if len(subs) < 2:
            continue
        seqs = []
        for s, (p, _e) in assignments.items():
            if cfg.force and (p == leaf or p.startswith(leaf + ".")):
                seqs.append(s)
            elif p == leaf and (cfg.force or not newly_classified_seqs or s in newly_classified_seqs or p.count(".") == 2):
                seqs.append(s)
        seqs.sort()
        if len(seqs) < cfg.min_items:
            continue
        for i in range(0, len(seqs), cfg.chunk_size):
            part = [items_by_seq[s] for s in seqs[i: i + cfg.chunk_size]]
            jobs.append((leaf, subs, table, part))

    if not jobs:
        return 0

    logger(f"L4 子类下钻：{len(jobs)} 个分片（并发 {concurrency}）")

    def judge(job):
        leaf, subs, table, rows = job
        prompt = _build_l4_prompt(leaf, table, rows)
        want = {r["seq"] for r in rows}
        sub_set = set(subs)
        last_err = ""
        for _ in range(2):
            try:
                out = [r for r in parse_jsonl_rows(client.complete_text(prompt)) if "seq" in r and "path" in r]
                for r in out:
                    r["path"] = str(r["path"]).strip().strip("`")
                if {r["seq"] for r in out} == want and all(r["path"] in sub_set for r in out):
                    return out
                last_err = f"rows={len(out)}/{len(want)}"
            except Exception as e:
                last_err = str(e)[:120]
        raise RuntimeError(f"{leaf}: {last_err}")

    updated = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(judge, j): j for j in jobs}
        for f in concurrent.futures.as_completed(futs):
            leaf, _subs, _tbl, rows = futs[f]
            try:
                for r in f.result():
                    old_path, old_edge = assignments[r["seq"]]
                    assignments[r["seq"]] = (r["path"], bool(r.get("edge")) or old_edge)
                    updated += 1
            except Exception as e:
                logger(f"  WARN L4 分片未成功 ({leaf}, {len(rows)} 条)，保留 3 段父叶: {e}")

    logger(f"L4 下钻完成：更新 {updated} 条为 4 段路径")
    return updated
