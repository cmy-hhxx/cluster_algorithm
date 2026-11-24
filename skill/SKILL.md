---
name: attribution-cluster
description: 对一批归因数据做三层/四层聚类（环节/机制/模式/子类），产出 cluster_tree.json 等 11 件交付物。当用户说"聚类这批归因"、"跑一下 cluster_algorithm"、"把 attribution.jsonl 聚个类"，或给出 attribution.jsonl 并要求聚类时使用。
---

# 归因聚类

工具与方法论就在本文件所在目录的上一级（下称「仓库」）。**口径以仓库内 `README.md` 与 `docs/methodology.md` 为准，不要临场发明。**

## 触发后必读
1. `README.md` 与 `docs/methodology.md`（三轴、7 机制、L3/L4 准入、计数口径、质量协议）
2. `docs/runbook.md`（服务器日常执行）或 `cluster_pkg/README.md`（独立模块调用）

## 方式 A：独立可移植模块（推荐新业务 / 外部调用）

```bash
python3 cluster_pkg/run_cluster.py -c config.yaml
```

支持 OpenAI-Responses 协议端点、基于既有聚类树目录的增量演进，以及新组件/新叶自适应创建。

## 方式 B：仓库原位流水线

在仓库根目录：

```bash
./run.sh init <attribution.jsonl 或目录> <run_dir> [eval_input 或 cases.jsonl] [channel]
./run.sh classify <run_dir> --concurrency 12 --effort none
./run.sh finish <run_dir> <out_dir> [全量会话数]
```

`verify` 必须 `errors=0` 且 `locator_mismatch=0` 才能交付。

## 交付话术（三件事，缺一不可）
1. 最大单点：会话数最高的 1–3 个叶子（优先读 `cluster_brief.md`）；
2. 最普遍机制：L2 排名及其与 L1 的交叉；
3. 诚实刻度：`edge` 比例、`verify` 校验结果、用了快速还是质量模式。

## 红线
- 单元是原子归因条目，一条只归一个叶子；多条命中按 M1→M7 取最早失效点。
- 不允许出现"其他/杂项"兜底桶；长尾按具体现象命名。
- 询问用户只问"数据/目标"层面的问题，口径问题查文档，不要问。
