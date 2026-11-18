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
# 托管网关（LiteLLM）按 key 限流：429 要退避重试，不是"网关挂了"
REQUEST_ATTEMPTS = int(os.environ.get("CLUSTER_REQUEST_ATTEMPTS", "5"))
# 非法 path 重试耗尽后，带着「本环节合法清单」再问一次（不替模型选，只把菜单摆正）
PATH_REPAIR = os.environ.get("CLUSTER_PATH_REPAIR", "1").strip().lower() not in ("0", "false", "no")
REPAIR_LOG = os.environ.get("CLUSTER_REPAIR_LOG",
                            os.path.join(ROOT, "logs", "missing_leaf_candidates.jsonl"))

TEMPERATURE = None
_lock = threading.Lock()
_stats = {"chunks": 0, "calls": 0, "items": 0, "failed": 0, "normalized": 0, "reused": 0,
          "repaired": 0, "nofit": 0}
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
            req = urllib.request.Request(base + "/models",
                                         headers={"Authorization": f"Bearer {API_KEY}"})
            with opener().open(req, timeout=6) as r:
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


def _retry_after(e):
    """从 429/503 里取 Retry-After（秒）；取不到就按指数退避。"""
    ra = 0
    try:
        hdrs = e.headers
        if hdrs and hdrs.get("Retry-After"):
            ra = float(hdrs.get("Retry-After"))
    except Exception:
        ra = 0
    return ra


def _is_throttle(e):
    code = getattr(e, "code", None)
    return code in (429, 503, 502, 504)


def call(gateways, prompt):
    body = {"model": MODEL, "input": prompt, "max_output_tokens": MAX_TOKENS}
    if REASONING and REASONING != "default":
        body["reasoning"] = {"effort": REASONING}
    if TEMPERATURE is not None:
        body["temperature"] = TEMPERATURE
    payload = json.dumps(body).encode()
    # 托管网关（LiteLLM）会按 key 限流，429 不是"网关卡死"，退避后重试同一台即可；
    # 只有真正连不上/鉴权失败才换下一台。整体最多 attempt 轮。
    attempts = max(1, REQUEST_ATTEMPTS)
    last = None
    n = next(_rr) % len(gateways)
    order = gateways[n:] + gateways[:n]
    for attempt in range(attempts):
        throttled = False
        for base in order:
            req = urllib.request.Request(
                base + "/responses", data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
            try:
                with opener().open(req, timeout=TIMEOUT) as r:
                    _stats["calls"] += 1
                    return json.loads(r.read())
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_throttle(e):
                    wait = _retry_after(e) or min(60, 2 * (2 ** attempt))
                    log(f"  限流 {getattr(e, 'code', '?')}，{wait:.0f}s 后重试（第 {attempt + 1}/{attempts} 轮）")
                    time.sleep(wait)
                    throttled = True
                    break
        if throttled:
            continue
        time.sleep(min(30, 2 ** attempt))
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
    """模型给的 path 全都不在本环节合法叶子里：缺叶或幻觉，不猜，直接失败。"""

    def __init__(self, msg, rows=None, repair=None):
        super().__init__(msg)
        self.rows = rows or []
        self.repair = repair or {}   # seq -> {"rejected":.., "outcome":"none"|"invalid"}


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


_INV = {}
_inv_lock = threading.Lock()


def l1_menu(l1):
    """本环节的叶子清单（用于重问时把菜单摆正）与已定义的机制集合。"""
    if not _INV:
        with _inv_lock:
            if not _INV:
                _INV.update(json.load(open(os.path.join(ROOT, "taxonomy", "inventory.json"),
                                           encoding="utf-8")))
    nodes = sorted((n for n in _INV["nodes"] if n["l1"] == l1), key=lambda n: n["id"])
    mechs = sorted({n["l2"] for n in nodes})
    rows = [f"| `{n['id']}` | {n['name']} | {n['l2']} {_INV['l2'][n['l2']]['name']} |"
            for n in nodes]
    return rows, mechs


def _why_rejected(path, mechs):
    mech = path.split(".")[1] if path.count(".") >= 1 else "?"
    if mech not in mechs:
        return f"本环节没有 {mech} 这个机制"
    return f"{mech} 在本环节只有清单里那几个序号"


def build_repair_prompt(l1, chunk, items, rejected, rules):
    menu, mechs = l1_menu(l1)
    missing = [f"M{i}" for i in range(1, 8) if f"M{i}" not in mechs]
    counts = {}
    for p in rejected.values():
        counts[p] = counts.get(p, 0) + 1
    why = "\n".join(f"- `{p}`（{n} 条）：不在清单里，{_why_rejected(p, mechs)}"
                    for p, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    body = "\n".join(json.dumps({k: it.get(k, "") for k in
                                 ("seq", "component", "label", "definition", "reason")},
                                ensure_ascii=False) for it in items)
    return f"""你是归类员，正在复核一批上一轮判定被退回的条目（共 {len(items)} 条）。

{rules}

## 为什么退回
上一轮给出的 path 不在本环节的合法清单里：

{why}

本环节（{l1}）只定义了这些机制：{", ".join(mechs)}。{("没有 " + "、".join(missing) + "。") if missing else ""}

## 本环节的合法叶子（path 只能逐字取自这里）
| 类别 | 名称 | 机制 |
|---|---|---|
{chr(10).join(menu)}

## 待复核条目（每行一个 JSON）
{body}

## 输出要求
只输出恰好 {len(items)} 行 JSONL，每行一个对象：{{"seq": <原样带回>, "path": "<上面的 id>", "uncertain": <true|false>}}。
- path 必须逐字来自上表，不要自造、不要简写、不要编序号。
- 这批本来就是边界条目，选中后标 `uncertain=true`。
- 只有当你确信上表**所有**叶子都会把它归到错误的机制/类别时，才写 `"path": "NONE"` —— NONE 不是新类别、不会写进树里，它只是「分类法缺这一叶」的信号，我们会据此补叶；能选最接近的就不要写 NONE。
- 不要解释、不要代码块、不要额外字段。"""


_repair_lock = threading.Lock()


def note_repair(rec):
    """缺叶候选/重问结果落盘（追加，独立于 work 目录，跑完不被清掉）。"""
    rec["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with _repair_lock:
            os.makedirs(os.path.dirname(REPAIR_LOG) or ".", exist_ok=True)
            with open(REPAIR_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"  缺叶候选记录写失败：{e}")


def repair_once(gateways, l1, chunk, items, last_rows, rules, valid):
    """把被退回的那几行连同一份正确的叶子清单再问一次。

    返回 (ok, rows, nofit, info)：
      ok=True  → rows 是「上一轮合法行 + 重问修好的行」的合并结果
      nofit=True → 模型明确说没有叶子装得下（缺叶），调用方据此快速失败
      都不成立 → 重问也非法，交给调用方按原逻辑二分/失败
    """
    by_seq = {str(r.get("seq")): str(r.get("path", "")) for r in last_rows}
    bad = [it for it in items if by_seq.get(str(it["seq"]), "") not in valid]
    if not bad:
        return False, None, False, {}
    rejected = {it["seq"]: by_seq.get(str(it["seq"]), "") for it in bad}
    prompt = build_repair_prompt(l1, chunk, bad, rejected, rules)
    try:
        rows = parse_rows(call(gateways, prompt))
        _stats["normalized"] += normalize_rows(rows, valid)   # 4 段路径同样收敛到父叶
    except Exception as e:  # noqa: BLE001
        log(f"{chunk}: 重问调用失败（{e}），按原逻辑处理")
        return False, None, False, {}
    want = {it["seq"] for it in bad}
    got = [r.get("seq") for r in rows]
    if len(rows) != len(bad) or set(got) != want or len(set(got)) != len(got):
        log(f"{chunk}: 重问返回 {len(rows)}/{len(bad)} 行，seq 对不上，按原逻辑处理")
        return False, None, False, {}

    nofit, fixed, info = [], [], {}
    for r in rows:
        seq = r.get("seq")
        p = str(r.get("path", "")).strip().strip("`").strip()
        if p.upper() == "NONE":
            nofit.append(seq)
            info[str(seq)] = {"rejected": rejected.get(seq, ""), "outcome": "none"}
            continue
        if p not in valid:
            log(f"{chunk}: 重问仍给出非法 path {p!r}（seq={seq}），按原逻辑处理")
            return False, None, False, {}
        fixed.append(r)
        info[str(seq)] = {"rejected": rejected.get(seq, ""), "outcome": "repaired",
                          "chosen": p}
    if nofit:
        _stats["nofit"] += len(nofit)
        log(f"{chunk}: 重问后模型判定「无叶子可装」{len(nofit)} 条（缺叶候选）："
            f"{[rejected.get(s, '') for s in nofit[:3]]}")
        for s in nofit:
            it = next((x for x in bad if x["seq"] == s), {})
            note_repair({"chunk": chunk, "l1": l1, "seq": s, "outcome": "none",
                         "rejected": rejected.get(s, ""), "component": it.get("component", ""),
                         "label": it.get("label", "")})
        return False, None, True, info

    for r in fixed:
        r["uncertain"] = True          # 重问选中 = 边界判定，下游按 edge 复核
        it = next((x for x in bad if x["seq"] == r.get("seq")), {})
        note_repair({"chunk": chunk, "l1": l1, "seq": r.get("seq"), "outcome": "repaired",
                     "rejected": info[str(r.get("seq"))]["rejected"], "chosen": r["path"],
                     "component": it.get("component", ""), "label": it.get("label", "")})
    _stats["repaired"] += len(fixed)
    badset = {str(s) for s in rejected}
    keep = [r for r in last_rows if str(r.get("seq")) not in badset]
    log(f"{chunk}: 重问修好 {len(fixed)} 行 -> {[r['path'] for r in fixed]}")
    return True, keep + fixed, False, info


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
    if invalid and PATH_REPAIR:
        ok, rows2, nofit, info = repair_once(gateways, l1, chunk, items, last_rows, rules, valid)
        if ok:
            return rows2
        if nofit:
            # 模型明说「没有叶子装得下」：这是缺叶，二分也变不出叶子，直接失败并留痕
            raise InvalidOutput(f"{chunk}: 重问后仍无合适叶子（缺叶候选，见 {REPAIR_LOG}）",
                                last_rows, info)
    if depth >= 2 or len(items) < 20:
        if invalid:
            raise InvalidOutput(f"{chunk}: {last_err}", last_rows)
        raise RuntimeError(f"{chunk}: {last_err}")
    mid = len(items) // 2
    log(f"{chunk}: split {len(items)} -> {mid}+{len(items)-mid} after {last_err}")
    parts = []
    for sub in (items[:mid], items[mid:]):
        parts.append(classify_items(gateways, l1, chunk, sub, rules, inv, valid, retries, depth + 1))
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
        # 不兜底：模型给不出合法叶就是缺叶或幻觉，宁可整片失败也不猜。
        # 但把模型原始 path 落盘，补齐叶/补判据时能直接照着改。
        got = {str(r.get("seq")): str(r.get("path", "")) for r in e.rows}
        recs = [{"chunk": chunk, "seq": it["seq"], "component": it.get("component", ""),
                 "label": it.get("label", ""),
                 "model_path": e.repair.get(str(it["seq"]), {}).get("rejected",
                                                                   got.get(str(it["seq"]), "")),
                 "repair": e.repair.get(str(it["seq"]), {}).get("outcome", "not-tried"),
                 "error": str(e)} for it in todo]
        dst_bad = os.path.join(work, f"unresolved_{chunk}.json")
        with open(dst_bad, "w", encoding="utf-8") as fh:
            json.dump(recs, fh, ensure_ascii=False, indent=1)
        log(f"{chunk}: 非法叶子 -> {dst_bad}（{len(todo)} 行，需补叶/补判据）")
        raise
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
    bits = [f"chunks={_stats['chunks']}/{len(manifest)} items={_stats['items']}",
            f"calls={_stats['calls']} failed={_stats['failed']} normalized={_stats['normalized']}",
            f"reused={_stats['reused']} elapsed={dt:.0f}s"]
    if _stats["repaired"]:
        bits.append(f"repaired={_stats['repaired']}")
    if _stats["nofit"]:
        bits.append(f"nofit={_stats['nofit']}")
    bits.append(f"rate={_stats['items']/max(dt,1)*60:.0f} items/min")
    print("\n" + " ".join(bits))
    if failed:
        for c, e in failed:
            print(f"  FAILED {c}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
