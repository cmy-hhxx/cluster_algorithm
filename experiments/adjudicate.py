#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Second-pass adjudication for items where the 3 cheap passes disagreed.

Usage: python3 experiments/adjudicate.py --votes experiments/results/r3_v0_none_x3.json \
         --effort low --tag adj_low
"""
import argparse, collections, concurrent.futures, hashlib, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import ROOT, load_items, load_gold, score
from run_eval import call, parse_rows, CACHE

INV = json.load(open(os.path.join(ROOT, "taxonomy/inventory.json"), encoding="utf-8"))
D = {n["id"]: n for n in INV["nodes"]}


def card(pid):
    n = D[pid]
    return f"### {pid} {n['name']}\n定义：{n['definition']}\n包含：{n['include']}\n不包含：{n['exclude']}"


def prompt(seq, item, cands):
    body = json.dumps({k: item.get(k, "") for k in ("seq", "component", "label", "definition", "reason")},
                      ensure_ascii=False)
    return f"""你是归因聚类的裁决员。下面这条问题在初判时出现了分歧，候选类别只有 {len(cands)} 个。
请读 definition 和 reason，选唯一最符合的一个。不要选候选之外的类别。

判定顺序：先问这个环节该做的动作停在哪一步（没做 / 做错对象 / 执行失败 / 结果不可用 / 算错 / 无依据 / 交付不符），
再按“失效动作 + 对象”落叶子。看 reason 里的真实失效点，不要只按 label 关键词。

候选：
{chr(10).join(card(c) for c in cands)}

条目：
{body}

只输出一行 JSON：{{"seq": {seq}, "path": "<候选之一>"}}。不要解释。"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--votes", required=True)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--tag", default="adj")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    votes = json.load(open(a.votes, encoding="utf-8"))["votes"]
    items, gold = load_items(), load_gold()
    todo = []
    for s, vv in votes.items():
        c = collections.Counter(vv.values())
        if len(c) > 1:
            todo.append((int(s), [p for p, _ in c.most_common()]))
    todo.sort()
    if a.limit:
        todo = todo[:a.limit]
    print(f"adjudicate {len(todo)} items effort={a.effort}", flush=True)

    def one(args):
        seq, cands = args
        p = prompt(seq, items[seq], cands)
        key = hashlib.sha1(("adj|" + a.effort + "|" + a.tag + "|" + p).encode()).hexdigest()[:20]
        cpath = os.path.join(CACHE, key + ".json")
        if os.path.exists(cpath):
            d = json.load(open(cpath, encoding="utf-8"))
            return seq, d["rows"], d["usage"], 0.0
        resp, dt = call(p, a.effort, 4096)
        rows = parse_rows(resp)
        json.dump({"rows": rows, "usage": resp.get("usage", {}), "elapsed": dt},
                  open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
        return seq, rows, resp.get("usage", {}), dt

    pred, t0 = {}, time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for seq, rows, usage, dt in pool.map(one, todo):
            if rows:
                pred[seq] = rows[0].get("path")
            print(f" {seq}: {pred.get(seq)} {dt:.0f}s", flush=True)
    sc = score(pred, gold)
    print("adjudicated", json.dumps(sc, ensure_ascii=False))
    # combined: unanimous from first pass + adjudicated
    combined = {}
    for s, vv in votes.items():
        c = collections.Counter(vv.values())
        if len(c) == 1:
            combined[int(s)] = c.most_common(1)[0][0]
    combined.update(pred)
    sc2 = score(combined, gold)
    print("combined", json.dumps(sc2, ensure_ascii=False))
    json.dump({"tag": a.tag, "effort": a.effort, "n_adj": len(todo), "adj_score": sc,
               "combined": sc2, "pred": {str(k): v for k, v in pred.items()},
               "elapsed_s": time.time() - t0},
              open(os.path.join(ROOT, "experiments/results", a.tag + ".json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
