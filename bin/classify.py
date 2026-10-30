#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量归类：用一次模型调用处理一个分片，替代 DSH 子代理。

用法：python3 bin/classify.py --work <run_dir> [--batch 200] [--concurrency 12]
                              [--retries 3] [--force]

分片来自 `run.sh init` 产出的 <run>/assign/*.jsonl（manifest.json 描述每片）。
输出写到 <run>/assign/<chunk>.out.jsonl，每行 {"seq":..,"path":"S3.M4.1","edge":false}。
已存在且 seq 完整的 .out.jsonl 会跳过（--force 强制重跑）。
"""
import argparse, concurrent.futures, itertools, json, os, re, sys, threading, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAYS = [u.strip().rstrip("/") for u in os.environ.get(
    "CLUSTER_GATEWAYS",
    "http://interactive-djahh4fczrhq:5001/v1,http://interactive-s4lvy0mmvu32:5001/v1").split(",") if u.strip()]
MODEL = os.environ.get("CLUSTER_MODEL", "deepseek-v4.1-flash")
API_KEY = os.environ.get("EVAL_GATEWAY_API_KEY", "dummy")
MAX_TOKENS = int(os.environ.get("CLUSTER_MAX_TOKENS", "32768"))
TIMEOUT = int(os.environ.get("CLUSTER_TIMEOUT", "600"))
REASONING = os.environ.get("CLUSTER_REASONING", "low")  # low 快 ~2.3x，质量由 A/B 与抽检把关

_lock = threading.Lock()
_stats = {"chunks": 0, "calls": 0, "items": 0, "failed": 0}


def log(msg):
    with _lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy_gateways():
    out = []
    for base in GATEWAYS:
        try:
            with opener().open(base + "/models", timeout=6) as r:
                if r.status == 200:
                    out.append(base)
        except Exception:
            pass
    return out


def load_rules(l1):
    raw = open(os.path.join(ROOT, "prompts", "01_assign.md"), encoding="utf-8").read()
    # 保留 §1–§5 与 §7（§6 的样例用 P/Q/S/A 前缀，会诱导模型输出非本片 L1 的 path）
    head = raw.split("## 6.")[0]
    tail = raw.split("## 7.")[1].split("## 8.")[0] if "## 7." in raw else ""
    body = head + "\n## 7. 输出契约（含 edge 判定）\n" + tail
    body = body.replace("{{ROOT}}", ROOT).replace("{{L1}}", l1)
    inv = open(os.path.join(ROOT, "taxonomy", f"INV_{l1}.md"), encoding="utf-8").read()
    valid = set(re.findall(r"^\| `(" + re.escape(l1) + r"\.[A-Z0-9.]+)` \|", inv, re.M))
    return body, inv, valid


_rr = itertools.count()


def call(gateways, prompt):
    body = {"model": MODEL, "input": prompt, "max_output_tokens": MAX_TOKENS}
    if REASONING and REASONING != "default":
        body["reasoning"] = {"effort": REASONING}
    payload = json.dumps(body).encode()
    last = None
    n = next(_rr) % len(gateways)
    for base in gateways[n:] + gateways[:n]:
        req = urllib.request.Request(
            base + "/responses", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
        try:
            with opener().open(req, timeout=TIMEOUT) as r:
                _stats["calls"] += 1
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"all gateways failed: {last}")


def parse_rows(resp):
    text = ""
    for o in resp.get("output") or []:
        for c in o.get("content") or []:
            if c.get("type") == "output_text":
                text += c.get("text", "")
    rows = []
    for line in text.splitlines():
        s = line.strip().rstrip(",")
        if not s.startswith("{"):
            continue
        try:
            rows.append(json.loads(s))
        except Exception:
            pass
    return rows


def build_prompt(l1, chunk, items, rules, inv):
    body = "\n".join(json.dumps({k: it.get(k, "") for k in ("seq", "component", "label", "definition", "reason")},
                                ensure_ascii=False) for it in items)
    return f"""你是归因聚类流水线中的归类代理 C-{chunk}，本片 L1={l1}，共 {len(items)} 条。

{rules}

## 叶子清单（path 必须来自这里）
{inv}

## 待判定条目（每行一个 JSON）
{body}

## 输出要求
只输出恰好 {len(items)} 行 JSONL，每行一个对象：{{"seq": <原样带回>, "path": "{l1}.M<k>.<j>", "edge": <true|false>}}。
不要解释、不要代码块、不要额外字段。每条都必须有输出，seq 必须与输入一一对应。"""


def valid_rows(rows, items, l1, valid):
    want = {it["seq"] for it in items}
    got = [r.get("seq") for r in rows]
    if len(got) != len(want) or set(got) != want or len(set(got)) != len(got):
        return False
    for r in rows:
        p = str(r.get("path", ""))
        if not re.fullmatch(rf"{l1}\.M[1-7]\.\d+", p) or p not in valid:
            return False
    return True


def classify_items(gateways, l1, chunk, items, rules, inv, valid, retries, depth=0):
    prompt = build_prompt(l1, chunk, items, rules, inv)
    last_err = None
    for attempt in range(retries):
        try:
            rows = parse_rows(call(gateways, prompt))
            if valid_rows(rows, items, l1, valid):
                return rows
            bad = [str(r.get("path")) for r in rows if str(r.get("path")) not in valid][:3]
            last_err = f"invalid output ({len(rows)}/{len(items)} rows, bad paths {bad})"
            log(f"{chunk}: attempt {attempt+1} {last_err}")
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(2 + attempt * 3)
    if depth >= 2 or len(items) < 20:
        raise RuntimeError(f"{chunk}: {last_err}")
    mid = len(items) // 2
    log(f"{chunk}: split {len(items)} -> {mid}+{len(items)-mid} after {last_err}")
    a = classify_items(gateways, l1, chunk, items[:mid], rules, inv, valid, retries, depth + 1)
    b = classify_items(gateways, l1, chunk, items[mid:], rules, inv, valid, retries, depth + 1)
    return a + b


def done_rows(path, want):
    if not os.path.exists(path):
        return None
    try:
        rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    except Exception:
        return None
    return rows if {r.get("seq") for r in rows} == want and len(rows) == len(want) else None


def run_chunk(gateways, work, meta, args):
    chunk, l1 = meta["chunk"], meta["l1"]
    src = os.path.join(work, "assign", chunk + ".jsonl")
    dst = os.path.join(work, "assign", chunk + ".out.jsonl")
    items = [json.loads(l) for l in open(src, encoding="utf-8") if l.strip()]
    want = {it["seq"] for it in items}
    if not args.force:
        rows = done_rows(dst, want)
        if rows is not None:
            log(f"{chunk}: skip ({len(rows)} rows, already done)")
            return 0
    rules, inv, valid = load_rules(l1)
    t0 = time.time()
    rows = classify_items(gateways, l1, chunk, items, rules, inv, valid, args.retries)
    with open(dst, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"seq": r["seq"], "path": r["path"], "edge": bool(r.get("edge", False))},
                                ensure_ascii=False) + "\n")
    log(f"{chunk}: {len(rows)} rows in {time.time()-t0:.0f}s")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    work = os.path.abspath(a.work)
    manifest = json.load(open(os.path.join(work, "assign", "manifest.json"), encoding="utf-8"))
    gateways = healthy_gateways()
    if not gateways:
        sys.exit("没有可用网关：" + ", ".join(GATEWAYS))
    log(f"work={work} chunks={len(manifest)} concurrency={a.concurrency} gateways={gateways}")

    t0 = time.time()
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futs = {pool.submit(run_chunk, gateways, work, m, a): m for m in manifest}
        for f in concurrent.futures.as_completed(futs):
            m = futs[f]
            try:
                _stats["items"] += f.result()
                _stats["chunks"] += 1
            except Exception as e:  # noqa: BLE001
                failed.append((m["chunk"], str(e)))
                _stats["failed"] += 1
                log(f"{m['chunk']}: FAILED {e}")
    dt = time.time() - t0
    print(f"\nchunks={_stats['chunks']}/{len(manifest)} items={_stats['items']} "
          f"calls={_stats['calls']} failed={_stats['failed']} elapsed={dt:.0f}s "
          f"rate={_stats['items']/max(dt,1)*60:.0f} items/min")
    if failed:
        for c, e in failed:
            print(f"  FAILED {c}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
