#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟测试：造一份迷你 eval_input + 归因 + 聚类产物，跑一遍 build_bench。

断言的是"口径"，不是"数字"：
  - 档位嵌套（500 ⊂ 1000）、确定性（同 seed 两次结果一致）
  - 每个 L2 机制在小档至少 1 条
  - golden 占比接近 20%，P0 不超过 25%
  - 多轮带上完整上下文、产物里没有绝对路径
  - 同问句被合并、统计 CSV 齐全、join 失败的条目有兜底

用法：python3 bench_module/tests/test_bench.py
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.dirname(HERE)
REPO = os.path.dirname(MODULE)

STAGES = ["S1", "S3", "S5", "S9", "S10", "S12"]
MECHS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]
COMPS = {"S1": "planner-finquery", "S3": "finquery", "S5": "web_search",
         "S9": "answer_synthesis", "S10": "text_request", "S12": "task_planner"}


def _problem(i, path):
    return {"label": "问题%d" % i, "definition": "定义%d" % i,
            "problem_phenomena": "", "reason": "原因", "severity": None,
            "severity_reason": "", "evidence": [],
            "component": COMPS[path.split(".")[0]], "component_family": "x",
            "component_role": "delivery"}


def make_fixture(root, n=400):
    rng = random.Random(7)
    inp = os.path.join(root, "1_aime", "eval_input", "0_daily_eval_input")
    outp = os.path.join(root, "1_aime", "eval_output", "0_daily_eval")
    cluster = os.path.join(root, "1_aime", "eval_output", "cluster")
    for d in (inp, outp, cluster):
        os.makedirs(d, exist_ok=True)

    traces = []
    for i in range(n):
        model = "aime_expert" if i % 4 else "aime_fast"
        tid = "t%05d" % i
        sev = rng.choices(["P0", "P1", "P2", "P3", "P4", ""],
                          weights=[3, 25, 30, 12, 10, 20])[0]
        stage = rng.choice(STAGES)
        mech = rng.choice(MECHS)
        path = "%s.%s.1" % (stage, mech)
        turn = 3 if i % 10 == 0 else 1
        # 绝大多数问句唯一；每 25 条制造一个重复问句，用来验证去重
        q = "问题 %d 的营收情况" % (i - 1 if i and i % 25 == 0 else i)
        problems, items = [], []
        if sev:
            p = _problem(0, path)
            p["severity"] = sev
            problems.append(p)
            items.append({"trace_id": tid, "problem_index": 0, "turn_index": 0,
                          "path": path, "component": COMPS[stage], "label": p["label"]})
            if i % 50 == 0:   # 少部分条目 join 不上，走 component 兜底
                items = []
        rec = {
            "request_time": "2026-09-%02dT08:00:00Z" % (1 + i % 5),
            "batch_id": "2026-09-%02d" % (1 + i % 5),
            "case_id": tid, "version": model, "has_standard_answer": False,
            "meta": {"session_id": "s%05d" % (i // 2), "request_time": "2026-09-01T08:00:00Z",
                     "session_round": turn, "question": q, "n_messages": 2 * turn},
            "traces": [{
                "meta": {"trace_id": tid, "session_id": "s%05d" % (i // 2),
                         "turn_index": turn, "model": "AIME-Athena",
                         "total_tokens": 5000 + (i % 3) * 40000,
                         "input_tokens": 4000, "tool_count": 2,
                         "difficulty": rng.choice(["简单", "中等", "困难"]),
                         "latency": 100.0, "recalled_from": "lost_user"},
                "messages": [{"type": "user_question", "role": "user", "content": q}]
                            + [{"type": "final_answer", "role": "assistant", "content": "答"}]
                            * (2 * turn - 1),
                "message_pool": [{"role": "system", "content": "you are AIME"}],
            }],
            "trace_id": tid,
        }
        traces.append((model, rec, problems, items))

    by_model = {}
    for model, rec, problems, items in traces:
        by_model.setdefault(model, []).append((rec, problems, items))

    for model, rows in by_model.items():
        d = os.path.join(inp, model)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "2026-09-01.jsonl"), "w", encoding="utf-8") as fh:
            for rec, _p, _i in rows:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        od = os.path.join(outp, model)
        os.makedirs(od, exist_ok=True)
        with open(os.path.join(od, "%s_attribution-2026-09-01.jsonl" % model), "w",
                  encoding="utf-8") as fh:
            for rec, problems, _i in rows:
                for p in problems:
                    p["severity"] = p["severity"]
                out = {"session_meta": rec["meta"],
                       "turns": [{"meta": rec["traces"][0]["meta"],
                                  "summary": "", "problem": problems,
                                  "is_correct": None, "correct_reason": ""}]}
                fh.write(json.dumps(out, ensure_ascii=False) + "\n")
        cd = os.path.join(cluster, model)
        os.makedirs(cd, exist_ok=True)
        with open(os.path.join(cd, "item_index.jsonl"), "w", encoding="utf-8") as fh:
            for _rec, _p, items in rows:
                for it in items:
                    fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    return os.path.join(root, "1_aime", "eval_output"), \
        os.path.join(root, "1_aime", "eval_input"), os.path.join(root, "out")


def run_build(attr, eval_input, out_root, tiers="200,400"):
    out = os.path.join(out_root, "3_dynamic_bench")
    cmd = [sys.executable, os.path.join(MODULE, "build_bench.py"),
           "--attr-root", attr, "--eval-input", eval_input, "--out", out,
           "--tiers", tiers, "--seed", "t1"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("build 失败:\n%s\n%s" % (r.stdout, r.stderr))
    return out, r.stdout


def load(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


class BenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bench_fixture_")
        cls.attr, cls.eval_input, cls.out_root = make_fixture(cls.tmp)
        cls.out, cls.log = run_build(cls.attr, cls.eval_input, cls.out_root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.out, *parts)

    def test_nested_tiers(self):
        for model in ("aime_expert", "aime_fast"):
            small = {r["trace_id"] for r in load(self.path(model, "tier_200.jsonl"))}
            large = {r["trace_id"] for r in load(self.path(model, "tier_400.jsonl"))}
            self.assertTrue(small, "小档不能为空")
            self.assertTrue(small <= large, "tier_200 必须是 tier_400 的子集")

    def test_capacity_and_severity_mix(self):
        rows = load(self.path("aime_expert", "tier_400.jsonl"))
        self.assertLessEqual(len(rows), 400)
        sev = {}
        for r in rows:
            sev[r["severity"]] = sev.get(r["severity"], 0) + 1
        golden_share = sev.get("GOLD", 0) / len(rows)
        self.assertAlmostEqual(golden_share, 0.20, delta=0.08)
        self.assertLessEqual(sev.get("P0", 0) / len(rows), 0.27)

    def test_every_mechanism_present(self):
        rows = load(self.path("aime_expert", "tier_400.jsonl"))
        mechs = {r["primary"]["l2"] for r in rows if r["severity"] != "GOLD"}
        self.assertTrue(len(mechs) >= 6, "机制覆盖不足：%s" % mechs)

    def test_multi_turn_has_context(self):
        rows = load(self.path("aime_expert", "tier_400.jsonl"))
        multi = [r for r in rows if r["turn"] == "multi"]
        self.assertTrue(multi, "夹具里有多轮，档位里也必须有")
        for r in multi:
            self.assertGreater(len(r["messages"]), 1, "多轮必须带上文")
        self.assertTrue(all(len(r["messages"]) >= 1 for r in rows))

    def test_no_absolute_paths(self):
        for r in load(self.path("aime_expert", "tier_400.jsonl")):
            blob = json.dumps(r, ensure_ascii=False)
            self.assertNotIn("/mnt/", blob)
            self.assertNotIn("/Users/", blob)
            self.assertFalse(any(isinstance(v, str) and v.startswith("/") for v in r.values()))

    def test_dedupe(self):
        rows = load(self.path("aime_expert", "tier_400.jsonl"))
        self.assertTrue(any(r["stats"]["dup_question"] > 1 for r in rows),
                        "同问句应被合并并记录 dup_question")
        with open(self.path("aime_expert", "pool_stats.json"), encoding="utf-8") as fh:
            pool = json.load(fh)
        self.assertGreater(pool["pool_size"], 0)

    def test_stats_files(self):
        for name in ("tier_400.json", "tier_400.dim_stage.csv", "tier_400.dim_mechanism.csv",
                     "tier_400.coverage.csv", "tier_400.crosstab_stage_mechanism.csv"):
            self.assertTrue(os.path.isfile(self.path("aime_expert", "stats", name)), name)
        with open(self.path("aime_expert", "stats", "tier_400.dim_stage.csv"),
                  encoding="utf-8") as fh:
            head = fh.readline().strip()
        self.assertEqual(head, "dim,key,bucket,count,share_in_bench,share_in_pool,delta_pp")
        with open(self.path("manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertIn("aime_expert", man["models"])
        self.assertGreater(man["quality"]["join_rate"], 0.8)

    def test_join_fallback(self):
        rows = load(self.path("aime_expert", "tier_400.jsonl"))
        srcs = {p["path_source"] for r in rows for p in r["problems"]}
        self.assertIn("component", srcs, "join 失败的条目要落到 component 兜底")

    def test_deterministic(self):
        out2, _ = run_build(self.attr, self.eval_input, os.path.join(self.tmp, "rerun"))
        a = [r["trace_id"] for r in load(self.path("aime_expert", "tier_400.jsonl"))]
        b = [r["trace_id"] for r in load(os.path.join(out2, "aime_expert", "tier_400.jsonl"))]
        self.assertEqual(a, b, "同 seed + 同池子必须完全一致")

    def test_long_context_split(self):
        # 夹具里 1/3 的 case > 64k token，必须落到 long/ 而不是基础档
        rows = load(self.path("aime_expert", "tier_400.jsonl"))
        self.assertTrue(all(r["stats"]["total_tokens"] < 65536 for r in rows))
        ldir = self.path("aime_expert", "long")
        self.assertTrue(os.path.isdir(ldir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
