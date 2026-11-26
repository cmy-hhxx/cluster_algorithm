# -*- coding: utf-8 -*-
"""既有聚类树脏数据清理器：按 case_id / trace_id / self-session 删除条目并零 API 重聚合交付。"""
import collections
import datetime
import json
import os
import shutil
import tempfile
import time
from typing import Any, Dict, Iterable, Optional

from .engine import __version__, _atomic_publish_staging, _file_lock
from .ingestor import clean, iso_time, read_lines, usable_sid
from .reporter import aggregate_and_deliver
from .tax_store import TaxonomyStore


def _read_drop_ids_file(path: str) -> set:
    if not path:
        return set()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"待删除 ID 清单文件不存在: {path}")
    with open(path, encoding="utf-8") as fh:
        return {ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")}


def _normalize_index_row(r: dict, idx_fallback_seq: int) -> dict:
    tid = clean(r.get("trace_id") or r.get("case_id"))
    sid = usable_sid(r.get("session_id")) or tid
    rtime = clean(r.get("request_time") or r.get("date") or r.get("batch_id"))
    dt = clean(r.get("date") or r.get("batch_id") or (rtime[:10] if rtime else ""))
    seq_val = r.get("seq")
    seq = int(seq_val) if seq_val is not None and str(seq_val).isdigit() else idx_fallback_seq
    return {
        "seq": seq,
        "date": dt,
        "batch_id": clean(r.get("batch_id") or dt),
        "model": clean(r.get("model")),
        "src_rel": clean(r.get("src_rel")),
        "case_id": clean(r.get("case_id") or tid),
        "trace_id": tid,
        "session_id": sid,
        "user_id": clean(r.get("user_id")),
        "question": clean(r.get("question")),
        "request_time": rtime,
        "request_time_iso": iso_time(rtime),
        "bad_case_id": clean(r.get("bad_case_id")),
        "channel": clean(r.get("channel") or "default"),
        "turn_index": int(r.get("turn_index") or 0),
        "problem_index": int(r.get("problem_index") or 0),
        "problem_id": clean(r.get("problem_id")),
        "severity": clean(r.get("severity")),
        "component": clean(r.get("component") or ""),
        "component_raw": clean(r.get("component_raw") or r.get("component") or ""),
        "label": clean(r.get("label") or (r.get("definition") or "")[:40]),
        "definition": clean(r.get("definition")),
        "reason": clean(r.get("reason")),
        "path": clean(r.get("path")),
        "edge": bool(r.get("edge", False)),
    }


def prune_tree(
    tree_dir: str,
    drop_ids: Optional[Iterable[str]] = None,
    drop_ids_file: str = "",
    self_session: bool = False,
    reason: str = "",
    dry_run: bool = False,
    output_dir: str = "",
    sessions_all: int = 0,
    font_path: str = "",
    logger=None,
) -> Dict[str, Any]:
    """按 case_id / trace_id 清单或 session_id == trace_id 从既有聚类树中删除脏数据并重聚合交付。"""
    t0 = time.time()
    log = logger or (lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True))

    src_dir = os.path.abspath(os.path.expanduser(tree_dir))
    out_dir = os.path.abspath(os.path.expanduser(output_dir)) if output_dir else src_dir

    ids = {str(x).strip() for x in (drop_ids or []) if str(x).strip()}
    if drop_ids_file:
        ids |= _read_drop_ids_file(os.path.expanduser(drop_ids_file))

    if not ids and not self_session:
        raise ValueError("必须指定至少一种清理条件：drop_ids / drop_ids_file / self_session")

    idx_path = os.path.join(src_dir, "item_index.jsonl")
    if not os.path.isfile(idx_path):
        raise FileNotFoundError(f"聚类树目录缺少 item_index.jsonl: {idx_path}")

    raw_rows = []
    for i, line in enumerate(read_lines(idx_path), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        it = _normalize_index_row(r, i)
        if not it["trace_id"] or not it["path"]:
            continue
        raw_rows.append(it)

    kept_items = []
    dropped_counts = collections.Counter()
    seen_seqs = set()
    next_seq = (max((r["seq"] for r in raw_rows), default=0)) + 1

    for it in raw_rows:
        drop_why = ""
        if self_session and it["session_id"] and it["session_id"] == it["trace_id"]:
            drop_why = "self_session"
        elif (it["case_id"] and it["case_id"] in ids) or (it["trace_id"] and it["trace_id"] in ids):
            drop_why = "drop_id"

        if drop_why:
            dropped_counts[drop_why] += 1
            continue

        if it["seq"] in seen_seqs:
            it["seq"] = next_seq
            next_seq += 1
        seen_seqs.add(it["seq"])
        kept_items.append(it)

    n_total = len(raw_rows)
    n_drop = sum(dropped_counts.values())
    n_left = len(kept_items)

    if dry_run:
        log(f"[dry-run] 原条目={n_total} 待删={n_drop} {dict(dropped_counts)} 剩余={n_left}")
        return {
            "status": "dry_run",
            "tree_dir": src_dir,
            "output_dir": out_dir,
            "total_before": n_total,
            "dropped_items": n_drop,
            "left_items": n_left,
            "by": dict(dropped_counts),
            "elapsed_sec": round(time.time() - t0, 2),
        }

    if n_drop == 0 and src_dir == out_dir:
        log(f"prune 检测：无匹配条目需要清理 (noop)，总条目={n_total}")
        return {
            "status": "noop",
            "tree_dir": src_dir,
            "output_dir": out_dir,
            "total_before": n_total,
            "dropped_items": 0,
            "left_items": n_left,
            "by": {},
            "elapsed_sec": round(time.time() - t0, 2),
        }

    if n_left == 0:
        raise RuntimeError(f"清理后剩余条目数为 0（原 {n_total} 条全部命中删除条件），已中止以保护原聚类树")

    out_parent = os.path.dirname(out_dir) or "."
    os.makedirs(out_parent, exist_ok=True)
    lock_file = os.path.join(out_parent, f".cluster_{os.path.basename(out_dir)}.lock")

    with _file_lock(lock_file):
        staging_dir = tempfile.mkdtemp(prefix=f".cluster_prune_{os.path.basename(out_dir)}_", dir=out_parent)
        try:
            stage_tax_dir = os.path.join(staging_dir, "taxonomy")
            tax = TaxonomyStore.initialize(target_tax_dir=stage_tax_dir, existing_tree_dir=src_dir)

            assignments = {it["seq"]: (it["path"], it["edge"]) for it in kept_items}
            ver = aggregate_and_deliver(
                items_list=kept_items,
                assignments=assignments,
                tax=tax,
                out_dir=staging_dir,
                version=__version__,
                sessions_all=sessions_all,
                font_path=font_path,
            )

            for aux in ("taxonomy_evolution.jsonl", "prune.jsonl"):
                for cand_dir in (out_dir, src_dir):
                    aux_src = os.path.join(cand_dir, aux)
                    if os.path.isfile(aux_src):
                        shutil.copy2(aux_src, os.path.join(staging_dir, aux))
                        break

            backup_dir = ""
            if os.path.isdir(out_dir):
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_dir = os.path.join(out_dir, f".prune-{ts}")
                idx_suffix = 2
                while os.path.exists(backup_dir):
                    backup_dir = os.path.join(out_dir, f".prune-{ts}-{idx_suffix}")
                    idx_suffix += 1
                os.makedirs(backup_dir, exist_ok=True)
                for bfn in ("item_index.jsonl", "cluster_tree.json"):
                    bp = os.path.join(out_dir, bfn)
                    if os.path.isfile(bp):
                        shutil.copy2(bp, os.path.join(backup_dir, bfn))

            _atomic_publish_staging(staging_dir, stage_tax_dir, out_dir)

            audit_rec = {
                "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "tree_dir": src_dir,
                "output_dir": out_dir,
                "reason": reason,
                "selectors": {
                    "self_session": bool(self_session),
                    "drop_ids_file": os.path.abspath(drop_ids_file) if drop_ids_file else "",
                    "drop_ids_count": len(ids),
                },
                "total_before": n_total,
                "dropped_items": n_drop,
                "left_items": n_left,
                "by": dict(dropped_counts),
                "backup": backup_dir,
            }
            with open(os.path.join(out_dir, "prune.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(audit_rec, ensure_ascii=False) + "\n")

            elapsed = round(time.time() - t0, 2)
            log(
                f"清理完成：删除 {n_drop} 条 {dict(dropped_counts)}，保留 {n_left} 条 | "
                f"verify err={ver['errors']} mismatch={ver['locator_mismatch']} | 耗时 {elapsed}s -> {out_dir}"
            )
            return {
                "status": "ok",
                "tree_dir": src_dir,
                "output_dir": out_dir,
                "total_before": n_total,
                "dropped_items": n_drop,
                "left_items": n_left,
                "by": dict(dropped_counts),
                "backup": backup_dir,
                "verify": ver,
                "elapsed_sec": elapsed,
            }
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
