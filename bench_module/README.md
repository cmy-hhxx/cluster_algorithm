# bench_module — AIME 动态 bench 取样器

从聚类产物里筛出一份**可复现、可解释、分布可控**的评测问句集，给模型训练/发版前跑。
**独立模块**：不 import 聚类代码、不改聚类流程、不写聚类目录，只读它的产物。
**0 依赖**：纯 Python 3 标准库（远程机器只有 python3.12 + orjson，没有 pandas/numpy）。
`orjson` 存在时会自动用来加速定位表扫描，不存在就走标准库，**不装也能跑**。

---

## 1. 它到底在做什么

四步，每步都能单独复算：

1. **装配 case 表**：扫 `eval_output/**/*attribution*.jsonl`，一行一个 `trace_id`（= 一个 case）。
2. **补错误模式标签**：join 聚类产物 `cluster/<model>/item_index.jsonl`，把每条 problem 对齐到
   分类法叶 `S9.M5.1`（环节.机制.叶）。join 不上就按 `component → 段` 兜到 L1，并标
   `path_source="component"`。
3. **补问句 + 去重**：归因里约 5% 的 case 没带 `question`（其中 P0/P1 上千条），先去
   `eval_input` 原文里补回来（`meta.question → translation.question → 最后一条 user 消息`）；
   再按「规范化问句 + 单/多轮」合并重复提问（同一句话被不同用户问 100 遍只算 1 个 case，
   `dup_question` 记录合并了几条）。
4. **分层抽样**：按 severity / L2 机制 / 单多轮 分层，抽 500 / 1000 / 5000 / 10000 档，
   从 `eval_input` 取回原问句 + **多轮完整上下文**，写成自包含 JSONL。

### 为什么 case 身份是 trace 而不是会话

一次提问 = 一个 `trace_id`。多轮会话里第 3 轮那条 trace 的 `messages` 已经包含前两轮完整历史
（实测 `len(messages) == meta.n_messages`），所以**带上下文 = 直接复制 messages**，不需要还原整段会话。
同一 `session_id` 的多条 trace 会因为问句不同、轮次不同而各自成为独立 case。

---

## 2. 口径（和用户 2026-11-18 确认过）

| 项 | 口径 |
|---|---|
| 池子 | 归因覆盖到的全部 case（`eval_input` 有 156k trace，但只有归因过的那部分是池子） |
| badcase | 有任意 problem 的 case，按**最严重**那条 problem 定 P0–P4 |
| goldencase | `problem` 为空的 case（`severity = "GOLD"`） |
| 去重键 | `(model, 规范化问句, 单/多轮)`；保留最早的一条 |
| 长上下文 | `total_tokens ≥ 65536` 的 case 不进基础档，落 `long/tier_N_long.jsonl` |
| 档位 | 每次重建都重抽（不 pin、不累积）；同 seed + 同池子 → 结果完全一致 |
| 嵌套 | `tier_500 ⊂ tier_1000 ⊂ tier_5000 ⊂ tier_10000`（同 seed 下逐级加题） |
| 双池 | `aime_expert` / `aime_fast` 分开建，互不掺和 |

### 配额

每档 = golden 20% + badcase 80%；badcase 内 P0 封顶 25%，其余按 **P1:P2:P3:P4 = 4:3:2:1** 分。
在 severity 内部再按 **L2 机制均分（每机制至少 1 条）**，机制内部按 **单轮:多轮 = 75:25**。

```
tier 500 : golden 100 | P0 125 | P1 137 | P2 103 | P3 68 | P4 34   （可用量不足时自动降档）
tier 10000: golden 2000 | P0 1769(封顶于池子) | P1 ~2492 | P2 ~1869 | P3 ~1246 | P4 ~623
```

某池凑不满某档时：**能出几档出几档**，`manifest.json` 里写 `requested` / `actual` / `shortfall`，
绝不掺水、绝不重复。`aime_expert` 的 P0/P1 只有 3k 量级，很可能出不了 10000 档。

---

## 3. 产物

```
/mnt/aime_eval_ssd/3_dynamic_bench/
├── manifest.json                   # 构建时间 / 参数 / 源指纹 / 每档 requested vs actual / 质量指标
├── aime_expert/
│   ├── pool_stats.json             # 池子全量分布（bench 是否无偏的基准线）
│   ├── tier_500.jsonl              # 一行一个 case，全内联、自包含
│   ├── tier_1000.jsonl
│   ├── long/tier_10_long.jsonl     # 长上下文单独一档
│   └── stats/
│       ├── tier_500.json                                  # 计数 + 配额完成度 + token 分位
│       ├── tier_500.dim_*.csv                             # 12 个维度长表
│       ├── tier_500.coverage.csv                          # 每维偏差是否在容差内
│       └── tier_500.crosstab_stage_mechanism.csv          # 环节 × 机制 交叉表
├── aime_fast/  （同结构）
└── _build/
    ├── trace_loc.tsv / .state.json                        # trace_id → 文件偏移，增量刷新
    └── build_<时间戳>.log
```

### 3.1 bench JSONL 每行的字段（可视化同事看这里）

| 字段 | 说明 |
|---|---|
| `trace_id` / `session_id` | 身份。**没有任何绝对路径**，拿 trace_id 回 `eval_input` 可还原 |
| `model` | `aime_expert` / `aime_fast` |
| `question` | 本轮问句（单轮就是全部；多轮是最后一轮） |
| `messages` | **完整上下文**（多轮含前序轮次、工具调用与返回） |
| `message_pool` | 会话级 system/背景消息池 |
| `turn` | `single` / `multi`（判定：`session_round > 1`） |
| `session_round` / `turn_index` | 轮次信息 |
| `severity` | `P0`–`P4` / `GOLD`（= 无 problem） |
| `primary` | 主标签 `{l1, l2, l3}`（`S9` / `M5` / `S9.M5.1`） |
| `problems` | 全部 problem，每条含 `severity / label / path / path_source / component` |
| `stats` | `total_tokens / input_tokens / difficulty / tool_count / latency / dup_question` |
| `meta` | 轨迹元信息白名单（工具名、技能名、时长、来源渠道等） |
| `bench` | `{tier, rank, seed, pool_size, stratum}` — 抽样可复算所需 |

### 3.2 统计 CSV 的格式（给可视化）

统一长表，直接 `group by dim, key` 就能画：

```
dim,key,bucket,count,share_in_bench,share_in_pool,delta_pp
```

- `dim`：维度名（severity / stage / mechanism / pattern / turn_type / day / difficulty /
  tokens / round / group / recalled_from / pattern_source）
- `key`：机器码（`S9` / `M5` / `S9.M5.1`）；`bucket`：人读名（答案合成 / 加工计算错误 / 叶名）
- `delta_pp`：bench 占比减池子占比（百分点）。**这是判断"抽偏没偏"的唯一指标**

`coverage.csv` 把每维压成一行：`max_abs_delta_pp`、`n_keys_missing`、`ok`。

---

## 4. 跑法

本机部署 + 跑（推荐）：

```bash
./bench_module/deploy.sh            # 只同步代码
./bench_module/deploy.sh --run      # 同步 + 后台起一次构建（首次要建定位表，较慢）
```

服务器上直接跑：

```bash
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
python3 bench_module/build_bench.py \
    --attr-root    /mnt/aime_eval_ssd/1_aime/eval_output \
    --cluster-root /mnt/aime_eval_ssd/1_aime/eval_output/cluster \
    --eval-input   /mnt/aime_eval_ssd/1_aime/eval_input \
    --out          /mnt/aime_eval_ssd/3_dynamic_bench
```

常用开关：

| 开关 | 默认 | 作用 |
|---|---|---|
| `--tiers` | `500,1000,5000,10000` | 要出哪些档位 |
| `--seed` | `20261118` | 抽样种子；换 seed = 换一批题，别随便换 |
| `--golden-share` / `--p0-cap` | `0.20` / `0.25` | 配额比例 |
| `--long-tokens` | `65536` | 长上下文的阈值（`total_tokens`） |
| `--skip-messages` | 关 | 只出统计和清单骨架，不取原文（试跑用） |
| `--models` | `aime_expert,aime_fast` | 建哪几个池 |

**首次运行**会为 `eval_input` 建 `_build/trace_loc.tsv`（156k 行，约 15MB），要读一遍几十 G 的
原文，十几分钟；之后按 `size+mtime` 增量刷新，重抽只 seek 读选中的那几千行。

---

## 5. 不变式（改动时别破坏）

1. `tier_500 ⊆ tier_1000 ⊆ tier_5000 ⊆ tier_10000`（同一次构建内）；
2. 同 `seed` + 同池子 + 同参数 → 产物逐字节确定（排序用 `md5(seed|trace_id)`，不用 `random`）；
3. 每个 L2 机制在每档至少 1 条（除非池子里该机制原本为 0）；
4. `GOLD` 行 `problems=[]`、`severity="GOLD"`；
5. 产物**元字段**内不出现绝对路径（`trace_id / batch_id / group / bench / stats`）；定位表内部也只用
   相对 `eval_input` 的路径。`messages / message_pool` 是模型原始轨迹，原样保留（含沙箱路径），
   改它就不是同一条 trace 了；
6. 只读聚类产物：`attr_root` / `cluster_root` / `eval_input` 下不写任何文件。

跑一遍自检：

```bash
python3 bench_module/tests/test_bench.py     # 端到端，约 0.3s，自带迷你夹具
```

---

## 6. 它故意不做什么

- **不做版本号/哈希快照**：用户明确要"一个完整测试集，动态加入"，每次重抽即可。
- **不做人工审核队列**：`GOLD` 是归因模型的判断（假阴性风险靠 `coverage.csv` 和下游抽检兜）。
- **不做可视化服务**：可视化由专门同事做，这里只保证数据是全的、口径是自解释的。
- **不接 `run_trees.sh`**：聚类流程不动。要"每轮自动补池"时，在外部调度里加一行 CLI 调用即可。
