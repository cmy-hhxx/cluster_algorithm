# -*- coding: utf-8 -*-
"""数据装载：归因 → case 表；聚类 item_index → 错误模式标签；问句去重。

只读。所有路径都由调用方传入，产物里不写任何绝对路径。
"""
import collections
import glob
import os

from .common import clean, iso_time, norm_question, read_jsonl, request_day

SKIP_DIRS = {".runs", ".delivery", ".archive", ".git", ".staging", "cluster",
             "comparison", "criteria", "__pycache__"}
SKIP_PREFIX = ("0_daily_eval-pre", ".bak", "backup", "stress-", "0_daily_eval_input_")
SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


def _attr_files(root, models):
    """找归因源：<root>/<组>/<model>/*attribution*.jsonl，跳过状态/备份/压测目录。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in SKIP_DIRS
                             and not d.startswith(SKIP_PREFIX))
        model = os.path.basename(dirpath)
        if models and model not in models:
            continue
        for fn in sorted(filenames):
            if "attribution" in fn and fn.endswith(".jsonl"):
                out.append((os.path.join(dirpath, fn), model))
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def scan_attribution(root, models, stats):
    """归因 → {(model, trace_id): case}。一条归因记录 = 一 turn = 一 trace。"""
    cases = {}
    files = _attr_files(root, models)
    stats["attr_files"] = len(files)
    for path, model in files:
        group = os.path.relpath(os.path.dirname(os.path.dirname(path)), root).replace("\\", "/")
        for rec in read_jsonl(path):
            sm = rec.get("session_meta") or {}
            for turn in rec.get("turns") or []:
                tm = turn.get("meta") or {}
                tid = clean(tm.get("trace_id")) or clean(rec.get("trace_id"))
                if not tid:
                    stats["no_trace_id"] += 1
                    continue
                key = (model, tid)
                c = cases.get(key)
                if c is None:
                    q = clean(sm.get("question")) or clean(tm.get("question"))
                    rt = iso_time(sm.get("request_time") or tm.get("timestamp"))
                    c = cases[key] = {
                        "model": model,
                        "trace_id": tid,
                        "session_id": clean(sm.get("session_id")) or clean(tm.get("session_id")),
                        "question": q,
                        "norm_q": norm_question(q),
                        "request_time": rt,
                        # 归因记录本身大多没有 batch_id：用请求日兜，定位原文时点名的就是这天的文件
                        "batch_id": clean(rec.get("batch_id")) or request_day(rt),
                        "group": group,
                        "session_round": _int(sm.get("session_round")) or _int(tm.get("turn_index")),
                        "turn_index": _int(tm.get("turn_index")),
                        "total_tokens": _int(tm.get("total_tokens")),
                        "input_tokens": _int(tm.get("input_tokens")),
                        "difficulty": clean(tm.get("difficulty")),
                        "recalled_from": clean(sm.get("recalled_from")),
                        "comefrom": clean(sm.get("comefrom")),
                        "source": clean(sm.get("source")),
                        "problems": [],
                        "_p": {},
                    }
                for i, p in enumerate(turn.get("problem") or []):
                    if not isinstance(p, dict):
                        continue
                    sev = clean(p.get("severity"))
                    if sev not in SEV_ORDER:
                        stats["bad_severity"] += 1
                        continue
                    # 同一 trace 可能被多份归因覆盖：按 problem_index 去重，保留先出现的
                    if i in c["_p"]:
                        continue
                    c["_p"][i] = True
                    c["problems"].append({
                        "index": i,
                        "severity": sev,
                        "label": clean(p.get("label")),
                        "definition": clean(p.get("definition")),
                        "component": clean(p.get("component")),
                        "component_role": clean(p.get("component_role")),
                        "component_family": clean(p.get("component_family")),
                    })
    for c in cases.values():
        c.pop("_p", None)
    stats["cases_raw"] = len(cases)
    return cases


def load_item_index(cluster_root, models):
    """聚类产物 item_index.jsonl → {(model, trace_id, problem_index): (path, component)}。

    path 形如 S9.M5.1，是分类法的叶 id，也是 L1/L2/L3 的唯一真源。
    """
    idx = {}
    files = 0
    for model in models:
        p = os.path.join(cluster_root, model, "item_index.jsonl")
        if not os.path.isfile(p):
            continue
        files += 1
        for r in read_jsonl(p):
            tid = clean(r.get("trace_id"))
            if not tid:
                continue
            try:
                pi = int(r.get("problem_index"))
            except (TypeError, ValueError):
                continue
            path = clean(r.get("path"))
            if not path.startswith("S"):
                continue
            idx[(model, tid, pi)] = (path, clean(r.get("component")))
    return idx, files


def _valid_path(p):
    parts = p.split(".")
    return len(parts) == 3 and parts[0].startswith("S") and parts[1].startswith("M")


def resolve_patterns(cases, idx, tax, stats):
    """给每个 problem 补齐 L1/L2/L3；join 不上就用 component 兜到 L1。"""
    for c in cases.values():
        for p in c["problems"]:
            hit = idx.get((c["model"], c["trace_id"], p["index"]))
            path = hit[0] if hit else ""
            if path and _valid_path(path):
                p["path"] = path
                p["path_source"] = "join"
                stats["join_ok"] += 1
            else:
                p["path"] = ""
                p["path_source"] = "component"
                stats["join_miss"] += 1
            stage = path.split(".")[0] if path else tax.stage_of_component(p["component"])
            p["l1"] = stage
            p["l2"] = path.split(".")[1] if path else ""
        if c["problems"]:
            # 主标签 = 最严重的那条；同级优先选 join 上的（有 L2），再按 index
            primary = min(c["problems"],
                          key=lambda p: (SEV_ORDER[p["severity"]],
                                         0 if p["path_source"] == "join" else 1,
                                         p["index"]))
            c["worst"] = primary["severity"]
            c["l1"] = primary["l1"]
            c["l2"] = primary["l2"]
            c["l3"] = primary["path"]
            c["primary_index"] = primary["index"]
        else:
            c["worst"] = ""
            c["l1"] = ""
            c["l2"] = ""
            c["l3"] = ""
            c["primary_index"] = -1
    return cases


def question_from_record(rec):
    """从 eval_input 原文里捞本轮问句：meta.question → translation.question → 最后一条 user 消息。"""
    q = clean((rec.get("meta") or {}).get("question"))
    if norm_question(q):
        return q
    traces = rec.get("traces") or []
    for tr in traces:
        tm = (tr or {}).get("meta") or {}
        tr_q = tm.get("translation") or {}
        if isinstance(tr_q, dict):
            q = clean(tr_q.get("question"))
            if norm_question(q):
                return q
        for m in (tr or {}).get("messages") or []:
            if not isinstance(m, dict):
                continue
            if m.get("type") == "user_question" or m.get("role") == "user":
                q = clean(m.get("content"))
                if norm_question(q):
                    return q
    return ""


def backfill_questions(cases, mat, stats):
    """归因里约 5% 的 case 没带 question（P0/P1 也有上千条）；去重前从 eval_input 补回来。

    这一步必须在 dedupe 之前：没有问句的 case 既没法去重、也不该被当成"无内容"丢掉。
    """
    need = [c for c in cases.values() if not c["norm_q"]]
    if not need:
        return cases
    missing = mat.ensure({c["trace_id"]: (c["model"], c["batch_id"]) for c in need})
    for c in need:
        if c["trace_id"] in missing:
            continue
        rec = mat.read(c["trace_id"])
        if not rec:
            continue
        q = question_from_record(rec)
        if q:
            c["question"] = q
            c["norm_q"] = norm_question(q)
            stats["q_backfilled"] += 1
    return cases


def _better(a, b):
    """a 是否比 b 更适合代表同一问句：早的时间优先，其次问题更多、更严重。"""
    ta = (a["request_time"] or "9999")
    tb = (b["request_time"] or "9999")
    if ta != tb:
        return ta < tb
    if len(a["problems"]) != len(b["problems"]):
        return len(a["problems"]) > len(b["problems"])
    return SEV_ORDER.get(a["worst"], 9) < SEV_ORDER.get(b["worst"], 9)


def dedupe(cases, long_tokens, stats):
    """trace 天然唯一；再按「规范化问句 + 单/多轮」合并重复提问。"""
    best = {}
    for c in cases.values():
        if not c["norm_q"]:
            stats["empty_question"] += 1
            continue
        c["turn"] = "multi" if c["session_round"] > 1 else "single"
        c["long"] = c["total_tokens"] >= long_tokens
        c["day"] = request_day(c["request_time"])
        c["dup_count"] = 1
        k = (c["model"], c["norm_q"], c["turn"])
        cur = best.get(k)
        if cur is None:
            best[k] = c
        elif _better(c, cur):
            c["dup_count"] = cur["dup_count"] + 1
            best[k] = c
        else:
            cur["dup_count"] += 1
    stats["cases_dedup"] = len(best)
    stats["dup_collapsed"] = sum(c["dup_count"] - 1 for c in best.values())
    return sorted(best.values(), key=lambda c: (c["model"], c["trace_id"]))
