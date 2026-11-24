#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归类与网关探活确定性单测（不联网，脚本化桩响应）。"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_classify(log_path):
    os.environ["CLUSTER_REPAIR_LOG"] = log_path
    os.environ["CLUSTER_PATH_REPAIR"] = "1"
    os.environ["CLUSTER_REQUEST_ATTEMPTS"] = "1"
    spec = importlib.util.spec_from_file_location(
        "classify_under_test", os.path.join(ROOT, "bin", "classify.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time.sleep = lambda _s: None
    return mod


def _resp(text):
    return {"output": [{"content": [{"type": "output_text", "text": text}]}]}


def _rows(seqs, path, uncertain=False):
    return "\n".join(json.dumps({"seq": s, "path": path, "uncertain": uncertain}) for s in seqs)


class ClassifyRepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_classify_")
        self.log_path = os.path.join(self.tmp, "missing_leaf.jsonl")
        self.C = _load_classify(self.log_path)
        self.rules, self.inv, self.valid = self.C.load_rules("S10")
        self.items = [
            {"seq": 1 + i, "component": "shell", "label": f"样例{i}",
             "definition": "生成请求使用了工具不支持的参数", "reason": "argparse 报 unrecognized arguments"}
            for i in range(3)
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self, seq_texts):
        box = {"n": 0, "prompts": []}

        def fake(_gateways, prompt):
            i = box["n"]
            box["n"] += 1
            box["prompts"].append(prompt)
            if i >= len(seq_texts):
                raise AssertionError(f"多调了一次模型（第 {i + 1} 次）")
            return _resp(seq_texts[i])

        self.C.call = fake
        return box

    def test_repair_recovers_bad_paths(self):
        box = self._install([_rows([1, 2, 3], "S10.M4.2"), _rows([1, 2, 3], "S10.M2.8")])
        got = self.C.classify_items(["http://stub"], "S10", "S10_t1", self.items,
                                    self.rules, self.inv, self.valid, 1)
        self.assertEqual({r["path"] for r in got}, {"S10.M2.8"})
        self.assertTrue(all(r.get("uncertain") for r in got))
        self.assertEqual(box["n"], 2)
        for must in ("S10.M4.2", "M4 在本环节只有清单里那几个序号", "NONE", "S10.M2.8"):
            self.assertIn(must, box["prompts"][1])
        with open(self.log_path, encoding="utf-8") as fh:
            recs = [json.loads(l) for l in fh]
        self.assertEqual(len(recs), 3)
        self.assertTrue(all(r["outcome"] == "repaired" and r["rejected"] == "S10.M4.2" for r in recs))

    def test_repair_still_invalid_raises(self):
        box = self._install([_rows([1, 2, 3], "S10.M4.2"), _rows([1, 2, 3], "S10.M9.9")])
        with self.assertRaises(self.C.InvalidOutput):
            self.C.classify_items(["http://stub"], "S10", "S10_t2", self.items,
                                  self.rules, self.inv, self.valid, 1)
        self.assertEqual(box["n"], 2)

    def test_repair_none_flags_missing_leaf(self):
        box = self._install([_rows([1, 2, 3], "S10.M4.2"), _rows([1, 2, 3], "NONE", uncertain=True)])
        with self.assertRaises(self.C.InvalidOutput) as ctx:
            self.C.classify_items(["http://stub"], "S10", "S10_t3", self.items,
                                  self.rules, self.inv, self.valid, 1)
        self.assertTrue(all(v.get("outcome") == "none" for v in ctx.exception.repair.values()))
        self.assertEqual(box["n"], 2)

    def test_valid_first_pass_skips_repair(self):
        box = self._install([_rows([1, 2, 3], "S10.M3.2")])
        got = self.C.classify_items(["http://stub"], "S10", "S10_t4", self.items,
                                    self.rules, self.inv, self.valid, 1)
        self.assertEqual({r["path"] for r in got}, {"S10.M3.2"})
        self.assertEqual(box["n"], 1)
        self.assertFalse(any(r.get("uncertain") for r in got))

    def test_normalize_4_segment_path(self):
        box = self._install([_rows([1, 2, 3], "S10.M2.8.1")])
        got = self.C.classify_items(["http://stub"], "S10", "S10_t5", self.items,
                                    self.rules, self.inv, self.valid, 1)
        self.assertEqual({r["path"] for r in got}, {"S10.M2.8"})
        self.assertEqual(box["n"], 1)

    def test_disable_repair_switch(self):
        self.C.PATH_REPAIR = False
        box = self._install([_rows([1, 2, 3], "S10.M4.2")])
        with self.assertRaises(self.C.InvalidOutput):
            self.C.classify_items(["http://stub"], "S10", "S10_t6", self.items,
                                  self.rules, self.inv, self.valid, 1)
        self.assertEqual(box["n"], 1)

    def test_missing_mechanism_noted_in_repair_prompt(self):
        r5, i5, v5 = self.C.load_rules("S5")
        box = self._install([_rows([1, 2, 3], "S5.M5.1"), _rows([1, 2, 3], "S5.M4.1")])
        got = self.C.classify_items(["http://stub"], "S5", "S5_t7", self.items, r5, i5, v5, 1)
        self.assertEqual({r["path"] for r in got}, {"S5.M4.1"})
        self.assertIn("本环节没有 M5", box["prompts"][1])


class GatewayProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_gw_")
        self.C = _load_classify(os.path.join(self.tmp, "rep.jsonl"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe_run(self, fail_times, attempts=3):
        self.C.GATEWAYS = ["gw-a"]
        self.C.GATEWAY_PROBE_ATTEMPTS = attempts
        cnt = {"i": 0}

        def fake_probe(_base):
            cnt["i"] += 1
            return cnt["i"] > fail_times

        self.C._probe_gateway = fake_probe
        return self.C.healthy_gateways(), cnt["i"]

    def test_transient_failure_recovers(self):
        got, tries = self._probe_run(fail_times=1)
        self.assertEqual(got, ["gw-a"])
        self.assertEqual(tries, 2)

    def test_persistent_failure_returns_empty(self):
        got, tries = self._probe_run(fail_times=99, attempts=3)
        self.assertEqual(got, [])
        self.assertEqual(tries, 3)

    def test_immediate_success_no_retry(self):
        got, tries = self._probe_run(fail_times=0)
        self.assertEqual(got, ["gw-a"])
        self.assertEqual(tries, 1)


if __name__ == "__main__":
    unittest.main()
