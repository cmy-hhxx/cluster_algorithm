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


README_TMPL = """# AIME 动态 bench（数据说明）

> 由 `bench_module` 自动生成。**只读集成品**：本目录不含原始数据，`trace_id` 可回
> `eval_input` 还原现场；任何一行都不带绝对路径。

- 构建时间：{built_at}
- 抽样种子：`{seed}`（同 seed + 同池子 → 同一批题；想换题就换 seed，但**换 seed 后不要和历史分数比**）
- 口径版本：bench_module v{version}
- 主池：`pool/` 下 {pool_cases} 条 case，最近刷新 {last_refresh}
- 可采样（过质量闸后）：{usable} 条；合并重复问句 {dup_collapsed} 条
- join 命中率 {join_rate:.1%}

## 目录

```
{out_name}/
├── manifest.json                 构建参数 + 主池状态 + 每档 requested/actual + 质量指标
├── pool/                         主池（唯一真源，每日增量刷新）
│   ├── aime_expert.jsonl         一行一个 case，只存小字段（无 messages）
│   └── aime_fast.jsonl
├── aime_expert/ | aime_fast/     两个池子分开建，互不掺和
│   ├── pool_stats.json           池子全量分布（bench 是否无偏的基准线）
│   ├── tier_500.jsonl …          一行一个 case，全内联、自包含
│   ├── long/tier_*_long.jsonl    长上下文（total_tokens ≥ {long_tokens}）单独一档
│   └── stats/
│       ├── tier_N.json                          计数 / 配额完成度 / token 分位
│       ├── tier_N.dim_*.csv                     12 个维度长表
│       ├── tier_N.coverage.csv                  每维偏差 + 是否设计性偏差
│       └── tier_N.crosstab_stage_mechanism.csv  环节 × 机制
```

## 一行一个 case 的字段

| 字段 | 说明 |
|---|---|
| `trace_id` / `session_id` | 身份，回源用 |
| `model` | `aime_expert` / `aime_fast` |
| `question` | 本轮问句（归一化前的原样文本） |
| `messages` | **完整上下文**：多轮含前序轮次、工具调用与返回 |
| `message_pool` | 会话级 system/背景消息池 |
| `turn` | `single` / `multi` |
| `session_round` / `turn_index` | 轮次 |
| `severity` | `P0`–`P4` / `GOLD`（GOLD = 归因未发现任何 problem） |
| `primary` | 主标签，`{{l1,l2,l3}}` 形如 `S9` / `M5` / `S9.M5.1` |
| `problems[]` | 全部 problem：`severity / label / path / path_source / component` |
| `stats` | `total_tokens / input_tokens / tool_count / latency / dup_question` |
| `meta` | 轨迹元信息白名单 |
| `bench` | `{{tier, rank, seed, pool_size, stratum}}` — 复算抽样所需 |

## 统计 CSV（长表，直接 group by 画图）

```
dim,key,bucket,count,share_in_bench,share_in_pool,delta_pp
```

- `bucket` 是人读名（`stage` → 答案合成，`mechanism` → 加工计算错误，`pattern` → 叶名）
- `stage / mechanism / pattern` 三张表**只统计有缺陷的 case**（GOLD 无环节/机制/叶，不混进来；
  它在 `severity` 维度里就是 `GOLD` 一档）
- `delta_pp` = bench 占比 − 池子占比（百分点）

### 怎么读 `delta_pp`（重要）

`coverage.csv` 里 `by_design=yes` 的维度（severity / stage / mechanism / pattern / turn_type）
**故意**偏离池子分布：bench 就是要多放 P0、每个机制都要有、单多轮按 75:25，
这些维度的 `delta_pp` 是设计结果，不是抽样误差。

真正衡量抽样质量的是**非**设计维度——`day` / `tokens` / `round` / `group` /
`recalled_from`：这些应该贴近 0。

如果上面这几维哪天飙了，说明抽样产生了时间或来源上的偏斜，要查。

> 难度（difficulty）标签已废弃且大面积缺失，不参与分层与统计。

## 三条必须知道的口径

1. **GOLD 不等于人工确认无问题**，它是"归因模型没发现 problem"。假阴性只能靠下游抽检发现。
2. **档位嵌套**：`tier_500 ⊂ tier_1000 ⊂ tier_5000 ⊂ tier_10000`（同一次构建内）。所以
   "加题后分数掉了"可以直接解读成新增题目更难，而不是换了一批题。
3. **不掺水**：某池子凑不满某档时 `actual < requested`，`manifest.json` 里如实记
   `shortfall`，不会重复计数、不会拿 P2–P4 硬凑。
4. **绝对路径**：产物元字段（trace_id / batch_id / group / bench / stats）里没有任何绝对路径；
   但 `messages` / `message_pool` 是模型原始轨迹，**原样保留**（沙箱里会出现 `/mnt/aime-oss/...`
   之类的路径），不要为了"干净"去改它——改了就不是同一条 trace 了。
"""


def write_readme(out, manifest, params):
    path = os.path.join(out, "README.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(README_TMPL.format(
            built_at=manifest.get("built_at", ""),
            seed=manifest["params"]["seed"],
            version=manifest.get("version", ""),
            pool_cases=manifest["pool"].get("cases", 0),
            last_refresh=manifest["pool"].get("last_refresh", ""),
            usable=manifest["pool"].get("usable_cases", 0),
            dup_collapsed=manifest["pool"].get("dup_collapsed", 0),
            join_rate=manifest["quality"].get("join_rate", 0.0),
            out_name=os.path.basename(out.rstrip("/")),
            long_tokens=params.get("long_tokens", 65536),
        ))
