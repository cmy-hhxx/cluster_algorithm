#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量归类：用一次模型调用处理一个分片，替代 DSH 子代理。

用法：python3 bin/classify.py --work <run_dir> [--concurrency 24]
                              [--effort none|low] [--consensus N] [--temperature 0]
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
REASONING = os.environ.get("CLUSTER_REASONING", "none")  # 默认快；质量模式设 low（+2.6pp，慢 5-6x）

TEMPERATURE = None
_lock = threading.Lock()
_stats = {"chunks": 0, "calls": 0, "items": 0, "failed": 0, "normalized": 0, "fallback": 0, "reused": 0}
_INV = None
_fallbacks = []
_REUSE = {}   # seq -> {path, edge}：复用上一版判定（--reuse），命中条目不再调模型


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
    body = open(os.path.join(ROOT, "prompts", "01_assign.md"), encoding="utf-8").read()
    body = body.replace("{{ROOT}}", ROOT).replace("{{L1}}", l1)
    inv = open(os.path.join(ROOT, "taxonomy", f"INV_{l1}.md"), encoding="utf-8").read()
    valid = set(re.findall(r"^\| `(" + re.escape(l1) + r"\.[A-Z0-9.]+)` \|", inv, re.M))
    return body, inv, valid


_rr = itertools.count()


def call(gateways, prompt):
    body = {"model": MODEL, "input": prompt, "max_output_tokens": MAX_TOKENS}
    if REASONING and REASONING != "default":
        body["reasoning"] = {"effort": REASONING}
    if TEMPERATURE is not None:
        body["temperature"] = TEMPERATURE
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
    return f"""你是归类员。本批共 {len(items)} 条，都已按 component 分到同一个环节；环节不用你判断。

{rules}

## 类别清单（path 必须来自这里）
{inv}

## 待判定条目（每行一个 JSON）
{body}

## 输出要求
只输出恰好 {len(items)} 行 JSONL，每行一个对象：{{"seq": <原样带回>, "path": "{l1}.M<k>.<j>", "uncertain": <true|false>}}。
不要解释、不要代码块、不要额外字段。每条都必须有输出，seq 必须与输入一一对应。"""


class InvalidOutput(RuntimeError):
    """模型给的 path 全都不在本环节合法叶子里（可兜底，不该拖垮整棵树）。"""

    def __init__(self, msg, rows=None):
        super().__init__(msg)
        self.rows = rows or []


def _inventory():
    global _INV
    if _INV is None:
        _INV = json.load(open(os.path.join(ROOT, 'taxonomy', 'inventory.json'), encoding='utf-8'))
    return _INV


def fallback_rows(l1, chunk, items, valid, rows, err):
    """兜底：模型给不出合法 path 时，按 component（再看机制）落本环节最相近的合法叶，全部标 edge。

    只用于「重试 + 二分都救不回来」的少数条目；同时写进 <work>/unresolved_*.json 方便人工复核。
    合法 path 缺失是 taxonomy 缺叶或模型幻觉，不应该让整棵树 rc=1（2026-11-11 aime_expert 事故）。
    """
    by_comp, ids = {}, sorted(valid)
    for n in _inventory()['nodes']:
        if n['id'] in valid:
            for c in n.get('components') or []:
                by_comp.setdefault(c, []).append(n['id'])
    got = {str(r.get('seq')): str(r.get('path', '')) for r in rows}
    out, recs = [], []
    for it in items:
        mp = got.get(str(it['seq']), '')
        bits = mp.split('.')
        mech = bits[1] if len(bits) >= 2 else ''
        cands = by_comp.get(it.get('component') or '', [])
        same = [c for c in cands if c.split('.')[1] == mech]
        path = (same or cands or ids or [''])[0]
        out.append({'seq': it['seq'], 'path': path, 'edge': True})
        recs.append({'chunk': chunk, 'seq': it['seq'], 'component': it.get('component', ''),
                     'label': it.get('label', ''), 'model_path': mp, 'path': path, 'error': err})
    with _lock:
        _fallbacks.extend(recs)
    log(f"{chunk}: FALLBACK {len(items)} rows -> {sorted({r['path'] for r in out})} "
        f"(model paths {sorted(set(got.values()))[:3]})")
    return out


def normalize_rows(rows, valid):
    """Collapse hallucinated 4-segment paths to their valid 3-segment parent."""
    n = 0
    for r in rows:
        p = str(r.get("path", "")).strip().strip("`")
        if p not in valid and p.count(".") == 3:
            parent = p.rsplit(".", 1)[0]
            if parent in valid:
                p = parent
                n += 1
        r["path"] = p
    return n


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
    last_err, last_rows, invalid = None, [], False
    for attempt in range(retries):
        try:
            rows = parse_rows(call(gateways, prompt))
            _stats["normalized"] += normalize_rows(rows, valid)
            if valid_rows(rows, items, l1, valid):
                return rows
            invalid, last_rows = True, rows
            bad = [str(r.get("path")) for r in rows if str(r.get("path")) not in valid][:3]
            last_err = f"invalid output ({len(rows)}/{len(items)} rows, bad paths {bad})"
            log(f"{chunk}: attempt {attempt+1} {last_err}")
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(2 + attempt * 3)
    if depth >= 2 or len(items) < 20:
        if invalid:
            raise InvalidOutput(f"{chunk}: {last_err}", last_rows)
        raise RuntimeError(f"{chunk}: {last_err}")
    mid = len(items) // 2
    log(f"{chunk}: split {len(items)} -> {mid}+{len(items)-mid} after {last_err}")
    parts = []
    for sub in (items[:mid], items[mid:]):
        try:
            parts.append(classify_items(gateways, l1, chunk, sub, rules, inv, valid, retries, depth + 1))
        except InvalidOutput as e:
            parts.append(fallback_rows(l1, chunk, sub, valid, e.rows, str(e)))
    return parts[0] + parts[1]


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
    reused = [{"seq": s, "path": _REUSE[s]["path"], "edge": bool(_REUSE[s].get("edge"))}
              for s in sorted(want) if s in _REUSE]
    todo = [it for it in items if it["seq"] not in _REUSE]
    if not todo:
        with open(dst, "w", encoding="utf-8") as fh:
            for r in reused:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        _stats["reused"] += len(reused)
        log(f"{chunk}: reused {len(reused)} rows（全部复用旧判定，未调模型）")
        return len(reused)
    rules, inv, valid = load_rules(l1)
    t0 = time.time()
    try:
        passes = [classify_items(gateways, l1, chunk, todo, rules, inv, valid, args.retries)
                  for _ in range(max(1, args.consensus))]
    except InvalidOutput as e:
        if not valid:
            raise
        passes = [fallback_rows(l1, chunk, todo, valid, e.rows, str(e))]
    bad = [f for f in _fallbacks if f["chunk"] == chunk]
    if bad:
        _stats["fallback"] += 1
        dst_bad = os.path.join(work, f"unresolved_{chunk}.json")
        with open(dst_bad, "w", encoding="utf-8") as fh:
            json.dump(bad, fh, ensure_ascii=False, indent=1)
        log(f"{chunk}: 已兜底 {len(bad)} 行 -> {dst_bad}（需人工复核分类法）")
    if len(passes) == 1:
        rows = passes[0]
        out = [{"seq": r["seq"], "path": r["path"],
                "edge": bool(r.get("uncertain", r.get("edge", False)))} for r in rows]
    else:
        votes = {}
        for rows in passes:
            for r in rows:
                votes.setdefault(r["seq"], []).append(r["path"])
        out = []
        for seq, paths in votes.items():
            counts = {}
            for p in paths:
                counts[p] = counts.get(p, 0) + 1
            best = max(counts, key=lambda p: (counts[p], p == paths[0]))
            out.append({"seq": seq, "path": best, "edge": len(counts) > 1})
    out = reused + out
    _stats["reused"] += len(reused)
    with open(dst, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"{chunk}: {len(out)} rows in {time.time()-t0:.0f}s"
        + (f"（复用 {len(reused)}，新判 {len(out)-len(reused)}）" if reused else ""))
    return len(out)


def main():
    global REASONING, TEMPERATURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--consensus", type=int, default=1, help="每片跑几遍投票；>1 时用分歧标 edge，成本×N")
    ap.add_argument("--reuse", default="", help="seq->path 复用表（bin/build_reuse_map.py 产出），命中条目不再调模型")
    ap.add_argument("--effort", default=REASONING, help="none/low/medium/high；none 最快")
    ap.add_argument("--temperature", type=float, default=None)
    a = ap.parse_args()
    REASONING, TEMPERATURE = a.effort, a.temperature

    work = os.path.abspath(a.work)
    if a.reuse:
        _REUSE.update({r["seq"]: r for r in (json.loads(l) for l in open(a.reuse, encoding="utf-8") if l.strip())})
        log(f"reuse: {len(_REUSE)} 条命中（--reuse {a.reuse}）")
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
          f"calls={_stats['calls']} failed={_stats['failed']} normalized={_stats['normalized']} "
          f"fallback={_stats['fallback']} reused={_stats['reused']} elapsed={dt:.0f}s "
          f"rate={_stats['items']/max(dt,1)*60:.0f} items/min")
    if failed:
        for c, e in failed:
            print(f"  FAILED {c}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
