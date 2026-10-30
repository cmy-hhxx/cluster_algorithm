# 优化实验（可复现）

这套脚本只服务 `OPTIMIZATION.md` 里的对照实验，不是生产入口。生产入口是 `bin/classify.py`。

## 评估集

- `audit/in/*.jsonl`：576 条双盲抽样条目（seq / l1 / component / label / definition）。
- `audit/a|b/*.out.jsonl`：两位标注员的独立判定。
- 金标 = 两人一致的 529 条；`lib.load_gold()` 负责组装。
- 输入文本从 `examples/lost_user_eval_2026-10_9stage/item_index.jsonl` 取完整 `label/definition/reason`，
  与生产输入一致；评估 seq 不参与任何训练/检索索引。

## 跑一轮

```bash
# 本地需先有到网关的隧道（或把 CLUSTER_GATEWAYS 指到可达地址）
python3 experiments/run_eval.py --variant v0 --effort none --temperature 0 --tag my_fast
python3 experiments/run_eval.py --variant v0 --effort low  --temperature 0 --tag my_quality
```

- `--variant v0`：直接用生产 `bin/classify.py` 的 `load_rules/build_prompt`，保证评的是线上提示词。
- `--chunk 60`：按环节分组后每片 60 条，便于在 4 并发下快速跑完。
- `--runs N`：同片跑 N 遍并多数投票；结果里同时给 `score`（投票后）和 `score_unanimous`（全一致子集）。
- `--seqs-file`：只评指定 seq（用于难例复判）。

结果写到 `experiments/results/<tag>.json`，含逐条 `pred`、`edges`、`votes`、token 用量与混淆对。
`experiments/cache/` 是原始响应缓存，按提示词哈希命名，已 gitignore。

## 其他脚本

- `adjudicate.py`：对 3 遍投票不一致的条目做二次裁决（实验显示收益为负，保留作证据）。
- `lib.py`：评估集、TF-IDF 检索、打分。

## 解读

- 叶子准确率是主指标；机制层（`S*.M*`）准确率用于判断“错在叶子边界还是错在机制”。
- 与 `audit/REPORT.md` 的 91.8% 对照：超过它通常意味着过拟合，不要拿单次高值当结论。
- 529 条的 95% 置信区间约 ±1.5pp；0.5–1pp 的差异不要当真实提升。
