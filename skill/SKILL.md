---
name: attribution-cluster
description: 对一批流失用户归因数据做三层聚类（环节/机制/模式），产出 cluster_tree.json 等交付物。当用户说"聚类这批归因"、"跑一下 cluster_algorithm"、"把 attribution.jsonl 聚个类"，或给出 attribution.jsonl 并要求聚类时使用。
---

# 归因聚类

工具与方法论就在本文件所在目录的上一级（下称「仓库」）。**口径以仓库内 README.md 为准，不要临场发明。**

## 触发后必读
1. `README.md`（三轴、7 机制、L3/L4 准入、计数口径、质量协议）
2. `RUNBOOK.md`（执行步骤）

## 执行
在仓库根目录：

```bash
./run.sh init <attribution.jsonl 或目录> <run_dir> [eval_session_inputs 目录]
```

然后直调网关批量归类（默认 24 并发、200 条/片）：

```bash
./run.sh classify <run_dir> --concurrency 32 --effort none   # 快：~4700 条/分钟（24–32 并发）
# 对外交付/排名：--effort low（91% vs 88%，慢 5–6 倍）
```

需要可靠的边界标记时加 `--consensus 2`（成本约 ×2，用两次分歧标 `edge`）。
脚本会自动跳过已完成分片；失败的片重试，仍失败则对半拆片重跑。

```bash
./run.sh finish <run_dir> <out_dir> <全量会话数> <源目录绝对路径>
```
`verify` 必须 `errors=0` 且 `locator_mismatch=0` 才能交付。

> 旧路子（派 DSH 子代理）仍可用，模板在 `prompts/01_assign_agent.md`；默认不再走它。

## 交付话术（三件事，缺一不可）
1. 最大单点：会话数最高的 1–3 个叶子；
2. 最普遍机制：L2 排名及其与 L1 的交叉；
3. 诚实刻度：edge 比例、定位校验结果、用了 fast 还是 quality 模式（有 α 就给 α）。

## 红线
- 单元是原子归因条目，一条只归一个叶子；多条命中按 M1→M7 取最早失效点。
- 不允许出现"其他/杂项"兜底桶；长尾按具体现象命名。
- `UNMAPPED > 5%` 时停下来提议扩充分类法，不要硬塞进现有叶子。
- 询问用户只问"数据/目标"层面的问题，口径问题查 README，不要问。
