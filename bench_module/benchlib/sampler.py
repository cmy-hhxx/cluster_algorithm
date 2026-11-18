# -*- coding: utf-8 -*-
"""分层取样：配额分配 + 确定性抽样 + 档位嵌套。

核心约束（2026-11-18 与用户确认）：
  1. 每档位是一份显式配额表：golden 20%，P0 封顶 25%，其余 P1:P2:P3:P4 = 4:3:2:1；
  2. 每个 L2 机制在每档至少 1 条（不足时向别的机制借额度）；
  3. 单/多轮 = 75:25；
  4. 每次重抽，但同一 seed + 同一池子 → 结果完全一致；
  5. tier_500 ⊂ tier_1000 ⊂ ...（档位单调递增，抽样顺序prefix 相同）。
"""
import collections

from .common import stable_hash

GOLD = "GOLD"
SEVS = ["P0", "P1", "P2", "P3", "P4"]
REST_W = {"P1": 4, "P2": 3, "P3": 2, "P4": 1}
TURN_W = {"single": 0.75, "multi": 0.25}


def allocate(total, weights, caps, min_one=False):
    """按权重把 total 个名额分给 keys，遵守 caps（可用量），可要求每个 key 至少 1。

    返回 {key: n}。用"先按比例取整、再按余数逐个补"的最大余数法，不会无限循环。
    """
    keys = [k for k in weights if caps.get(k, 0) > 0]
    got = {k: 0 for k in weights}
    budget = min(int(total), sum(caps[k] for k in keys))
    if budget <= 0 or not keys:
        return got

    if min_one:
        for k in sorted(keys, key=lambda k: (-weights[k], k)):
            if budget <= 0:
                break
            got[k] = 1
            budget -= 1
            if got[k] >= caps[k]:
                continue

    active = [k for k in keys if got[k] < caps[k]]
    while budget > 0 and active:
        wmap = {k: max(0.0, weights.get(k, 0.0)) for k in active}
        wsum = sum(wmap.values())
        if wsum <= 0:
            wmap = {k: 1.0 for k in active}
            wsum = float(len(active))
        raw = {k: budget * wmap[k] / wsum for k in active}
        add = {k: min(int(raw[k]), caps[k] - got[k]) for k in active}
        taken = sum(add.values())
        if taken == 0:
            # 比例份额都 <1：按余数从大到小各补 1 个
            order = sorted(active, key=lambda k: (-(raw[k] - int(raw[k])), k))
            for k in order:
                if budget <= 0:
                    break
                if got[k] < caps[k]:
                    got[k] += 1
                    budget -= 1
            active = [k for k in active if got[k] < caps[k]]
            continue
        for k, v in add.items():
            got[k] += v
        budget -= taken
        active = [k for k in active if got[k] < caps[k]]
    return got


def severity_targets(n, caps, params):
    """档位 n 的 severity 配额：golden 20% / P0 封顶 25% / 其余 4:3:2:1。"""
    golden = min(caps.get(GOLD, 0), int(round(n * params["golden_share"])))
    bad_avail = sum(caps.get(s, 0) for s in SEVS)
    bad_budget = min(n - golden, bad_avail)
    p0 = min(caps.get("P0", 0), int(round(n * params["p0_cap"])), bad_budget)
    rest = max(0, bad_budget - p0)
    rest_t = allocate(rest, REST_W, {s: caps.get(s, 0) for s in REST_W})
    out = {GOLD: golden, "P0": p0}
    out.update(rest_t)
    return out


def _order(cases, seed):
    """分层内排序：先按 L1 轮转（保证环节覆盖），桶内按 (seed, trace_id) 哈希。

    纯哈希排序会让某个环节因为样本多而占满小档；轮转把它摊平。
    """
    by_l1 = {}
    for c in cases:
        by_l1.setdefault(c["l1"] or "NA", []).append(c)
    for k in by_l1:
        by_l1[k].sort(key=lambda c: stable_hash(seed, c["trace_id"]))
    keys = sorted(by_l1)
    out = []
    i = 0
    while True:
        added = 0
        for k in keys:
            lst = by_l1[k]
            if i < len(lst):
                out.append(lst[i])
                added += 1
        if not added:
            break
        i += 1
    return out


def plan_tiers(pool, tiers, params):
    """返回 {tier: {'cases': [...], 'severity_target': {...}, 'shortfall': n}}。"""
    seed = params["seed"]
    buckets = {}
    for c in pool:
        sev = c["worst"] or GOLD
        mech = c["l2"] or "NA"
        buckets.setdefault(sev, {}).setdefault(mech, {}).setdefault(c["turn"], []).append(c)
    for sev in buckets:
        for mech in buckets[sev]:
            for turn in buckets[sev][mech]:
                buckets[sev][mech][turn] = _order(buckets[sev][mech][turn], seed)

    caps_sev = {sev: sum(len(t) for turns in cells.values() for t in turns.values())
                for sev, cells in buckets.items()}
    prev = collections.defaultdict(int)
    plans = {}
    for n in tiers:
        st = severity_targets(n, caps_sev, params)
        cell_target = {}
        for sev, tgt in st.items():
            if tgt <= 0 or sev not in buckets:
                continue
            mech = buckets[sev]
            caps_m = {m: sum(len(t) for t in turns.values()) for m, turns in mech.items()}
            mt = allocate(tgt, {m: 1.0 for m in mech}, caps_m, min_one=True)
            for m, mtg in mt.items():
                if mtg <= 0:
                    continue
                turns = mech[m]
                caps_t = {t: len(turns[t]) for t in turns}
                tt = allocate(mtg, TURN_W, caps_t, min_one=True)
                for t, v in tt.items():
                    if v > 0:
                        cell_target[(sev, m, t)] = v

        for k in list(prev):
            cell_target.setdefault(k, 0)
        for k, v in cell_target.items():
            prev[k] = max(prev[k], v)      # 档位变大时不能把上一档的题吐出来

        total = sum(prev.values())
        while total > n:
            over = [(k, prev[k] - cell_target.get(k, 0)) for k in prev
                    if prev[k] > cell_target.get(k, 0) and prev[k] > 1]
            if over:
                over.sort(key=lambda kv: (-kv[1], kv[0]))
                k = over[0][0]
            else:
                k = max(prev, key=lambda k: (prev[k], k))
                if prev[k] <= 0:
                    break
            prev[k] -= 1
            total -= 1

        picked, shortfall = [], 0
        for key in sorted(prev):
            sev, mech, turn = key
            lst = buckets.get(sev, {}).get(mech, {}).get(turn, [])
            v = prev[key]
            if v > len(lst):
                shortfall += v - len(lst)
                v = len(lst)
            picked.extend(lst[:v])

        plans[n] = {"cases": picked, "severity_target": st, "shortfall": shortfall,
                    "requested": n, "actual": len(picked)}
    return plans
