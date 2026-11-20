# -*- coding: utf-8 -*-
"""数据装载：归因 → case 表；聚类 item_index → 错误模式标签；问句去重。

只读。所有路径都由调用方传入，产物里不写任何绝对路径。
"""
import collections
import glob
import os
import re

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


Q_CUTS = ("\n\n--- ---\n", "\n--- ---\n", "# Content of attachments and URLs",
          "# Attachments and URLs", "# Content of attachments")


def split_question(raw):
    """问句里常被塞进系统注入块（附件/URL 处理说明），把它切掉。

    例："Cuando presenta resultados ... \n\n\n--- ---\n\n# Content of attachments ..."
    → 真问句只有前半段。切完为空就保留原文（宁可脏，不要丢）。
    """
    q = clean(raw)
    cut = min((q.find(m) for m in Q_CUTS if q.find(m) > 0), default=-1)
    if cut <= 0:
        return q, ""
    head = q[:cut].strip()
    return (head, q) if head else (q, "")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def case_from_turn(rec, turn, model, group):
    """一条归因记录里的一个 turn → 一个 case（trace 粒度）。池子和旧扫描路径共用这一套。"""
    sm = rec.get("session_meta") or {}
    tm = turn.get("meta") or {}
    tid = clean(tm.get("trace_id")) or clean(rec.get("trace_id"))
    if not tid:
        return None
    raw_q = clean(sm.get("question")) or clean(tm.get("question"))
    q, qfull = split_question(raw_q)
    rt = iso_time(sm.get("request_time") or tm.get("timestamp"))
    c = {
        "model": model,
        "trace_id": tid,
        "session_id": clean(sm.get("session_id")) or clean(tm.get("session_id")),
        "question": q,
        "question_full": qfull,
        "request_time": rt,
        # 归因记录本身大多没有 batch_id：用请求日兜，定位原文时点名的就是这天的文件
        "batch_id": clean(rec.get("batch_id")) or request_day(rt),
        "group": group,
        "session_round": _int(sm.get("session_round")) or _int(tm.get("turn_index")),
        "turn_index": _int(tm.get("turn_index")),
        "total_tokens": _int(tm.get("total_tokens")),
        "input_tokens": _int(tm.get("input_tokens")),
        "recalled_from": clean(sm.get("recalled_from")),
        "comefrom": clean(sm.get("comefrom")),
        "source": clean(sm.get("source")),
        "problems": [],
    }
    for i, p in enumerate(turn.get("problem") or []):
        if not isinstance(p, dict):
            continue
        sev = clean(p.get("severity"))
        if sev not in SEV_ORDER:
            continue
        c["problems"].append({
            "index": i,
            "severity": sev,
            "label": clean(p.get("label")),
            "definition": clean(p.get("definition")),
            "component": clean(p.get("component")),
            "component_role": clean(p.get("component_role")),
            "component_family": clean(p.get("component_family")),
        })
    return c


def scan_attribution(root, models, stats):
    """归因 → {(model, trace_id): case}（一次性全量扫描，池子增量刷新走 pool.py）。"""
    cases = {}
    files = _attr_files(root, models)
    stats["attr_files"] = len(files)
    for path, model in files:
        group = os.path.relpath(os.path.dirname(os.path.dirname(path)), root).replace("\\", "/")
        for rec in read_jsonl(path):
            for turn in rec.get("turns") or []:
                c = case_from_turn(rec, turn, model, group)
                if not c:
                    stats["no_trace_id"] += 1
                    continue
                key = (model, c["trace_id"])
                cur = cases.get(key)
                if cur is None:
                    cases[key] = c
                    continue
                # 同一 trace 被多份归因覆盖：按 problem_index 去重合并
                seen = {p["index"] for p in cur["problems"]}
                for p in c["problems"]:
                    if p["index"] not in seen:
                        cur["problems"].append(p)
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


def drop_unclassified(cases, stats):
    """只留"解释得清"的 case：每一条 problem 都必须落到分类法叶（S9.M5.1）。

    join 不上的 case 不进 bench——它在档位里既占配额、又说不清是哪一种错误模式。
    GOLD（无 problem）保留：它是明确定义的"无缺陷"类。
    """
    keep = {}
    for key, c in cases.items():
        if c["worst"] and any(not p["path"] for p in c["problems"]):
            # 主标签或任意一条 problem 落不到叶，都算"解释不清"：整条不要
            stats["join_failed_dropped"] += 1
            continue
        keep[key] = c
    return keep


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
            q, qfull = split_question(q)
            c["question"] = q
            c["question_full"] = qfull
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


URL_ONLY = re.compile(r"^\s*https?://\S+\s*$", re.I)


def dedupe(cases, long_tokens, stats, min_chars=6, drop_low_quality=True):
    """trace 天然唯一；再按「规范化问句 + 单/多轮」合并重复提问。

    同时做一道质量闸（`drop_low_quality`）：问句归一化后短于 `min_chars`、或整条就是一个 URL 的，
    直接剔除——它们没有可评的内容，进了 bench 只会稀释分数。剔除条数记进 stats。
    """
    best = {}
    for c in cases.values():
        if not c["norm_q"]:
            stats["empty_question"] += 1
            continue
        multi = c["session_round"] > 1
        if drop_low_quality and not multi:
            # 单轮的短问句/裸 URL 没有可评内容；多轮的追问（"还有呢？"）有上文，不算垃圾
            if len(c["norm_q"]) < min_chars:
                stats["q_too_short"] += 1
                continue
            if URL_ONLY.match(c["question"] or ""):
                stats["q_url_only"] += 1
                continue
        elif multi and len(c["norm_q"]) < min_chars:
            c["short_followup"] = True
            stats["q_short_followup_kept"] += 1
        c["turn"] = "multi" if c["session_round"] > 1 else "single"
        c["long"] = c["total_tokens"] >= long_tokens
        c["day"] = request_day(c["request_time"])
        c["dup_count"] = 1
        if not c["day"] or not c["group"]:
            # 维度值必须完整：缺日期/缺来源会让统计出现空桶，直接不进池
            stats["meta_incomplete_dropped"] += 1
            continue
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
