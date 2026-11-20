# -*- coding: utf-8 -*-
"""统计与可视化数据：维度长表、覆盖度、交叉表、池子基准分布。

产物是给可视化同事直接 group by 用的长表 CSV：
  dim,key,bucket,count,share_in_bench,share_in_pool,delta_pp
`delta_pp` = bench 占比 - 池子占比（百分点），是判断"这一维抽偏了没有"的唯一指标。
"""
import collections
import csv
import glob
import os

from .common import jdump, round_bucket

CSV_COLS = ["dim", "key", "bucket", "count", "share_in_bench", "share_in_pool", "delta_pp"]


def dim_defs(tax):
    """(维度名, 取值函数, 人读名函数, 是否只看有缺陷的 case)。

    stage / mechanism / pattern 只统计**有缺陷**的 case：GOLD 是"无缺陷"，它没有环节/机制/叶，
    混进来会变成 NA 桶——看板上就是说不清的"其他"。severity 维度保留 GOLD，那才是它的位置。
    """
    return [
        ("severity", lambda c: c["worst"] or "GOLD", lambda k: k, False),
        ("stage", lambda c: c["l1"] or "NA", tax.stage_name, True),
        ("mechanism", lambda c: c["l2"] or "NA", tax.mech_name, True),
        ("pattern", lambda c: c["l3"] or "NA", lambda k: tax.leaf_name(k) or k, True),
        ("turn_type", lambda c: c["turn"], lambda k: k, False),
        ("day", lambda c: c["day"], lambda k: k, False),
        ("round", lambda c: round_bucket(c["session_round"]), lambda k: k, False),
        ("group", lambda c: c["group"], lambda k: k, False),
    ]


def dim_table(pool, bench, name, key_fn, label_fn, only_defect=False):
    if only_defect:
        pool = [c for c in pool if c["worst"]]
        bench = [c for c in bench if c["worst"]]
    pc = collections.Counter(key_fn(c) for c in pool)
    bc = collections.Counter(key_fn(c) for c in bench)
    npool, nbench = len(pool) or 1, len(bench) or 1
    rows = []
    for k in sorted(set(pc) | set(bc)):
        sp, sb = pc[k] / npool, bc[k] / nbench
        rows.append({"dim": name, "key": k, "bucket": label_fn(k), "count": bc[k],
                     "share_in_bench": round(sb, 6), "share_in_pool": round(sp, 6),
                     "delta_pp": round((sb - sp) * 100, 2)})
    return rows


def all_tables(pool, bench, tax):
    return {name: dim_table(pool, bench, name, kf, lf, only_defect)
            for name, kf, lf, only_defect in dim_defs(tax)}


def write_dim_csvs(stats_dir, tier, tables):
    for name, rows in tables.items():
        p = os.path.join(stats_dir, "tier_%d.dim_%s.csv" % (tier, name))
        with open(p, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLS)
            w.writeheader()
            for r in rows:
                w.writerow(r)


def write_crosstab(path, pool, bench, tax):
    """环节 × 机制交叉表：bench 计数 + 池子计数，看覆盖有没有成片空白。"""
    pool = [c for c in pool if c["worst"]]
    bench = [c for c in bench if c["worst"]]
    pcs = collections.Counter((c["l1"] or "NA", c["l2"] or "NA") for c in pool)
    bcs = collections.Counter((c["l1"] or "NA", c["l2"] or "NA") for c in bench)
    stages = sorted({k[0] for k in pcs} | {k[0] for k in bcs})
    mechs = sorted({k[1] for k in pcs} | {k[1] for k in bcs})
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["stage", "stage_name"] + mechs + ["bench_total", "pool_total"])
        for s in stages:
            row = [s, tax.stage_name(s)] + [bcs[(s, m)] for m in mechs]
            row += [sum(bcs[(s, m)] for m in mechs), sum(pcs[(s, m)] for m in mechs)]
            w.writerow(row)
        w.writerow(["TOTAL", ""] + [sum(bcs[(s, m)] for s in stages) for m in mechs]
                   + [len(bench), len(pool)])


BY_DESIGN = {"severity", "stage", "mechanism", "pattern", "turn_type"}


def write_coverage(path, tables, tol_pp=10.0):
    """每维一行：最大偏差、池里有但 bench 完全没采到的 key 数、是否在容差内。

    `by_design` 的维度是**故意**偏离池子分布的（严重度封顶、机制均分、单多轮 75:25），
    它们的 delta_pp 是设计结果、不是抽样误差；真正的抽样质量看 day / tokens /
    round / group / recalled_from 这几维——这些应该贴近 0。
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["dim", "max_abs_delta_pp", "n_keys", "n_keys_missing",
                    "tolerance_pp", "by_design", "ok"])
        for name, rows in tables.items():
            mx = max((abs(r["delta_pp"]) for r in rows), default=0.0)
            missing = sum(1 for r in rows if r["share_in_pool"] >= 0.01 and r["count"] == 0)
            designed = name in BY_DESIGN
            w.writerow([name, round(mx, 2), len(rows), missing, tol_pp,
                        "yes" if designed else "no",
                        "by_design" if designed else ("yes" if mx <= tol_pp else "NO")])


def pool_stats(pool, tax, params):
    """池子全量分布：bench 是否无偏，全靠这份基准。"""
    tabs = {}
    for name, kf, lf, only_defect in dim_defs(tax):
        src = [c for c in pool if c["worst"]] if only_defect else pool
        cnt = collections.Counter(kf(c) for c in src)
        n = len(src) or 1
        tabs[name] = [{"key": k, "bucket": lf(k), "count": v, "share": round(v / n, 6)}
                      for k, v in sorted(cnt.items())]
    return {
        "pool_size": len(pool),
        "by_severity": dict(sorted(collections.Counter(c["worst"] or "GOLD" for c in pool).items())),
        "by_turn": dict(collections.Counter(c["turn"] for c in pool)),
        "by_model": dict(collections.Counter(c["model"] for c in pool)),
        "n_patterns": len({c["l3"] for c in pool if c["l3"]}),
        "n_mechanisms": len({c["l2"] for c in pool if c["l2"]}),
        "n_stages": len({c["l1"] for c in pool if c["l1"]}),
        "dims": tabs,
        "params": params,
    }


def tier_stats(tier, plan, bench, pool, tax, params):
    return {
        "tier": tier,
        "requested": plan["requested"],
        "actual": plan["actual"],
        "shortfall": plan["shortfall"],
        "severity_target": plan["severity_target"],
        "severity_actual": dict(sorted(collections.Counter(
            c["worst"] or "GOLD" for c in bench).items())),
        "stage_actual": {k: v for k, v in sorted(collections.Counter(
            c["l1"] for c in bench if c["worst"]).items())},
        "mechanism_actual": {k: v for k, v in sorted(collections.Counter(
            c["l2"] for c in bench if c["worst"]).items())},
        "turn_actual": dict(collections.Counter(c["turn"] for c in bench)),
        "pool_size": len(pool),
        "pool_by_severity": dict(sorted(collections.Counter(
            c["worst"] or "GOLD" for c in pool).items())),
        "params": params,
    }


def write_stats(stats_dir, tier, plan, bench, pool, tax, params):
    os.makedirs(stats_dir, exist_ok=True)
    # 只覆盖写不删旧文件的话，下线过的维度（如 difficulty）会一直留在目录里误导人
    for f in glob.glob(os.path.join(stats_dir, "tier_%d.*" % tier)):
        os.unlink(f)
    st = tier_stats(tier, plan, bench, pool, tax, params)
    jdump(st, os.path.join(stats_dir, "tier_%d.json" % tier))
    tabs = all_tables(pool, bench, tax)
    write_dim_csvs(stats_dir, tier, tabs)
    write_coverage(os.path.join(stats_dir, "tier_%d.coverage.csv" % tier), tabs)
    write_crosstab(os.path.join(stats_dir, "tier_%d.crosstab_stage_mechanism.csv" % tier),
                   pool, bench, tax)
    return st


TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DATASET_README.md")


def write_readme(out, manifest, params):
    """把当前 manifest 的数字灌进模板，生成数据目录的 README。"""
    pool = manifest.get("pool") or {}
    quality = manifest.get("quality") or {}
    rows = []
    for model, info in (manifest.get("models") or {}).items():
        for tier, st in sorted((info.get("tiers") or {}).items(), key=lambda kv: int(kv[0])):
            rows.append("| %s | %s | %s | %s |" % (
                model, tier, st.get("actual"), st.get("shortfall", 0)))
    tier_table = ("| 模型 | 档位 | 实际条数 | shortfall |\n|---|---|---|---|\n"
                  + "\n".join(rows)) if rows else "（本次未抽档位）"

    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        text = fh.read()
    repl = {
        "@@BUILT_AT@@": manifest.get("built_at", ""),
        "@@VERSION@@": manifest.get("version", ""),
        "@@SEED@@": (manifest.get("params") or {}).get("seed", ""),
        "@@POOL_CASES@@": str(pool.get("cases", 0)),
        "@@POOL_EXPERT@@": str((pool.get("cases_by_model") or {}).get("aime_expert", 0)),
        "@@POOL_FAST@@": str((pool.get("cases_by_model") or {}).get("aime_fast", 0)),
        "@@LAST_REFRESH@@": pool.get("last_refresh", ""),
        "@@USABLE@@": str(pool.get("usable_cases", 0)),
        "@@JOIN_RATE@@": "%.1f%%" % ((quality.get("join_rate") or 0) * 100),
        "@@TIER_TABLE@@": tier_table,
        "@@LONG_TOKENS@@": str((manifest.get("params") or {}).get("long_tokens", 65536)),
        "@@JOIN_FAILED@@": str(quality.get("join_failed_dropped", 0)),
        "@@DUP@@": str(pool.get("dup_collapsed", 0)),
        "@@Q_SHORT@@": str(quality.get("q_too_short_dropped", 0)),
        "@@Q_URL@@": str(quality.get("q_url_only_dropped", 0)),
        "@@Q_FOLLOW@@": str(quality.get("q_short_followup_kept", 0)),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
