#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原位流水线确定性回归测试：覆盖分类法自检、ingest 兜底、增量 delta/merge、聚合/L4/校验/召回、prune。"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _run(cmd):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"命令失败 ({r.returncode}): {cmd}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
    return r.stdout


class TaxonomyIntegrityTest(unittest.TestCase):
    def test_taxonomy_lint_and_matrix(self):
        out = _run([sys.executable, "bin/taxonomy_lint.py"])
        self.assertIn("errors=0", out)
        _run([sys.executable, "bin/export_matrix.py", "--check"])


class PipelineEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_pipe_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_and_incremental_pipeline_with_l4_and_recall(self):
        cases_p = os.path.join(self.tmp, "cases.jsonl")
        with open(cases_p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "trace_id": "t0", "session_id": "s0",
                "question": "茅台近三年营收是多少", "request_time": "2026-10-01 08:00:00",
                "batch_id": "2026-10-01", "model": "aime_fast",
            }, ensure_ascii=False) + "\n")

        run1 = os.path.join(self.tmp, "run1")
        out1 = os.path.join(self.tmp, "out1")
        _run(["bash", "run.sh", "init", os.path.join(HERE, "fixture", "attribution.jsonl"), run1, cases_p, "aime"])

        path_map = {"finquery": "S3.M3.2.4", "planner-search": "S2.M2.4", "答案合成": "S9.M6.4"}
        for f in glob.glob(os.path.join(run1, "assign", "*.jsonl")):
            if f.endswith("manifest.json"):
                continue
            with open(f, encoding="utf-8") as src, open(f.replace(".jsonl", ".out.jsonl"), "w", encoding="utf-8") as dst:
                for line in src:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    dst.write(json.dumps({"seq": r["seq"], "path": path_map[r["component"]], "edge": False}) + "\n")

        _run(["bash", "run.sh", "finish", run1, out1, "3"])
        with open(os.path.join(out1, "verify.json"), encoding="utf-8") as fh:
            ver = json.load(fh)
        self.assertEqual(ver["errors"], 0)
        self.assertEqual(ver["locator_mismatch"], 0)
        self.assertEqual(ver["total"], 3)

        # 验证 item_index.jsonl 保留完整字段（含 component / label / date）且 recall 正常工作
        with open(os.path.join(out1, "item_index.jsonl"), encoding="utf-8") as fh:
            idx = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(idx), 3)
        self.assertEqual(idx[0]["question"], "茅台近三年营收是多少")
        self.assertTrue(all(r.get("component") and r.get("label") for r in idx))
        rec_out = _run([sys.executable, "bin/cluster.py", "recall", "--out", out1, "--node", "S3.M3.2", "--full"])
        self.assertIn("matched=1", rec_out)
        self.assertIn("茅台近三年营收是多少", rec_out)

        # 验证增量 delta + merge + S10 机制汇总 + reuse_map
        attr2 = os.path.join(self.tmp, "attr2.jsonl")
        shutil.copy2(os.path.join(HERE, "fixture", "attribution.jsonl"), attr2)
        with open(attr2, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "session_meta": {"session_id": "s_s10", "request_time": "2026-10-02 09:00:00"},
                "turns": [{"meta": {"trace_id": "t_s10"},
                           "problem": [
                               {"problem_id": "p1", "component": "finquery 请求生成",
                                "label": "参数拼接", "definition": "多对象拼接", "reason": "契约不符"},
                               {"problem_id": "p2", "component": "understand-image-25",
                                "label": "识图读错", "definition": "柱状图数值看反", "reason": "证据明确"},
                           ]}],
            }, ensure_ascii=False) + "\n")

        run_new = os.path.join(self.tmp, "run_new")
        delta = os.path.join(self.tmp, "run_delta")
        merged = os.path.join(self.tmp, "run_merged")
        out2 = os.path.join(self.tmp, "out2")
        _run([sys.executable, "bin/cluster.py", "ingest", "--input", attr2, "--out", run_new, "--channel", "aime"])
        _run([sys.executable, "bin/make_delta.py", "--old", run1, "--new", run_new, "--out", delta])
        with open(os.path.join(delta, "stats.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["delta"], 2)

        _run([sys.executable, "bin/cluster.py", "chunk", "--work", delta, "--size", "60"])
        delta_map = {"finquery": "S10.M2.8", "understand_image": "S8.M5.2"}
        for f in glob.glob(os.path.join(delta, "assign", "*.jsonl")):
            if f.endswith("manifest.json"):
                continue
            with open(f, encoding="utf-8") as src, open(f.replace(".jsonl", ".out.jsonl"), "w", encoding="utf-8") as dst:
                for line in src:
                    if line.strip():
                        r = json.loads(line)
                        dst.write(json.dumps({"seq": r["seq"], "path": delta_map[r["component"]], "edge": False}) + "\n")

        _run([sys.executable, "bin/merge_runs.py", "--out", merged, run1, delta])
        _run(["bash", "run.sh", "finish", merged, out2, "4"])
        with open(os.path.join(out2, "cluster_tree.json"), encoding="utf-8") as fh:
            tree2 = json.load(fh)
        m2_roll = next(x for x in tree2["mechanism_rollup"] if x["mechanism"] == "M2")
        self.assertIn("S10", m2_roll["by_l1"], "mechanism_rollup.by_l1 必须包含 S10–S12")

        reuse_p = os.path.join(self.tmp, "reuse.jsonl")
        reuse_out = _run([sys.executable, "bin/build_reuse_map.py", "--old", out2, "--work", run_new, "--out", reuse_p])
        self.assertIn("reuse: 5/5", reuse_out)


class SessionFillAndPruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_sess_prune_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_id_none_fallback(self):
        cases = [
            ("None", "t_none", "t_none", 1),
            ("", "t_empty", "t_empty", 1),
            (None, "t_null", "t_null", 1),
            ("s_real", "t_real", "s_real", 0),
        ]
        src = os.path.join(self.tmp, "attr.jsonl")
        with open(src, "w", encoding="utf-8") as fh:
            for sid, tid, _, _ in cases:
                rec = {
                    "session_meta": {"request_time": "2026-11-11 05:21:36"},
                    "turns": [{"meta": {"trace_id": tid},
                               "problem": [{"problem_id": "p1", "component": "finquery",
                                            "label": "报错", "definition": "SQL 报错", "reason": "证据"}]}],
                }
                if sid is not None:
                    rec["session_meta"]["session_id"] = sid
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out = os.path.join(self.tmp, "run")
        _run([sys.executable, "bin/cluster.py", "ingest", "--input", src, "--out", out, "--channel", "aime"])
        with open(os.path.join(out, "stats.json"), encoding="utf-8") as fh:
            stats = json.load(fh)
        self.assertEqual(stats["session_filled"], 3)
        with open(os.path.join(out, "items.jsonl"), encoding="utf-8") as fh:
            by_tid = {r["trace_id"]: r["session_id"] for r in (json.loads(l) for l in fh)}
        for _, tid, want_sid, _ in cases:
            self.assertEqual(by_tid[tid], want_sid)

    def test_prune_items_keeps_three_files_in_sync(self):
        work = os.path.join(self.tmp, "run_prune")
        os.makedirs(os.path.join(work, "assign"))
        rows = [
            {"seq": 1, "case_id": "c_ok", "trace_id": "t_ok", "session_id": "s_ok"},
            {"seq": 2, "case_id": "c_bad", "trace_id": "t_bad", "session_id": "t_bad"},
            {"seq": 3, "case_id": "c_drop", "trace_id": "t_drop", "session_id": "s_drop"},
            {"seq": 4, "case_id": "c_ok2", "trace_id": "t_ok2", "session_id": "s_ok2"},
        ]
        for fn in ("items.jsonl", "locators.jsonl"):
            with open(os.path.join(work, fn), "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
        with open(os.path.join(work, "assign", "merged.out.jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({"seq": r["seq"], "path": "S1.M1.1", "edge": False}) + "\n")
        drop_p = os.path.join(self.tmp, "drop.txt")
        with open(drop_p, "w", encoding="utf-8") as fh:
            fh.write("c_drop\n")

        _run([sys.executable, "bin/prune_items.py", "--work", work,
              "--drop-ids", drop_p, "--self-session", "--reason", "unit_test"])
        for fn in ("items.jsonl", "locators.jsonl", os.path.join("assign", "merged.out.jsonl")):
            with open(os.path.join(work, fn), encoding="utf-8") as fh:
                self.assertEqual([json.loads(l)["seq"] for l in fh], [1, 4])
        with open(os.path.join(work, "prune.jsonl"), encoding="utf-8") as fh:
            rec = json.loads(fh.read().strip())
        self.assertEqual(rec["dropped_items"], 2)
        self.assertEqual(rec["left_items"], 2)


if __name__ == "__main__":
    unittest.main()
