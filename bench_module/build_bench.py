#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIME 动态 bench：主池 + 分层取样（独立模块，0 依赖，只读聚类产物）。

数据流：

    eval_output/**/*attribution*.jsonl ─┐
                                        ├─→ pool/<model>.jsonl   ← 落盘的主池（唯一真源，增量刷新）
    eval_output/cluster/item_index.jsonl┘         │
                                                  ├─→ tier_500 / 1000 / 5000 / 10000
    eval_input（取 messages，定位表加速）──────────┘

两种用法：
  只刷池（每天一次，也可手动）：   --pool-only
  从池重抽档位（发版前，手动）：   --no-refresh

不改聚类流程，不写聚类目录，不依赖 pandas/numpy。
"""
import argparse
import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from benchlib import __version__                                   # noqa: E402
from benchlib.common import jdump, Taxonomy                        # noqa: E402
from benchlib.loader import (dedupe, drop_unclassified, load_item_index,  # noqa: E402
                             resolve_patterns)
from benchlib.materialize import Materializer                      # noqa: E402
from benchlib.pool import Pool                                     # noqa: E402
from benchlib.report import pool_stats, write_readme, write_stats  # noqa: E402
from benchlib.sampler import plan_tiers                            # noqa: E402

META_KEYS = ["trace_id", "session_id", "turn_index", "model", "agent_id", "name",
             "mode", "runtime_mode", "timestamp", "latency", "n_messages",
             "n_observations", "total_tokens", "input_tokens", "output_tokens",
             "tool_count", "tool_names_text", "skill_count", "skill_names_text",
             "outcome", "status", "comefrom", "source",
             "recalled_from", "recalled_date", "question_at", "run_started_at",
             "ended_at", "question_length", "rued", "rued_hits", "answer_preview"]


class Tee:
    def __init__(self, path):
        self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()

    def close(self):
        self.fh.close()


def bench_row(c, rec, tier, rank, params, pool_size):
    tr = {}
    if rec:
        traces = rec.get("traces") or []
        tr = traces[0] if traces and isinstance(traces[0], dict) else {}
    tmeta = tr.get("meta") or {}
    meta = {k: tmeta.get(k) for k in META_KEYS}
    problems = []
    for p in c["problems"]:
        problems.append({
            "severity": p["severity"],
            "label": p["label"],
            "path": p["path"],
            "path_source": p["path_source"],
            "l1": p["l1"], "l2": p["l2"],
            "component": p["component"],
            "component_role": p["component_role"],
        })
    row = {
        "trace_id": c["trace_id"],
        "session_id": c["session_id"],
        "model": c["model"],
        "question": c["question"],
        "request_time": c["request_time"],
        "batch_id": c["batch_id"],
        "group": c["group"],
        "turn": c["turn"],
        "session_round": c["session_round"],
        "turn_index": c["turn_index"],
        "severity": c["worst"] or "GOLD",
        "primary": {"l1": c["l1"], "l2": c["l2"], "l3": c["l3"]},
        "problems": problems,
        "stats": {
            "total_tokens": c["total_tokens"],
            "input_tokens": c["input_tokens"],
            "tool_count": tmeta.get("tool_count"),
            "latency": tmeta.get("latency"),
            "dup_question": c["dup_count"],
            "short_followup": bool(c.get("short_followup")),
        },
        "meta": meta,
        "messages": tr.get("messages") or [],
        "message_pool": tr.get("message_pool") or [],
        "bench": {
            "tier": tier,
            "rank": rank,
            "seed": params["seed"],
            "pool_size": pool_size,
            "stratum": {"severity": c["worst"] or "GOLD", "l2": c["l2"] or "NA",
                        "turn": c["turn"]},
        },
    }
    if c.get("question_full"):
        row["question_full"] = c["question_full"]   # 被系统模板污染的原问句，留档可回溯
    return row


def write_tier(path, cases, mat, tier, params, pool_size, log, stats):
    tmp = path + ".tmp"
    written = []
    with open(tmp, "w", encoding="utf-8") as fh:
        for c in cases:
            rec = mat.read(c["trace_id"]) if mat else None
            if mat and rec is None:
                stats["materialize_missing"] += 1
                continue
            fh.write(json.dumps(bench_row(c, rec, tier, len(written), params, pool_size),
                                ensure_ascii=False) + "\n")
            written.append(c)
    os.replace(tmp, path)
    log("  写出 %s（%d 条）" % (os.path.basename(path), len(written)))
    return written


def main():
    ap = argparse.ArgumentParser(description="AIME 动态 bench：主池 + 分层取样")
    ap.add_argument("--attr-root", required=True, help="归因根，如 .../1_aime/eval_output")
    ap.add_argument("--cluster-root", default="", help="聚类产物根，默认 <attr-root>/cluster")
    ap.add_argument("--eval-input", required=True, help="eval_input 根（trace 粒度原文）")
    ap.add_argument("--out", required=True, help="产物根，如 /mnt/aime_eval_ssd/3_dynamic_bench")
    ap.add_argument("--pool-dir", default="", help="主池目录，默认 <out>/pool")
    ap.add_argument("--models", default="aime_expert,aime_fast")
    ap.add_argument("--tiers", default="500,1000,5000,10000")
    ap.add_argument("--seed", default="20261118")
    ap.add_argument("--golden-share", type=float, default=0.20)
    ap.add_argument("--p0-cap", type=float, default=0.25)
    ap.add_argument("--long-tokens", type=int, default=65536)
    ap.add_argument("--long-share", type=float, default=0.02)
    ap.add_argument("--min-question-chars", type=int, default=6)
    ap.add_argument("--keep-low-quality", action="store_true")
    ap.add_argument("--skip-messages", action="store_true",
                    help="只出统计与清单骨架（不取 messages），用于快速试跑")
    ap.add_argument("--pool-only", action="store_true", help="只刷新主池，不抽档位")
    ap.add_argument("--no-refresh", action="store_true", help="不刷池，直接从现有池子重抽")
    a = ap.parse_args()

    out = os.path.abspath(a.out)
    build_dir = os.path.join(out, "_build")
    pool_dir = os.path.abspath(a.pool_dir or os.path.join(out, "pool"))
    os.makedirs(build_dir, exist_ok=True)
    os.makedirs(pool_dir, exist_ok=True)
    log = Tee(os.path.join(build_dir, "build_%s.log" % time.strftime("%Y%m%dT%H%M%S")))

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    tiers = sorted({int(t) for t in a.tiers.split(",") if t.strip()})
    params = {
        "seed": a.seed,
        "golden_share": a.golden_share,
        "p0_cap": a.p0_cap,
        "long_tokens": a.long_tokens,
        "long_share": a.long_share,
        "min_question_chars": a.min_question_chars,
        "drop_low_quality": not a.keep_low_quality,
        "tiers": tiers,
        "models": models,
        "channel": "aime",
    }
    t0 = time.time()
    log("build_bench v%s  models=%s tiers=%s seed=%s%s"
        % (__version__, models, tiers, a.seed,
           "  [只刷池]" if a.pool_only else ("  [不刷池]" if a.no_refresh else "")))

    tax = Taxonomy()
    stats = collections.Counter()
    stats["materialize_missing"] = 0

    mat = None
    if not a.skip_messages:
        mat = Materializer(os.path.abspath(a.eval_input), build_dir, log)

    log("1/5 主池 …")
    pool = Pool(pool_dir, models, log)
    refresh = None
    if a.no_refresh:
        pool.load()
        log("  跳过刷新，直接用现有池子（%d 条）"
            % sum(len(v) for v in pool.cases.values()))
    else:
        refresh = pool.refresh(os.path.abspath(a.attr_root), mat)

    manifest = {
        "module": "bench_module", "version": __version__,
        "channel": "aime",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "params": params,
        "pool": {
            "dir": os.path.basename(pool_dir),
            "cases_by_model": {m: len(pool.cases.get(m) or {}) for m in models},
            "cases": sum(len(v) for v in pool.cases.values()),
            "last_refresh": pool.state.get("last_refresh", ""),
            "refresh": refresh,
        },
        "models": {},
    }
    if a.pool_only:
        jdump(manifest, os.path.join(pool_dir, "manifest.json"))
        log("完成（只刷池），耗时 %.1fs，池子 %s：%s"
            % (time.time() - t0, manifest["pool"]["cases"], pool_dir))
        log.close()
        return

    log("2/5 join 聚类 item_index …")
    cluster_root = os.path.abspath(a.cluster_root or os.path.join(a.attr_root, "cluster"))
    idx, nidx = load_item_index(cluster_root, models)
    log("  item_index %d 个模型目录，%d 条 problem-叶 映射" % (nidx, len(idx)))

    log("3/5 补 L1/L2/L3 + 质量闸 …")
    cases = {}
    for model in models:
        for tid, c in (pool.cases.get(model) or {}).items():
            cases[(model, tid)] = c
    resolve_patterns(cases, idx, tax, stats)
    total_p = stats["join_ok"] + stats["join_miss"]
    join_rate = round(stats["join_ok"] / total_p, 4) if total_p else 0.0
    log("  join 命中 %d / %d（%.1f%%）" % (stats["join_ok"], total_p, join_rate * 100))
    cases = drop_unclassified(cases, stats)
    log("  剔除 join 失败 case %d 条（任一条 problem 落不到叶）"
        % stats["join_failed_dropped"])

    pool_cases = dedupe(cases, a.long_tokens, stats, a.min_question_chars,
                        not a.keep_low_quality)
    log("  可采样 %d 条（合并同问句 %d；剔除：空问句 %d / 单轮过短 %d / 单轮裸URL %d / 缺字段 %d；"
        "保留多轮短追问 %d）"
        % (len(pool_cases), stats["dup_collapsed"], stats["empty_question"],
           stats["q_too_short"], stats["q_url_only"], stats["meta_incomplete_dropped"],
           stats["q_short_followup_kept"]))

    manifest["source"] = {
        "attr_root": os.path.basename(os.path.abspath(a.attr_root)),
        "cluster_item_index_models": nidx,
        "join_rate": join_rate,
    }
    manifest["pool"].update({
        "usable_cases": len(pool_cases),
        "dup_collapsed": stats["dup_collapsed"],
        "empty_question": stats["empty_question"],
    })

    for model in models:
        pool_m = [c for c in pool_cases if c["model"] == model]
        base = [c for c in pool_m if not (c["total_tokens"] >= a.long_tokens)]
        longp = [c for c in pool_m if c["total_tokens"] >= a.long_tokens]
        log("4/5 [%s] 可采样 %d（常规 %d / 长上下文 %d）" % (model, len(pool_m), len(base), len(longp)))
        if not base:
            log("  WARN 该模型没有可用 case，跳过")
            continue

        mdir = os.path.join(out, model)
        sdir = os.path.join(mdir, "stats")
        os.makedirs(mdir, exist_ok=True)
        ps = pool_stats(base, tax, params)
        jdump(ps, os.path.join(mdir, "pool_stats.json"))

        plans = plan_tiers(base, tiers, params)
        long_tiers = sorted({max(1, int(round(t * a.long_share))) for t in tiers})
        lplans = plan_tiers(longp, long_tiers, params) if longp else {}

        if mat:
            wanted = {}
            for pl in plans.values():
                for c in pl["cases"]:
                    wanted[c["trace_id"]] = (c["model"], c["batch_id"])
            for pl in lplans.values():
                for c in pl["cases"]:
                    wanted[c["trace_id"]] = (c["model"], c["batch_id"])
            log("  定位 %d 条 trace 的原文 …" % len(wanted))
            missing = mat.ensure(wanted)
            log("  定位失败 %d 条" % len(missing))
            ok = set(wanted) - missing
            for tid in list(ok):
                if mat.read(tid) is None:
                    ok.discard(tid)
            if len(ok) != len(wanted) - len(missing):
                log("  原文不可解析 %d 条" % (len(wanted) - len(missing) - len(ok)))
        else:
            ok = None

        def _flt(cases, _ok=ok):
            return [c for c in cases if c["trace_id"] in _ok] if _ok is not None else cases

        m_info = {"pool_size": len(pool_m), "base_pool": len(base),
                  "long_pool": len(longp),
                  "pool_by_severity": ps["by_severity"], "pool_by_turn": ps["by_turn"],
                  "tiers": {}}
        for tier, plan in plans.items():
            written = write_tier(os.path.join(mdir, "tier_%d.jsonl" % tier), _flt(plan["cases"]),
                                 mat, tier, params, len(base), log, stats)
            st = write_stats(sdir, tier, plan, written, base, tax, params)
            n = len(written)
            m_info["tiers"][tier] = {"requested": plan["requested"], "actual": n,
                                     # shortfall 是"最终没写出去"的差额（池子不足 + 原文取不到）
                                     "shortfall": plan["requested"] - n}
            log("  tier_%d：requested=%d actual=%d（%s）"
                % (tier, plan["requested"], n,
                   ",".join("%s:%d" % kv for kv in st["severity_actual"].items())))

        if lplans:
            ldir = os.path.join(mdir, "long")
            os.makedirs(ldir, exist_ok=True)
            m_info["long"] = {}
            for ltier, plan in lplans.items():
                written = write_tier(os.path.join(ldir, "tier_%d_long.jsonl" % ltier),
                                     _flt(plan["cases"]), mat, ltier, params, len(longp),
                                     log, stats)
                m_info["long"][ltier] = {"actual": len(written), "pool": len(longp)}

        manifest["models"][model] = m_info

    if mat:
        mat.save()

    manifest["quality"] = {
        "materialize_missing": stats["materialize_missing"],
        "join_rate": join_rate,
        "join_miss_problems": stats["join_miss"],
        "join_failed_dropped": stats["join_failed_dropped"],
        "q_too_short_dropped": stats["q_too_short"],
        "q_url_only_dropped": stats["q_url_only"],
        "meta_incomplete_dropped": stats["meta_incomplete_dropped"],
        "q_short_followup_kept": stats["q_short_followup_kept"],
        "bad_severity": stats["bad_severity"],
    }
    manifest["elapsed_sec"] = round(time.time() - t0, 1)
    jdump(manifest, os.path.join(out, "manifest.json"))
    write_readme(out, manifest, params)
    log("5/5 完成，用时 %.1fs。产物：%s" % (manifest["elapsed_sec"], out))
    log.close()


if __name__ == "__main__":
    main()
