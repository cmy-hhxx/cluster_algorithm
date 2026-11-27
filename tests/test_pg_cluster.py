#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PG-Native AIME 聚类流水线确定性回归测试（零外网、零真实 PG 依赖）。"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))

import cluster as C  # noqa: E402
import pg_cluster as PG  # noqa: E402


class FakePGBackend:
    """内存桩：模拟 PG 16 查询、Zstd pread、5003 /clusters/bind 与 writers publish-cluster。"""

    def __init__(self, rows=None, blobs=None, tree_info=None, bound_rows=None, latest_ts=None):
        self.rows = list(rows or [])
        self.blobs = dict(blobs or {})
        self.tree_info = tree_info
        self.bound_rows = list(bound_rows or [])
        self.latest_ts = latest_ts
        self.pread_calls = []
        self.bind_calls = []
        self.publish_calls = []
        self.l4_rows = []

    def pread_attr(self, rel_path: str, offset: int, length: int):
        key = (rel_path, int(offset), int(length))
        self.pread_calls.append(key)
        if key not in self.blobs:
            raise FileNotFoundError(f"missing blob {key}")
        return self.blobs[key]

    def current_tree_info(self, model: str):
        return self.tree_info

    def latest_clustered_at(self, model: str):
        return self.latest_ts

    def model_counts(self, model: str):
        bound = len(self.bound_rows) + sum(len(c["bindings"]) for c in self.bind_calls)
        return {"total": bound, "bound": bound, "unclustered": 0, "l4_bound": 0}

    def fetch_unclustered(self, model: str, limit: int = 5000):
        return self.rows[:limit] if limit else list(self.rows)

    def fetch_l4_candidates(self, model: str, leaves, *, force: bool = False, limit: int = 5000):
        return self.l4_rows[:limit] if limit else list(self.l4_rows)

    def sessions_all_count(self, model: str) -> int:
        return len({C.usable_sid(r.get("session_id")) or r["trace_id"] for r in self.bound_rows}) + 5

    def iter_bound_problems(self, model: str, itersize: int = 10000):
        for r in self.bound_rows:
            yield dict(r)

    def bind_clusters(self, bindings, *, tree_id=None, cluster_version=None, data_cutoff=None, batch_size=500):
        self.bind_calls.append(
            {
                "bindings": list(bindings),
                "tree_id": tree_id,
                "cluster_version": cluster_version,
                "data_cutoff": data_cutoff,
                "batch_size": batch_size,
            }
        )
        return {"updated": len(bindings), "changes_emitted": len(bindings)}

    def publish_via_writer(self, model: str, cluster_dir: str, *, release_tag=None, skip_bind=True):
        files = sorted(os.listdir(cluster_dir))
        self.publish_calls.append(
            {
                "model": model,
                "cluster_dir": cluster_dir,
                "release_tag": release_tag,
                "skip_bind": skip_bind,
                "files": files,
            }
        )
        return {
            "cmd": "publish-cluster",
            "model": model,
            "tree_id": 99,
            "release_tag": release_tag,
            "nodes": 10,
            "bound": 0 if skip_bind else len(self.bound_rows),
            "changes_emitted": 0,
        }


def _sample_pg_row(
    pid: int,
    *,
    trace_id: str = "t1",
    session_id: str = "s1",
    scope: str = "turn",
    turn_index: int = 0,
    problem_index: int = 0,
    component: str = "finquery",
    label: str = "取数超时",
    severity: str = "P1",
    cluster_code: str | None = None,
    is_edge: bool = False,
    attr_rel: str = "eval_output/aime_fast/2026/1127/attribution_part_0000.jsonl.zst",
    attr_offset: int = 0,
    attr_len: int = 128,
    req_dt: datetime.datetime | None = None,
):
    req_dt = req_dt or datetime.datetime(2026, 11, 27, 6, 0, 0, tzinfo=datetime.timezone.utc)
    return {
        "problem_id": pid,
        "output_id": 100 + pid,
        "input_id": 200 + pid,
        "model": "aime_fast",
        "biz_day": datetime.date(2026, 11, 27),
        "trace_id": trace_id,
        "scope": scope,
        "turn_index": turn_index,
        "problem_index": problem_index,
        "severity": severity,
        "component": component,
        "component_family": component,
        "label": label,
        "cluster_code": cluster_code,
        "is_edge": is_edge,
        "tags": [],
        "attr_offset": attr_offset,
        "attr_len": attr_len,
        "attr_rel": attr_rel,
        "session_id": session_id,
        "user_id": "u1",
        "batch_id": "2026-11-27",
        "case_id": trace_id,
        "request_time": req_dt,
        "question_text": "AAPL 营收是多少",
    }


class TestPGCluster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pg_cluster_test_")
        self._orig_root = PG.ROOT
        self._orig_append_ledger = PG.append_ledger
        self.ledger_rows = []
        PG.ROOT = self.tmp
        PG.append_ledger = lambda row: self.ledger_rows.append(row)
        # 确保本地测试能读到仓库的 VERSION.json 与 taxonomy/
        os.symlink(os.path.join(ROOT, "VERSION.json"), os.path.join(self.tmp, "VERSION.json"))
        os.symlink(os.path.join(ROOT, "taxonomy"), os.path.join(self.tmp, "taxonomy"))

    def tearDown(self):
        PG.ROOT = self._orig_root
        PG.append_ledger = self._orig_append_ledger
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hydrate_groups_by_frame_and_supports_session_scope(self):
        rel = "eval_output/aime_fast/2026/1127/attribution_part_0000.jsonl.zst"
        doc = {
            "turns": [
                {
                    "problem": [
                        {"component": "finquery", "label": "L0", "definition": "def0", "reason": "r0"},
                        {"component": "finquery", "label": "L1", "definition": "def1", "reason": "r1"},
                    ]
                }
            ],
            "session_problem": [
                {"component": "answer-synthesis", "label": "LS", "definition": "defS", "reason": "rS"}
            ],
        }
        backend = FakePGBackend(blobs={(rel, 100, 500): doc})
        rows = [
            _sample_pg_row(1, scope="turn", turn_index=0, problem_index=0, attr_rel=rel, attr_offset=100, attr_len=500),
            _sample_pg_row(2, scope="turn", turn_index=0, problem_index=1, attr_rel=rel, attr_offset=100, attr_len=500),
            _sample_pg_row(3, scope="session", turn_index=0, problem_index=0, attr_rel=rel, attr_offset=100, attr_len=500),
        ]
        hydrated = PG.hydrate_problems_from_zst(rows, backend.pread_attr)
        # 同一 Zstd 帧只解压 1 次
        self.assertEqual(len(backend.pread_calls), 1)
        self.assertEqual(hydrated[1]["definition"], "def0")
        self.assertEqual(hydrated[2]["reason"], "r1")
        self.assertEqual(hydrated[3]["definition"], "defS")

    def test_build_delta_quarantines_unregistered_and_fills_session_id(self):
        rows = [
            _sample_pg_row(10, component="finquery", session_id="None", trace_id="tr_10"),
            _sample_pg_row(11, component="totally_unknown_widget_xyz_999", session_id="s11", trace_id="tr_11"),
        ]
        hydrated = {
            10: {"definition": "SQL 执行超时", "reason": "timeout 30s"},
            11: {"definition": "未知工具", "reason": "boom"},
        }
        valid, unreg = PG.build_delta_items(rows, hydrated)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["seq"], 10)
        self.assertEqual(valid[0]["session_id"], "tr_10")  # "None" 兜底到 trace_id
        self.assertEqual(valid[0]["definition"], "SQL 执行超时")
        self.assertEqual(len(unreg), 1)
        self.assertEqual(unreg[0]["component_raw"], "totally_unknown_widget_xyz_999")

    def test_incremental_noop_skips_gateway_and_triggers_daily_publish(self):
        now_ts = datetime.datetime(2026, 11, 27, 5, 0, 0, tzinfo=datetime.timezone.utc)
        old_gen = datetime.datetime(2026, 11, 26, 4, 0, 0, tzinfo=datetime.timezone.utc)
        rel = "a.zst"
        bound = [
            _sample_pg_row(
                1,
                component="finquery",
                cluster_code="S3.M3.2.1",
                attr_rel=rel,
                attr_offset=0,
                attr_len=10,
            )
        ]
        blobs = {(rel, 0, 10): {"turns": [{"problem": [{"definition": "超限报错"}]}]}}
        backend = FakePGBackend(
            rows=[],
            blobs=blobs,
            bound_rows=bound,
            latest_ts=now_ts,
            tree_info={
                "tree_id": 2,
                "version": "4.4.21",
                "release_tag": "v4.4.21_20261126",
                "generated_at": old_gen,
                "node_codes": {"S3.M3.2", "S3.M3.2.1"},
            },
        )
        gw_called = []
        res = PG.run_incremental_model(
            backend,
            "aime_fast",
            daily_publish_hour=0,  # 任意小时都触发每日窗口检查
            gateway_checker=lambda: gw_called.append(1) or ["http://gw"],
        )
        self.assertEqual(res["rc"], 0)
        self.assertEqual(res["status"], "noop")
        self.assertEqual(gw_called, [])  # noop 不探活网关
        self.assertTrue(res["published"])
        self.assertEqual(len(backend.publish_calls), 1)
        self.assertTrue(backend.publish_calls[0]["skip_bind"])
        self.assertNotIn("item_index.jsonl", backend.publish_calls[0]["files"])

    def test_incremental_l3_l4_bind_quarantine_and_new_leaf_publish(self):
        rel = "a.zst"
        doc = {
            "turns": [
                {
                    "problem": [
                        {"component": "finquery", "label": "超限", "definition": "行超限", "reason": "oom"},
                        {"component": "finquery", "label": "未覆盖", "definition": "缺字段", "reason": "missing"},
                        {"component": "unknown_brand_new_tool_888", "label": "未知", "definition": "d", "reason": "r"},
                    ]
                }
            ]
        }
        t_later = datetime.datetime(2026, 11, 27, 8, 30, 0, tzinfo=datetime.timezone.utc)
        rows = [
            _sample_pg_row(101, trace_id="t101", session_id="s101", problem_index=0, component="finquery", attr_rel=rel, attr_offset=0, attr_len=50, req_dt=t_later),
            _sample_pg_row(102, trace_id="t102", session_id="s102", problem_index=1, component="finquery", attr_rel=rel, attr_offset=0, attr_len=50),
            _sample_pg_row(103, trace_id="t103", session_id="s103", problem_index=2, component="unknown_brand_new_tool_888", attr_rel=rel, attr_offset=0, attr_len=50),
        ]
        # 已有树不含 S3.M3.2.1，用来验证首次命中新叶自动触发发版
        bound_after = [
            _sample_pg_row(101, trace_id="t101", session_id="s101", problem_index=0, component="finquery", cluster_code="S3.M3.2.1", attr_rel=rel, attr_offset=0, attr_len=50, req_dt=t_later),
            _sample_pg_row(102, trace_id="t102", session_id="s102", problem_index=1, component="finquery", cluster_code="S3.M4.1", attr_rel=rel, attr_offset=0, attr_len=50),
        ]
        backend = FakePGBackend(
            rows=rows,
            blobs={(rel, 0, 50): doc},
            bound_rows=bound_after,
            tree_info={
                "tree_id": 2,
                "version": PG.pipeline_version(),
                "release_tag": f"v{PG.pipeline_version()}_{PG.now_local().strftime('%Y%m%d')}",
                "generated_at": datetime.datetime.now(datetime.timezone.utc),
                "node_codes": {"S3.M4.1"},
            },
        )

        def fake_classify(work_dir, concurrency, repair_log):
            out = os.path.join(work_dir, "assign", "S3_c001.out.jsonl")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": 101, "path": "S3.M3.2", "edge": False}) + "\n")
                fh.write(json.dumps({"seq": 102, "path": "S3.M4.1", "edge": True}) + "\n")
            return 0

        def fake_l4(work_dir, leaves_csv, concurrency, *, force=False):
            out = os.path.join(work_dir, "assign", "zz_l4.out.jsonl")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": 101, "path": "S3.M3.2.1", "edge": False}) + "\n")
            return 0

        res = PG.run_incremental_model(
            backend,
            "aime_fast",
            l4_leaves=["S3.M3.2"],
            classify_runner=fake_classify,
            l4_runner=fake_l4,
            gateway_checker=lambda: ["http://gw"],
        )
        # 存在未登记组件 -> rc=1 告警，但合法的 101/102 照常 L3+L4 并回填 PG！
        self.assertEqual(res["rc"], 1)
        self.assertEqual(res["status"], "partial_failed")
        self.assertEqual(res["unregistered"], 1)
        self.assertEqual(res["bound"], 2)
        self.assertEqual(res["l4_upgraded"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "logs", "unregistered_aime_fast.json")))

        # 验证回填内容（101 升级为 L4，102 保留 L3 + edge=True，附带 tree_id + data_cutoff）
        self.assertEqual(len(backend.bind_calls), 1)
        bc = backend.bind_calls[0]
        self.assertEqual(bc["tree_id"], 2)
        self.assertEqual(bc["data_cutoff"], "2026-11-27T08:30:00+00:00")
        by_pid = {b["problem_id"]: b for b in bc["bindings"]}
        self.assertEqual(by_pid[101]["cluster_code"], "S3.M3.2.1")
        self.assertFalse(by_pid[101]["is_edge"])
        self.assertEqual(by_pid[102]["cluster_code"], "S3.M4.1")
        self.assertTrue(by_pid[102]["is_edge"])
        # 命中了 node_codes 之外的 S3.M3.2 / S3.M3.2.1 -> 自动触发 publish
        self.assertTrue(res["published"])
        self.assertEqual(len(backend.publish_calls), 1)

    def test_build_tree_from_pg_verifies_session_union_and_rebind_mode(self):
        rel = "a.zst"
        doc = {
            "turns": [
                {
                    "problem": [
                        {"definition": "def_l4_1"},
                        {"definition": "def_l4_2"},
                    ]
                }
            ],
            "session_problem": [{"definition": "def_sess"}],
        }
        # 同一会话 s1 命中同一父叶 S3.M3.2 的两个 L4 子类 -> 父叶 sessions 必须去重为 1 而非 2！
        bound = [
            _sample_pg_row(1, trace_id="t1", session_id="s1", scope="turn", problem_index=0, component="finquery", cluster_code="S3.M3.2.1", attr_rel=rel, attr_offset=0, attr_len=20),
            _sample_pg_row(2, trace_id="t1", session_id="s1", scope="turn", problem_index=1, component="finquery", cluster_code="S3.M3.2.2", attr_rel=rel, attr_offset=0, attr_len=20),
            _sample_pg_row(3, trace_id="t2", session_id="s2", scope="session", problem_index=0, component="finquery", cluster_code="S3.M4.1", is_edge=True, attr_rel=rel, attr_offset=0, attr_len=20),
        ]
        backend = FakePGBackend(blobs={(rel, 0, 20): doc}, bound_rows=bound)
        res = PG.publish_model(backend, "aime_fast", release_tag="v4.7.0_20261127", rebind_all=True)
        self.assertEqual(res["agg"]["total_items"], 3)
        self.assertEqual(res["agg"]["total_sessions"], 2)
        self.assertEqual(res["agg"]["verify"], "err=0,mismatch=0")
        pub = backend.publish_calls[0]
        self.assertFalse(pub["skip_bind"])
        self.assertIn("item_index.jsonl", pub["files"])
        for fn in (
            "cluster_tree.json",
            "cluster_tree.txt",
            "cluster_brief.md",
            "cluster_summary.md",
            "cluster_matrix.png",
            "leaves.tsv",
            "syndrome.json",
            "syndrome.md",
            "verify.json",
            "MANIFEST.json",
        ):
            self.assertIn(fn, pub["files"], f"missing deliverable {fn}")

        tree = json.load(open(os.path.join(pub["cluster_dir"], "cluster_tree.json"), encoding="utf-8"))
        l3_m32 = next(
            l3
            for l1 in tree["tree"]["children"]
            for l2 in l1["children"]
            for l3 in l2["children"]
            if l3["id"] == "S3.M3.2"
        )
        self.assertEqual(l3_m32["count"], 2)
        self.assertEqual(l3_m32["sessions"], 1)  # 并集去重！
        self.assertEqual(l3_m32["samples"][0]["definition"], "def_l4_1")

        # 确认 item_index.jsonl 显式携带 scope 字段
        idx_rows = [json.loads(l) for l in open(os.path.join(pub["cluster_dir"], "item_index.jsonl"), encoding="utf-8")]
        scopes = {r["seq"]: r["scope"] for r in idx_rows}
        self.assertEqual(scopes[3], "session")

    def test_backfill_l4_binds_upgraded_codes(self):
        rel = "b.zst"
        doc = {"turns": [{"problem": [{"definition": "明细", "reason": "原因"}]}]}
        backend = FakePGBackend(blobs={(rel, 0, 10): doc}, tree_info={"tree_id": 5, "node_codes": set()})
        backend.l4_rows = [
            _sample_pg_row(501, component="finquery", cluster_code="S3.M3.2", is_edge=True, attr_rel=rel, attr_offset=0, attr_len=10)
        ]

        def fake_l4(work_dir, leaves_csv, concurrency, *, force=False):
            with open(os.path.join(work_dir, "assign", "zz_l4.out.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": 501, "path": "S3.M3.2.1", "edge": False}) + "\n")
            return 0

        res = PG.run_backfill_l4_model(
            backend,
            "aime_fast",
            ["S3.M3.2"],
            l4_runner=fake_l4,
            gateway_checker=lambda: ["http://gw"],
        )
        self.assertEqual(res["rc"], 0)
        self.assertEqual(res["bound"], 1)
        self.assertEqual(backend.bind_calls[0]["bindings"][0]["cluster_code"], "S3.M3.2.1")
        # 保留原 L3 的 edge=True 标记
        self.assertTrue(backend.bind_calls[0]["bindings"][0]["is_edge"])


if __name__ == "__main__":
    unittest.main()
