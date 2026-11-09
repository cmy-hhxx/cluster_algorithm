# RUNBOOK：收到「帮我聚类」指令后我做什么

用户只需给：**哪批数据（问财 / AIME）**。数据落在哪台机器上、根目录是什么，见 [DEPLOY.md](DEPLOY.md) §0 的差异表。
两台机器的连接信息（host / SSH 端口 / 容器名 / 登录方式与密钥）都在仓库根的 `.servers.conf`（本机文件，已 gitignore，不入库、不下发）。

**第一件事是认渠道，位置固定**：问财的聚类在乌兰（`eval_data`）跑，AIME 的聚类在弗吉尼亚（`aime_rsi_data`）跑，不要在同一台机器上跑两个渠道。跨渠道的数据不合并、不对比，
`bad_case_id` 的哈希里带渠道，两台机器各自只写自己的数据根和索引。

## 0. 参数

| 变量 | 取值 |
|---|---|
| 机器 / 渠道 | 问财 → 乌兰；AIME → 弗吉尼亚（`CLUSTER_CHANNEL` 由 `.deploy/env.sh` 定） |
| `SRC` | 用户给的 jsonl / 目录（批量跑时由 `bin/discover_trees.py` 自动发现，不用手填） |
| `CASES` | 可选，`eval_session_inputs` 目录，用于补 `case_id` 与问题原文 |
| `RUN` | 运行目录，如 `<输出目录>/run` |
| `OUT` | 交付目录，如 `<输出目录>` |
| 并发 | 乌兰 12（8 核 / 16 GiB）、弗吉尼亚 8（6 核 / 64 GiB）；**不要超过 32** |
| 分片 | 200 条/片，按环节分组，跨环节不混片 |
| 精度 | `CLUSTER_REASONING=none`（默认，快）/ `low`（准，慢 5–6 倍） |

## 1. 解析与分片（确定性，秒级）

```bash
python3 bin/taxonomy_lint.py          # 分类法自检，必须 errors=0
python3 bin/cluster.py ingest --input $SRC --cases $CASES --out $RUN
python3 bin/cluster.py chunk  --work $RUN --size 200
```

看两个数：`bad_lines`（应为 0，非 0 说明源数据有坏行，先报告用户）、`components`（出现未映射的 component 要停下来确认，不要硬塞进现有环节）。

## 2. 归类（直调网关，默认 24 并发）

```bash
CLUSTER_REASONING=none ./run.sh classify $RUN --concurrency 24
# 对外交付/排名：CLUSTER_REASONING=low ./run.sh classify $RUN --concurrency 24
```

- 每片自检：行数、seq 集合、path 合法；不合格自动重试，仍失败则对半拆片重跑。
- 产出 `$RUN/assign/<chunk>.out.jsonl`，每行 `{seq, path, edge}`。
- 需要可靠的边界标记时加 `--consensus 2`（成本约 ×2，用两次分歧标 `edge`）。

> 网关没开通的机器（当前是弗吉尼亚）跑到这一步会失败，但 `bin/run_trees.sh` 在开跑前就会查网关并直接退 3，不会浪费几分钟。
> 本地联调跑不了这一步（本机连不到内网网关），替代做法是派 DSH 子代理逐片判：每片给 `prompts/01_assign.md` + `taxonomy/INV_S<x>.md`，8–12 并发，产出同样是 `assign/<chunk>.out.jsonl`。口径不变，只是慢。见 [DEPLOY.md](DEPLOY.md) §3。

## 3. 聚合与校验（确定性，秒级）

```bash
python3 bin/cluster.py aggregate --work $RUN --out $OUT --sessions-all <全量会话数>
python3 bin/cluster.py verify    --work $RUN --out $OUT --src <源目录绝对路径>
```

`verify` 必须 `errors=0` 且 `locator_mismatch=0` 才能交付。

## 4. 报告

```bash
python3 bin/cluster.py report   --out $OUT   # 生成 cluster_summary.md + cluster_tree.txt + cluster_brief.md
python3 bin/cluster.py chart    --out $OUT
python3 bin/cluster.py syndrome --out $OUT
```

## 5. 可选：L4 下钻（只在叶子 ≥300 时）

1. 从 `leaves.tsv` 挑 `items ≥ 300` 的叶子；
2. 每个叶子先派 1 个归纳代理（`prompts/02_l4_induct.md`），确认能写出互斥 include/exclude；
3. 再按 200 条分片派归类代理（`prompts/03_l4_assign.md`），path 写成 `S3.M2.1.1`；
4. L4 节点元数据写入 `taxonomy/l4_nodes.json`，再重跑 `aggregate`（会自动挂到第 4 层）。

## 6. 可选：质量抽检

- 抽样：分层 600 条（每叶 ≥3），两份独立标注 → `prompts/04_audit.md`；
- 算 Krippendorff α（`bin/krippendorff.py`）；
- α < 0.6 的叶子：改 `definition/include/exclude` 后重标，不接受“凑合”。

## 7. 交付清单（缺一不可）

```
cluster_tree.json     主树（含 samples 定位）
cluster_tree.txt      人读版
cluster_summary.md    分布 + 标签 + Top 模式
cluster_brief.md      一页纸结论（无内部代码，给业务看）
cluster_matrix.png    环节×机制矩阵
item_index.jsonl      全量索引（seq → path + 定位）
syndrome.md/json      会话级组合
leaves.tsv            叶子明细表
MANIFEST.json         版本、规模、文件校验和
```

## 8. 交付话术

同时给出三件事，不要只丢文件：
1. **最大单点**（会话数最高的 1–3 个类别），直接读 `cluster_brief.md`；
2. **最普遍的机制**（机制排名）与它和环节的交叉；
3. **诚实刻度**（edge 比例、定位校验结果、用了哪种精度模式）。

## 9. 耗时参考（2026-10-30 实测）

| 模式 | 实测 | 35,000 条（外推） | 叶子准确率 |
|---|---|---|---|
| 快速（none，32 并发） | 6,243 条 / 79s | **~8 分钟** | 88.1% |
| 快速（none，12 并发） | 11,843 条 / 475s | ~24 分钟 | 88.1% |
| 质量（low，12–16 并发） | 11,843 条 / ~40 分钟 | ~2 小时 | 91.3% |

长尾分片会拖慢整批：`classify.py` 已对失败片对半重跑；若某片长期超时，调低 `CLUSTER_TIMEOUT` 让它尽快失败并拆分。
