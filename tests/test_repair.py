#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重问机制（CLUSTER_PATH_REPAIR）的确定性测试：不联网，用脚本化响应替换 call()。

覆盖：
  1. 三次都给出不存在的 S10.M4.2 → 重问拿到合法叶 → 合并、标 uncertain、落缺叶候选日志
  2. 重问仍给出非法 path → 抛 InvalidOutput（不替模型猜）
  3. 重问回答 NONE → 抛 InvalidOutput，且 repair 字段记为 none
  4. 首轮就合法 → 不触发重问（调用次数 = 1）
  5. 4 段路径（S10.M2.8.1）按 normalize 收敛到合法父叶 → 不触发重问
  6. 关掉开关 CLUSTER_PATH_REPAIR=0 → 行为退回 4.4.7（不重问、直接失败）
  7. 机制位在本环节不存在（S5.M5.1）→ 重问提示词点明「本环节没有 M5」
"""
import json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))

LOG = os.path.join(tempfile.mkdtemp(), "missing_leaf.jsonl")
os.environ["CLUSTER_REPAIR_LOG"] = LOG
os.environ["CLUSTER_PATH_REPAIR"] = "1"
os.environ["CLUSTER_REQUEST_ATTEMPTS"] = "1"

import classify  # noqa: E402

classify.time.sleep = lambda s: None

RULES, INV, VALID = classify.load_rules("S10")
ITEMS = [{"seq": 1 + i, "component": "shell", "label": f"样例{i}",
          "definition": "生成请求使用了工具不支持的参数", "reason": "argparse 报 unrecognized arguments"}
         for i in range(3)]
GATEWAYS = ["http://stub"]


def resp(text):
    return {"output": [{"content": [{"type": "output_text", "text": text}]}]}


def rows(seqs, path):
    return "\n".join(json.dumps({"seq": s, "path": path, "uncertain": False}) for s in seqs)


def install(seq_texts):
    """把 call() 换成按顺序返回预设文本，并返回 {prompt: count} 记录。"""
    box = {"n": 0, "prompts": []}

    def fake(gateways, prompt):
        i = box["n"]
        box["n"] += 1
        box["prompts"].append(prompt)
        if i >= len(seq_texts):
            raise AssertionError(f"多调了一次模型（第 {i + 1} 次）")
        return resp(seq_texts[i])

    classify.call = fake
    return box


def case(name):
    print(f"- {name}")


def main():
    ok = 0

    # 1. 重问修好
    case("重问修好 3 行")
    box = install([rows([1, 2, 3], "S10.M4.2"),        # 首轮：不存在的机制位
                   rows([1, 2, 3], "S10.M2.8")])       # 重问：合法
    got = classify.classify_items(GATEWAYS, "S10", "S10_t1", ITEMS, RULES, INV, VALID, 1)
    assert len(got) == 3 and {r["path"] for r in got} == {"S10.M2.8"}, got
    assert all(r.get("uncertain") for r in got), "重问命中应标 uncertain/edge"
    assert box["n"] == 2, box["n"]
    repair_prompt = box["prompts"][1]
    for must in ("S10.M4.2", "M4 在本环节只有清单里那几个序号", "NONE", "S10.M2.8"):
        assert must in repair_prompt, f"重问提示词缺 {must!r}"
    recs = [json.loads(l) for l in open(LOG, encoding="utf-8")]
    assert len(recs) == 3 and all(r["outcome"] == "repaired" and r["rejected"] == "S10.M4.2"
                                  for r in recs), recs
    assert classify._stats["repaired"] == 3
    ok += 1

    # 2. 重问仍非法 → 失败（不猜）
    case("重问仍非法 → InvalidOutput")
    box = install([rows([1, 2, 3], "S10.M4.2"),
                   rows([1, 2, 3], "S10.M9.9")])
    try:
        classify.classify_items(GATEWAYS, "S10", "S10_t2", ITEMS, RULES, INV, VALID, 1)
        raise AssertionError("应当抛 InvalidOutput")
    except classify.InvalidOutput:
        pass
    assert box["n"] == 2
    ok += 1

    # 3. NONE → 失败，且带 nofit 信息
    case("重问回答 NONE → 缺叶候选")
    before = classify._stats["nofit"]
    box = install([rows([1, 2, 3], "S10.M4.2"),
                   "\n".join(json.dumps({"seq": s, "path": "NONE", "uncertain": True}) for s in (1, 2, 3))])
    try:
        classify.classify_items(GATEWAYS, "S10", "S10_t3", ITEMS, RULES, INV, VALID, 1)
        raise AssertionError("应当抛 InvalidOutput")
    except classify.InvalidOutput as e:
        assert all(v.get("outcome") == "none" for v in e.repair.values()), e.repair
    assert classify._stats["nofit"] == before + 3
    recs = [json.loads(l) for l in open(LOG, encoding="utf-8")]
    assert sum(1 for r in recs if r["outcome"] == "none") == 3
    ok += 1

    # 4. 首轮合法 → 不重问
    case("首轮合法 → 不重问")
    box = install([rows([1, 2, 3], "S10.M3.2")])
    got = classify.classify_items(GATEWAYS, "S10", "S10_t4", ITEMS, RULES, INV, VALID, 1)
    assert {r["path"] for r in got} == {"S10.M3.2"} and box["n"] == 1
    assert not any(r.get("uncertain") for r in got)
    ok += 1

    # 5. 4 段路径收敛
    case("4 段路径收敛到父叶")
    box = install([rows([1, 2, 3], "S10.M2.8.1")])
    got = classify.classify_items(GATEWAYS, "S10", "S10_t5", ITEMS, RULES, INV, VALID, 1)
    assert {r["path"] for r in got} == {"S10.M2.8"} and box["n"] == 1
    ok += 1

    # 6. 关掉开关时行为回到 4.4.7 之前
    case("CLUSTER_PATH_REPAIR=0 → 不重问")
    classify.PATH_REPAIR = False
    box = install([rows([1, 2, 3], "S10.M4.2")])
    try:
        classify.classify_items(GATEWAYS, "S10", "S10_t6", ITEMS, RULES, INV, VALID, 1)
        raise AssertionError("应当抛 InvalidOutput")
    except classify.InvalidOutput:
        pass
    assert box["n"] == 1
    classify.PATH_REPAIR = True
    ok += 1

    # 7. 机制位不存在
    case("机制位不存在 → 提示词点明")
    classify.PATH_REPAIR = True
    r5, i5, v5 = classify.load_rules("S5")
    box = install([rows([1, 2, 3], "S5.M5.1"),
                   rows([1, 2, 3], "S5.M4.1")])
    got = classify.classify_items(GATEWAYS, "S5", "S5_t7", ITEMS, r5, i5, v5, 1)
    assert {r["path"] for r in got} == {"S5.M4.1"}, got
    assert "本环节没有 M5" in box["prompts"][1]
    ok += 1

    print(f"REPAIR OK （{ok}/7）")


if __name__ == "__main__":
    main()
