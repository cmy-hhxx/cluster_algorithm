# -*- coding: utf-8 -*-
"""流式数据摄入与历史聚类树增量基线加载器。

支持两种归因输入格式：
  1) 标准多轮归因 JSONL：`{"session_meta": ..., "turns": [{"meta": {"trace_id": ...}, "problem": [...]}]}`
  2) 扁平单行问题 JSONL：`{"trace_id": ..., "component": ..., "label": ..., "definition": ..., "reason": ...}`
"""
import collections
import glob
import hashlib
import json
import os
import re

from .tax_store import TaxonomyStore

SKIP_DIRS = {".runs", ".delivery", ".archive", ".git", ".staging", "cluster",
             "comparison", "criteria", "__pycache__", "taxonomy", "_work"}
SKIP_PREFIXES = ("0_daily_eval-pre", "1_lost_user_eval-pre", ".bak", "backup", "stress-")
CHANNEL_PREFIX = {"wencai": "bc_wc_", "aime": "bc_ai_"}


def clean(s) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        return str(s)
    if any("\ud800" <= c <= "\udfff" for c in s):
        return s.encode("utf-8", "ignore").decode("utf-8")
    return s


def usable_sid(s) -> str:
    v = clean(s).strip()
    return "" if v.lower() in ("", "none", "null", "nan", "n/a", "-") else v


def as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except Exception:
            return []
    return []


def norm_question(q: str) -> str:
    return re.sub(r"\s+", "", q or "")


def norm_minute(ts: str) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", (ts or "").strip())
    return f"{m.group(1)} {m.group(2)}" if m else ""


def iso_time(ts: str) -> str:
    s = (ts or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
        return s
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$", s)
    return f"{m.group(1)}T{m.group(2)}+08:00" if m else ""


def bad_case_id(channel: str, question: str, request_time: str) -> str:
    q, t = norm_question(question), norm_minute(request_time)
    if not q or not t:
        return ""
    h = hashlib.sha256(f"{channel}\x1f{q}\x1f{t}".encode("utf-8")).hexdigest()[:16]
    prefix = CHANNEL_PREFIX.get(channel, f"bc_{re.sub(r'[^a-z0-9]', '', channel.lower())[:4] or 'ext'}_")
    return prefix + h


def guess_channel(path: str) -> str:
    p = path.replace("\\", "/")
    low = p.lower()
    if "/1_aime/" in p or p.startswith("1_aime/") or "aime" in low:
        return "aime"
    if "/0_wencai/" in p or "wencai" in low:
        return "wencai"
    return "default"


def read_lines(path: str):
    with open(path, encoding="utf-8", errors="surrogatepass", newline="") as fh:
        for line in fh:
            yield line[:-1] if line.endswith("\n") else line


def discover_input_files(input_path: str):
    if os.path.isfile(input_path):
        return [os.path.abspath(input_path)]
    out = []
    root = os.path.abspath(input_path)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIRS and not d.startswith(SKIP_PREFIXES)
        )
        jsonl_files = sorted(fn for fn in filenames if fn.endswith(".jsonl"))
        attr_files = [fn for fn in jsonl_files if "attribution" in fn]
        chosen = attr_files if attr_files else [
            fn for fn in jsonl_files
            if fn not in ("item_index.jsonl", "items.jsonl", "locators.jsonl", "taxonomy_evolution.jsonl")
            and not fn.endswith(".out.jsonl")
        ]
        for fn in chosen:
            out.append(os.path.join(dirpath, fn))
    return sorted(out)


def load_cases(cases_path: str):
    cases = {}
    if not cases_path or not os.path.exists(cases_path):
        return cases
    files = [cases_path] if os.path.isfile(cases_path) else sorted(
        glob.glob(os.path.join(cases_path, "**", "*.jsonl"), recursive=True)
    )
    for f in files:
        if any(part.startswith(".") for part in os.path.relpath(f, os.path.dirname(cases_path)).split(os.sep)):
            continue
        for line in read_lines(f):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            tid = clean(r.get("trace_id") or r.get("case_id"))
            if not tid:
                continue
            meta = r.get("meta") or {}
            trs = r.get("traces")
            tm = (trs[0].get("meta") or {}) if isinstance(trs, list) and trs and isinstance(trs[0], dict) else {}
            cases[tid] = {
                "case_id": clean(r.get("case_id") or tid),
                "question": clean(r.get("question") or meta.get("question") or tm.get("question") or tm.get("question_zh") or ""),
                "session_id": clean(r.get("session_id") or meta.get("session_id") or tm.get("session_id") or ""),
                "request_time": clean(r.get("request_time") or meta.get("request_time") or ""),
                "batch_id": clean(r.get("batch_id") or ""),
                "model": clean(r.get("model") or r.get("version") or tm.get("mode") or ""),
            }
    return cases


def load_baseline_from_tree(tree_dir: str):
    """从既有聚类树目录读取历史已归类条目（key = (trace_id, str(problem_index))）。"""
    if not tree_dir or not os.path.isdir(tree_dir):
        return {}
    idx_path = os.path.join(tree_dir, "item_index.jsonl")
    if not os.path.isfile(idx_path):
        return {}
    baseline = {}
    for line in read_lines(idx_path):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        tid = clean(r.get("trace_id") or r.get("case_id"))
        path = clean(r.get("path"))
        if not tid or not path:
            continue
        pi = int(r.get("problem_index") or 0)
        sid = usable_sid(r.get("session_id")) or tid
        rtime = clean(r.get("request_time") or r.get("date") or r.get("batch_id"))
        dt = clean(r.get("date") or r.get("batch_id") or (rtime[:10] if rtime else ""))
        baseline[(tid, str(pi))] = {
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
            "problem_index": pi,
            "problem_id": clean(r.get("problem_id")),
            "severity": clean(r.get("severity")),
            "component": clean(r.get("component") or ""),
            "component_raw": clean(r.get("component_raw") or r.get("component") or ""),
            "label": clean(r.get("label") or (r.get("definition") or "")[:40]),
            "definition": clean(r.get("definition")),
            "reason": clean(r.get("reason")),
            "stage": path.split(".")[0],
            "path": path,
            "edge": bool(r.get("edge", False)),
        }
    return baseline


def _iter_problem_entries(rec: dict, rel: str, ln: int):
    """兼容嵌套 turns[].problem[] 与单行扁平问题记录。"""
    if isinstance(rec.get("turns"), list):
        smeta = rec.get("session_meta") or {}
        src_rel = clean((rec.get("_source") or {}).get("original_file")) or rel
        for ti, turn in enumerate(rec.get("turns") or []):
            tmeta = (turn or {}).get("meta") or {}
            tid = clean(tmeta.get("trace_id") or smeta.get("trace_id") or rec.get("trace_id"))
            if not tid:
                tid = hashlib.sha256(f"{rel}:{ln}:{ti}".encode("utf-8")).hexdigest()[:16]
            for pi, p in enumerate(as_list((turn or {}).get("problem"))):
                if isinstance(p, dict):
                    yield ti, pi, tid, smeta, tmeta, src_rel, p
        return

    # 扁平格式：一行即一条归因问题
    if rec.get("component") and (rec.get("definition") or rec.get("label")):
        tid = clean(rec.get("trace_id") or rec.get("case_id"))
        if not tid:
            tid = hashlib.sha256(f"{rel}:{ln}".encode("utf-8")).hexdigest()[:16]
        pi = int(rec.get("problem_index", 0) or 0)
        ti = int(rec.get("turn_index", 0) or 0)
        smeta = {
            "session_id": rec.get("session_id"),
            "question": rec.get("question"),
            "request_time": rec.get("request_time") or rec.get("date"),
            "user_id": rec.get("user_id"),
        }
        yield ti, pi, tid, smeta, rec, rel, rec


def scan_raw_items(input_path: str, cases_path: str, channel: str, tax: TaxonomyStore, records=None):
    """解析输入文件或内存记录列表，返回 (raw_items_list, unknown_components_samples, ingest_stats)。"""
    files = discover_input_files(input_path) if input_path else []
    if not files and not records:
        raise FileNotFoundError(f"未提供有效输入数据: input={input_path!r}")

    cases = load_cases(cases_path)
    base_dir = (
        (input_path if os.path.isdir(input_path) else os.path.dirname(os.path.abspath(input_path)))
        if input_path else "."
    )
    seen = set()
    items = []
    unknown_samples = collections.defaultdict(list)
    stats = collections.Counter()

    def _consume_record(rec, rel: str, model_dir: str, ch: str, ln: int):
        if not isinstance(rec, dict):
            return
        for ti, pi, tid, smeta, tmeta, src_rel, p in _iter_problem_entries(rec, rel, ln):
            key = (tid, str(pi))
            if key in seen:
                stats["dup_items"] += 1
                continue
            seen.add(key)

            c = cases.get(tid) or {}
            if c:
                stats["case_joined"] += 1
            else:
                stats["case_missing"] += 1
            question = c.get("question") or clean(smeta.get("question") or tmeta.get("question"))
            sid = usable_sid(c.get("session_id") or smeta.get("session_id") or tmeta.get("session_id"))
            if not sid:
                sid = tid
                stats["session_filled"] += 1
            rtime = c.get("request_time") or clean(smeta.get("request_time") or rec.get("request_time"))
            batch = c.get("batch_id") or clean(rec.get("batch_id")) or (rtime[:10] if rtime else "")
            model = c.get("model") or clean(rec.get("model")) or model_dir
            cid = c.get("case_id") or tid
            bid = bad_case_id(ch, question, rtime)

            comp_raw = clean(p.get("component")).strip()
            tool, comp_label, stage, role_tax, is_reg = tax.canon_component(comp_raw)
            role = clean(p.get("component_role")) or role_tax
            family = clean(p.get("component_family"))

            it = {
                "key": key,
                "date": batch or (rtime[:10] if rtime else ""),
                "batch_id": batch,
                "model": model,
                "src_rel": src_rel,
                "case_id": cid,
                "trace_id": tid,
                "session_id": sid,
                "user_id": clean(smeta.get("user_id")),
                "question": question,
                "request_time": rtime,
                "request_time_iso": iso_time(rtime),
                "bad_case_id": bid,
                "channel": ch,
                "turn_index": ti,
                "problem_index": pi,
                "problem_id": clean(p.get("problem_id")),
                "severity": clean(p.get("severity")),
                "component": tool,
                "component_label": comp_label,
                "component_raw": comp_raw,
                "component_role": role,
                "component_family": family,
                "stage": stage,
                "label": clean(p.get("label")),
                "definition": clean(p.get("definition")),
                "reason": clean(p.get("reason")),
            }
            items.append(it)
            if not is_reg or not stage:
                if len(unknown_samples[comp_raw]) < 3:
                    unknown_samples[comp_raw].append({
                        "component_raw": comp_raw,
                        "component_role": role,
                        "component_family": family,
                        "label": it["label"],
                        "definition": it["definition"][:240],
                        "reason": it["reason"][:240],
                    })

    for f in files:
        rel = os.path.relpath(f, base_dir).replace(os.sep, "/")
        model_dir = os.path.basename(os.path.dirname(f))
        ch = guess_channel(f) if channel == "auto" else channel
        for ln, line in enumerate(read_lines(f), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                stats["bad_lines"] += 1
                continue
            _consume_record(rec, rel, model_dir, ch, ln)

    if records:
        ch_mem = "default" if channel == "auto" else channel
        for ln, rec in enumerate(records, 1):
            _consume_record(rec, "<memory>", "", ch_mem, ln)

    stats["files"] = len(files) + (1 if records else 0)
    stats["items"] = len(items)
    return items, dict(unknown_samples), dict(stats)
