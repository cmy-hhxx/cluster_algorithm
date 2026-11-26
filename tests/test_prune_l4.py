#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prune_items 在 L4 附加文件（assign/zz_l4.out.jsonl）存在时的回归测试。

背景：4.4.18 起 assign/ 不只有一个 merged.out.jsonl，zz_l4.out.jsonl 只覆盖被 L4 下钻的
条目（items 的子集）。旧版把 assign/ 下所有 *.out.jsonl 的保留行加总，再与 items 比，
在有 zz_l4 的树上必然报「不一致」并退出（2026-11-26 r533 剔除 8,496 trace 时命中）。
本测试锁定：主文件必须与 items 对得上、附加文件只要求不残留被删 seq。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRUNE = os.path.join(ROOT, "bin", "prune_items.py")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def count_lines(path):
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


class PruneWithL4Test(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="prune-l4-")
        os.makedirs(os.path.join(self.work, "assign"))
        items = [
            {"seq": 1, "case_id": "A", "trace_id": "t1", "session_id": "s1", "problem_index": 0},
            {"seq": 2, "case_id": "B", "trace_id": "t2", "session_id": "s2", "problem_index": 0},
            {"seq": 3, "case_id": "C", "trace_id": "t3", "session_id": "s3", "problem_index": 0},
        ]
        write_jsonl(os.path.join(self.work, "items.jsonl"), items)
        write_jsonl(os.path.join(self.work, "locators.jsonl"), items)
        write_jsonl(os.path.join(self.work, "assign", "merged.out.jsonl"),
                    [{"seq": it["seq"], "l1": "S9"} for it in items])
        write_jsonl(os.path.join(self.work, "assign", "zz_l4.out.jsonl"),
                    [{"seq": 2, "zz_l4": "S9.M5.2.1"}])
        self.drop = os.path.join(self.work, "drop.txt")
        with open(self.drop, "w", encoding="utf-8") as fh:
            fh.write("B\n")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_drop_keeps_files_consistent(self):
        rc = subprocess.run([sys.executable, PRUNE, "--work", self.work,
                             "--drop-ids", self.drop, "--reason", "unit-test"],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr or rc.stdout)

        self.assertEqual(count_lines(os.path.join(self.work, "items.jsonl")), 2)
        self.assertEqual(count_lines(os.path.join(self.work, "locators.jsonl")), 2)
        # 主文件仍与 items 一一对应
        self.assertEqual(count_lines(os.path.join(self.work, "assign", "merged.out.jsonl")), 2)
        # 附加文件里指向被删 seq 的那一行必须一起清掉
        self.assertEqual(count_lines(os.path.join(self.work, "assign", "zz_l4.out.jsonl")), 0)

        with open(os.path.join(self.work, "prune.jsonl"), encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["dropped_items"], 1)
        self.assertEqual(rec["left_items"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
