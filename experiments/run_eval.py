#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a prompt variant over the 576-item audit eval set and score it."""
import argparse, collections, concurrent.futures, hashlib, json, os, re, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import ROOT, load_items, load_gold, item_text, score, confusion

GATEWAYS = os.environ.get("CLUSTER_GATEWAYS", "http://127.0.0.1:15001/v1,http://127.0.0.1:15002/v1").split(",")
MODEL = os.environ.get("CLUSTER_MODEL", "deepseek-v4.1-flash")
KEY = os.environ.get("EVAL_GATEWAY_API_KEY", "dummy")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(prompt, effort, max_tokens, timeout=900, temperature=None):
    body = {"model": MODEL, "input": prompt, "max_output_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    if effort and effort != "default":
        body["reasoning"] = {"effort": effort}
    payload = json.dumps(body).encode()
    last = None
    for base in GATEWAYS:
        req = urllib.request.Request(base + "/responses", data=payload,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {KEY}"})
        try:
            t0 = time.time()
            with opener().open(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            return resp, time.time() - t0
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(str(last))


def parse_rows(resp):
    text = ""
    for o in resp.get("output") or []:
        for c in o.get("content") or []:
            if c.get("type") == "output_text":
                text += c.get("text", "")
    rows = []
    for line in text.splitlines():
        s = line.strip().rstrip(",")
        if s.startswith("{"):
            try:
                rows.append(json.loads(s))
            except Exception:
                pass
    return rows


def leaf_spec(inv, l1, ids=None):
    out = []
    for n in inv["nodes"]:
        if n["l1"] != l1 or (ids is not None and n["id"] not in ids):
            continue
        out.append(f"### {n['id']} {n['name']}\n定义：{n['definition']}\n包含：{n['include']}\n不包含：{n['exclude']}")
    return "\n".join(out)


RULES = """你是归因聚类流水线里的归类员。任务：给每条归因问题选 1 个类别。
只能选下面列出的类别，一条只能选一个；不许新造类别，不许写“其他”。

判定顺序（命中即停）：
1. 用 component 定环节，这一步不需要你判断。
2. 读 definition 和 reason，问：这个环节本来该做的动作，停在哪一步？
   - 完全没做 → M1
   - 做了，但对象/指标/时段/范围错了 → M2
   - 做了但报错/超时/零行/空返回/被截断 → M3
   - 有返回，但内容不相关/是样板/过期/缺字段 → M4
   - 数据和对象都对，但换算/聚合/口径/比较算错 → M5
   - 结论没有证据，或与已有证据相反 → M6
   - 该说的没说/格式语言不对/答非所问 → M7
3. 在同一个 M 下，按“失效动作 + 对象”选唯一类别。
4. 两个类别都说得通时，选更早失效的那个；仍不确定就选最接近的，并标 uncertain=true。

硬规则：
- 看 reason 里的真实失效点，不要只按 label 里的关键词（“零行”不一定是 M3，“缺失”不一定是 M4）。
- 不要因为某个类别常见就绕开它，也不要为了凑数塞进不相干的类别。
- uncertain 只表示“边界模糊”，不影响你给出的唯一结论。"""


def build_v1(inv, l1, rows):
    body = "\n".join(json.dumps({k: r.get(k, "") for k in ("seq", "component", "label", "definition", "reason")},
                                ensure_ascii=False) for r in rows)
    return f"""{RULES}

## 本环节的类别（共 {len([n for n in inv['nodes'] if n['l1']==l1])} 个）
{leaf_spec(inv, l1)}

## 待判定条目（每行一个 JSON）
{body}

## 输出
只输出 {len(rows)} 行 JSONL，每行：{{"seq": <原样>, "path": "{l1}.M<k>.<j>", "uncertain": <true|false>}}
不要解释，不要代码块，不要多余字段。"""


def add_why(prompt, l1, n):
    head = prompt.split("## 输出要求")[0]
    return head + f"""## 输出要求（先写 m 和 why，再写 path）
只输出 {n} 行 JSONL，每行一个对象，字段顺序固定：
{{"seq": <原样带回>, "m": "M<1-7>", "why": "<不超过 12 个字，写出决定性信号>", "path": "{l1}.M<k>.<j>", "edge": <true|false>}}
m 必须与 path 的机制位一致；why 只写看到的关键信号（如“字段没返回”“条件写死零行”），不写推理过程。
不要解释，不要代码块，不要多余字段。"""


def build_v2(inv, l1, rows, cand):
    body = "\n".join(json.dumps({k: r.get(k, "") for k in ("seq", "component", "label", "definition", "reason")},
                                ensure_ascii=False) for r in rows)
    blocks = []
    for i, r in enumerate(rows):
        ids = cand[r["seq"]]
        blocks.append(f"[{i+1}] " + json.dumps({k: r.get(k, "") for k in ("seq", "component", "label", "definition", "reason")},
                                               ensure_ascii=False) + "\n候选：" + "；".join(ids))
    return f"""{RULES}

## 候选类别（每条只从它自己的候选里选一个）
{leaf_spec(inv, l1)}

## 待判定条目
{chr(10).join(blocks)}

## 输出
只输出 {len(rows)} 行 JSONL，每行：{{"seq": <原样>, "path": "<该条候选之一>", "uncertain": <true|false>}}
不要解释，不要代码块，不要多余字段。"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="v1", choices=["v0", "v1", "v2", "v4"])
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--chunk", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--nonce", default="")
    ap.add_argument("--truncate", type=int, default=0, help="truncate definition/reason to N chars")
    ap.add_argument("--runs", type=int, default=1, help="repeat each chunk N times and majority-vote")
    ap.add_argument("--seqs-file", help="only evaluate seqs listed in this file")
    ap.add_argument("--temperature", type=float, default=None)
    a = ap.parse_args()

    inv = json.load(open(os.path.join(ROOT, "taxonomy/inventory.json"), encoding="utf-8"))
    items, gold = load_items(), load_gold()
    seqs = [s for s in gold if gold[s]["agree"]]
    seqs.sort()
    if a.seqs_file:
        keep = {int(x) for x in open(a.seqs_file).read().split()}
        seqs = [s for s in seqs if s in keep]
    if a.limit:
        seqs = seqs[:a.limit]
    groups = {}
    for s in seqs:
        groups.setdefault(gold[s]["l1"], []).append(s)
    chunks = []
    for l1, ss in sorted(groups.items()):
        for i in range(0, len(ss), a.chunk):
            chunks.append((l1, ss[i:i + a.chunk]))

    os.makedirs(CACHE, exist_ok=True)
    tag = a.tag or f"{a.variant}_{a.effort}_k{a.k}"
    cand = None
    if a.variant == "v2":
        from lib import build_shortlist_index, shortlist
        st = time.time()
        si = build_shortlist_index(inv, items, set(gold))
        cand = {s: [p for p, _ in shortlist(si, items[s], gold[s]["l1"], a.k)] for s in seqs}
        print(f"shortlist built in {time.time()-st:.1f}s k={a.k}", flush=True)

    def run_one(args):
        l1, ss, ridx = args
        rows = [dict(items[s]) for s in ss]
        if a.truncate:
            for r in rows:
                for k in ("definition", "reason"):
                    r[k] = (r.get(k) or "")[:a.truncate]
        if a.variant == "v0":
            import importlib.util
            spec = importlib.util.spec_from_file_location("prod", os.path.join(ROOT, "bin/classify.py"))
            prod = importlib.util.module_from_spec(spec); spec.loader.exec_module(prod)
            rules, inv_md, valid = prod.load_rules(l1)
            prompt = prod.build_prompt(l1, "eval", rows, rules, inv_md)
            fmt = "prod"
        elif a.variant == "v4":
            import importlib.util
            spec = importlib.util.spec_from_file_location("prod", os.path.join(ROOT, "bin/classify.py"))
            prod = importlib.util.module_from_spec(spec); spec.loader.exec_module(prod)
            rules, inv_md, valid = prod.load_rules(l1)
            prompt = add_why(prod.build_prompt(l1, "eval", rows, rules, inv_md), l1, len(rows))
            fmt = "v4"
        elif a.variant == "v1":
            prompt = build_v1(inv, l1, rows)
            fmt = "v1"
        else:
            prompt = build_v2(inv, l1, rows, cand)
            fmt = "v2"
        key = hashlib.sha1((a.variant + "|" + MODEL + "|" + a.effort + "|" + str(a.temperature) + "|" + a.nonce + "|" + str(ridx) + "|" + prompt).encode()).hexdigest()[:20]
        cpath = os.path.join(CACHE, key + ".json")
        if os.path.exists(cpath) and not a.force:
            cached = json.load(open(cpath, encoding="utf-8"))
            return {"l1": l1, "ridx": ridx, "rows": cached["rows"], "usage": cached["usage"], "elapsed": cached["elapsed"], "cache": True}
        resp, dt = call(prompt, a.effort, a.max_tokens, temperature=a.temperature)
        prows = parse_rows(resp)
        usage = resp.get("usage", {})
        json.dump({"rows": prows, "usage": usage, "elapsed": dt, "prompt_chars": len(prompt)},
                  open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
        return {"l1": l1, "ridx": ridx, "rows": prows, "usage": usage, "elapsed": dt, "cache": False}

    t0 = time.time()
    out = []
    tasks = [(l1, ss, i) for (l1, ss) in chunks for i in range(a.runs)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futs = [pool.submit(run_one, c) for c in tasks]
        for f in concurrent.futures.as_completed(futs):
            try:
                r = f.result()
                out.append(r)
                print(f"{r['l1']}#{r.get('ridx','')}: {len(r['rows'])} rows {r['elapsed']:.0f}s "
                      f"cache={r['cache']} tok={r['usage'].get('input_tokens')}/{r['usage'].get('output_tokens')}",
                      flush=True)
            except Exception as e:  # noqa: BLE001
                print("FAILED", e, flush=True)
    runs = collections.defaultdict(dict)
    for r in out:
        for row in r["rows"]:
            runs[row.get("seq")][r.get("ridx", 0)] = row.get("path")
    pred = {}
    edges = {}
    unanimous = {}
    for r in out:
        for row in r["rows"]:
            edges[row.get("seq")] = bool(row.get("edge") or row.get("uncertain"))
    for seq, votes in runs.items():
        c = collections.Counter(votes.values())
        top = c.most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            pred[seq] = votes.get(0, top[0][0])  # tie: first run decides
        else:
            pred[seq] = top[0][0]
        if len(c) == 1:
            unanimous[seq] = top[0][0]
    sc = score(pred, gold)
    sc_vote = score(pred, gold)
    sc_un = score(unanimous, gold)
    n_pred = len([s for s in seqs if s in pred])
    dt = time.time() - t0
    res = {"tag": tag, "variant": a.variant, "effort": a.effort, "k": a.k, "chunk": a.chunk, "runs": a.runs,
           "concurrency": a.concurrency, "n": len(seqs), "predicted": n_pred, "elapsed_s": dt,
           "items_per_min": len(seqs) / max(dt, 1) * 60, "score": sc,
           "score_unanimous": sc_un, "unanimous_n": len(unanimous),
           "votes": {str(k): v for k, v in runs.items()},
           "edges": {str(k): v for k, v in edges.items()},
           "tokens_in": sum((r["usage"].get("input_tokens") or 0) for r in out),
           "tokens_out": sum((r["usage"].get("output_tokens") or 0) for r in out),
           "pred": {str(k): v for k, v in pred.items()},
           "confusion": confusion(pred, gold)}
    os.makedirs(os.path.join(ROOT, "experiments/results"), exist_ok=True)
    json.dump(res, open(os.path.join(ROOT, "experiments/results", tag + ".json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({k: res[k] for k in ("tag", "n", "predicted", "elapsed_s", "items_per_min",
                                         "tokens_in", "tokens_out")}, ensure_ascii=False))
    print("vote", json.dumps(sc, ensure_ascii=False))
    print("unanimous", json.dumps(sc_un, ensure_ascii=False))
    print("confusion", res["confusion"][:12])


if __name__ == "__main__":
    main()
