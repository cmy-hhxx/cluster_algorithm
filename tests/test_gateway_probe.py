#!/usr/bin/env python3
"""网关探活重试单测（4.4.15）

2026-11-22 09:37 一次瞬时探测失败导致「没有可用网关」，整棵 aime_fast 判失败。
healthy_gateways() 改为每网关最多 N 次 + 退避；这里用假 _probe_gateway 验证：
  1) 第一次失败、第二次成功 → 网关仍可用
  2) 一直失败 → 返回空（不静默放过）
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("classify_mod", os.path.join(ROOT, "bin", "classify.py"))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def run(calls, attempts=3):
    C.GATEWAYS = ["gw-a"]
    C.GATEWAY_PROBE_ATTEMPTS = attempts
    n = {"i": 0}

    def probe(base):
        n["i"] += 1
        return n["i"] > calls

    C._probe_gateway = probe
    return C.healthy_gateways(), n["i"]


fails = []
got, tries = run(calls=1)          # 第一次失败，第二次成功
if got != ["gw-a"]:
    fails.append(f"瞬时失败应重试成功，实际 {got}")
if tries != 2:
    fails.append(f"应探测 2 次，实际 {tries}")
got, tries = run(calls=99, attempts=3)   # 一直失败
if got:
    fails.append(f"持续失败应返回空，实际 {got}")
if tries != 3:
    fails.append(f"应探测 3 次，实际 {tries}")
got, tries = run(calls=0)          # 一次就好
if got != ["gw-a"] or tries != 1:
    fails.append(f"首次成功不应重试，实际 {got}/{tries}")

if fails:
    for f in fails:
        print("FAIL", f)
    sys.exit(1)
print("GW-PROBE OK （3/3）")
