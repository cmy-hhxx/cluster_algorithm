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


def _select_l4_jobs(items_by_seq: dict, assignments: dict, tax: TaxonomyStore, cfg: L4Config):
    """按配置筛选待下钻父叶并切分分片，返回 [(leaf, subs, table, part_items), ...]。"""
    if not cfg.enabled or not cfg.leaves or not tax.l4_nodes:
        return []

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
            elif not cfg.force and p == leaf:
                seqs.append(s)
        seqs.sort()
        if len(seqs) < cfg.min_items:
            continue
        for i in range(0, len(seqs), cfg.chunk_size):
            part = [items_by_seq[s] for s in seqs[i: i + cfg.chunk_size]] if items_by_seq else seqs[i: i + cfg.chunk_size]
            jobs.append((leaf, subs, table, part))
    return jobs


def has_pending_l4(assignments: dict, tax: TaxonomyStore, cfg: L4Config) -> bool:
    """判断当前分配结果中是否存在待执行或待重跑的 L4 下钻任务。"""
    return bool(_select_l4_jobs({}, assignments, tax, cfg))


def _judge_slice(client: ResponsesClient, leaf: str, subs: list, table: str, rows: list, logger, depth: int = 0):
    """单分片 L4 判定：2 次重试失败后在满足条件时自动对半拆片。"""
    prompt = _build_l4_prompt(leaf, table, rows)
    want = {int(r["seq"]) for r in rows}
    sub_set = set(subs)
    last_err = ""
    for _ in range(2):
        try:
            raw_rows = parse_jsonl_rows(client.complete_text(prompt))
            out = []
            for r in raw_rows:
                if "seq" not in r or "path" not in r:
                    continue
                try:
                    seq_int = int(r["seq"])
                except (TypeError, ValueError):
                    continue
                r["seq"] = seq_int
                r["path"] = str(r["path"]).strip().strip("`")
                out.append(r)
            got_seqs = [r["seq"] for r in out]
            if (
                len(out) == len(want)
                and len(set(got_seqs)) == len(want)
                and set(got_seqs) == want
                and all(r["path"] in sub_set for r in out)
            ):
                return out
            last_err = f"rows={len(out)}/{len(want)}"
        except Exception as e:
            last_err = str(e)[:120]

    if depth < 2 and len(rows) >= 20:
        mid = len(rows) // 2
        logger(f"  {leaf}: L4 分片对半拆片 {len(rows)} -> {mid}+{len(rows) - mid} ({last_err})")
        left = _judge_slice(client, leaf, subs, table, rows[:mid], logger, depth + 1)
        right = _judge_slice(client, leaf, subs, table, rows[mid:], logger, depth + 1)
        return left + right

    raise RuntimeError(f"{leaf}: {last_err}")


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
    jobs = _select_l4_jobs(items_by_seq, assignments, tax, cfg)
    if not jobs:
        return 0

    logger(f"L4 子类下钻：{len(jobs)} 个分片（并发 {concurrency}）")

    def judge(job):
        leaf, subs, table, rows = job
        return _judge_slice(client, leaf, subs, table, rows, logger)

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
