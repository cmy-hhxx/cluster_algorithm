# -*- coding: utf-8 -*-
"""case 主池：落盘、增量刷新。

这是 bench 的**唯一真源**：一行一个 case（trace 粒度），只存小字段，不存 messages。
档位（500/1000/5000/10000）每次都是从这份池子里抽，不再重扫归因。

增量：`pool/state.json` 记着每个归因文件的 size+mtime，刷新时只解析新增/变更的文件；
已入库的 case 以**最新一次归因**为准（重跑过的批次会修正标签），`first_seen` 保留最早入池时间。
"""
import collections
import os
import time

from .common import (clean, iso_time, jdump, jload, norm_question, read_jsonl,
                     request_day)
from .loader import (SKIP_DIRS, SKIP_PREFIX, case_from_turn, question_from_record,
                     split_question)

POOL_VERSION = 1


def list_attr_files(root, models):
    """归因源文件：<root>/<组>/<model>/*attribution*.jsonl，跳过状态/备份/压测目录。"""
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
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root).replace("\\", "/")
                out.append((p, rel, model))
    return out


def _stat(p):
    st = os.stat(p)
    return [st.st_size, st.st_mtime_ns]


class Pool:
    def __init__(self, pool_dir, models, log=print):
        self.dir = pool_dir
        self.models = list(models)
        self.log = log
        self.state_path = os.path.join(pool_dir, "state.json")
        self.state = jload(self.state_path, {}) or {}
        if self.state.get("version") != POOL_VERSION:
            # 口径变了或首次运行：旧 state 作废，重新全量入库（幂等，不会重复）
            self.state = {"version": POOL_VERSION, "files": {}, "last_refresh": ""}
            self._fresh = True
        else:
            self._fresh = False
        self.cases = {m: {} for m in self.models}

    # ---------- 读写 ----------

    def path(self, model):
        return os.path.join(self.dir, "%s.jsonl" % model)

    def load(self):
        """读池子并补齐派生字段（norm_q / turn / long / day），采样直接用。"""
        self.cases = {m: {} for m in self.models}
        for model in self.models:
            p = self.path(model)
            if not os.path.isfile(p):
                continue
            for rec in read_jsonl(p):
                tid = rec.get("trace_id")
                if tid:
                    self.cases[model][tid] = hydrate(rec)
        return self.cases

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        total = 0
        for model in self.models:
            cases = self.cases.get(model) or {}
            total += len(cases)
            tmp = self.path(model) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for tid in sorted(cases):
                    fh.write(_dump(cases[tid]))
            os.replace(tmp, self.path(model))
        self.state["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.state["cases"] = total
        jdump(self.state, self.state_path)

    # ---------- 增量刷新 ----------

    def refresh(self, attr_root, mat=None):
        """只解析新增/变更的归因文件，合并进池子，然后落盘。"""
        self.load()
        files = list_attr_files(attr_root, self.models)
        changed, new_cases, updated = [], 0, 0
        for path, rel, model in files:
            try:
                sig = _stat(path)
            except OSError:
                continue
            if self.state["files"].get(rel) == sig:
                continue
            changed.append(rel)
            for rec in read_jsonl(path):
                for turn in rec.get("turns") or []:
                    c = case_from_turn(rec, turn, model,
                                       os.path.relpath(os.path.dirname(os.path.dirname(path)),
                                                       attr_root).replace("\\", "/"))
                    if not c:
                        continue
                    old = self.cases[model].get(c["trace_id"])
                    c["first_seen"] = (old or {}).get("first_seen") or _now()
                    c["last_seen"] = _now()
                    c["attr_file"] = rel
                    c["attr_mtime"] = sig[1]
                    if old:
                        updated += 1
                    else:
                        new_cases += 1
                    self.cases[model][c["trace_id"]] = c
            self.state["files"][rel] = sig

        if mat and (new_cases or updated):
            self._backfill(mat)

        # 落盘后再 hydrate：内存里的 case 直接能被采样用（norm_q / turn / day 等派生字段）
        for model in self.models:
            self.cases[model] = {tid: hydrate(c) for tid, c in self.cases[model].items()}
        self.save()
        stats = {"files_total": len(files), "files_changed": len(changed),
                 "new_cases": new_cases, "updated_cases": updated,
                 "pool_cases": sum(len(v) for v in self.cases.values())}
        self.log("  池子：文件 %d（变更 %d），新增 %d，更新 %d，总量 %d"
                 % (stats["files_total"], stats["files_changed"],
                    new_cases, updated, stats["pool_cases"]))
        return stats

    def _backfill(self, mat):
        """归因里没带问句的，从 eval_input 补；只补缺的，命中率靠定位表。"""
        need = [c for m in self.cases for c in self.cases[m].values() if not c["question"]]
        if not need:
            return
        missing = mat.ensure({c["trace_id"]: (c["model"], c["batch_id"]) for c in need})
        n = 0
        for c in need:
            if c["trace_id"] in missing:
                continue
            rec = mat.read(c["trace_id"])
            if not rec:
                continue
            q = question_from_record(rec)
            if q:
                c["question"], c["question_full"] = split_question(q)
                n += 1
        self.log("  从 eval_input 补回问句 %d 条" % n)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


_KEEP = ("model", "trace_id", "session_id", "question", "question_full", "request_time",
         "batch_id", "group", "session_round", "turn_index", "total_tokens",
         "input_tokens", "recalled_from", "comefrom", "source", "problems",
         "first_seen", "last_seen", "attr_file", "attr_mtime")


def _dump(c):
    import json
    return json.dumps({k: c.get(k) for k in _KEEP}, ensure_ascii=False) + "\n"


def hydrate(rec):
    """池里的裸记录 → 采样用的 case（补派生字段，口径与旧版一致）。"""
    c = dict(rec)
    c["question"] = clean(c.get("question"))
    c["question_full"] = clean(c.get("question_full"))
    c["norm_q"] = norm_question(c["question"])
    c["day"] = request_day(c.get("request_time"))
    c["session_round"] = int(c.get("session_round") or 0)
    c["turn"] = "multi" if c["session_round"] > 1 else "single"
    c["total_tokens"] = int(c.get("total_tokens") or 0)
    c["problems"] = c.get("problems") or []
    c["dup_count"] = 1
    c["worst"] = ""
    c["l1"] = c["l2"] = c["l3"] = ""
    return c


def total_cases(cases_by_model):
    return sum(len(v) for v in cases_by_model.values())
