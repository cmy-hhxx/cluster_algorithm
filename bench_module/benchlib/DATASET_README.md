# AIME 动态 Bench — 数据说明

> 本文件由 `bench_module` 自动生成，描述**本目录当前这一版数据**。
> 代码与口径见仓库 `cluster_algorithm/bench_module/README.md`。

| | |
|---|---|
| 构建时间 | @@BUILT_AT@@ |
| 口径版本 | bench_module v@@VERSION@@ |
| 抽样种子 | `@@SEED@@` |
| 主池规模 | **@@POOL_CASES@@** 条 case（expert @@POOL_EXPERT@@ / fast @@POOL_FAST@@），最近刷新 @@LAST_REFRESH@@ |
| 可采样 | @@USABLE@@ 条（过质量闸后） |
| join 命中率 | @@JOIN_RATE@@ |

---

## 0. 一句话

**主池**是唯一真源（`pool/`，每天自动入库新 case）；**档位**是从主池按配额抽出来的评测集
（`tier_*.jsonl`）。池子天天变，档位什么时候重抽由发版节奏决定——这样两次发版之间的分数才可比。

```
归因 jsonl ──┐
             ├─→ pool/<model>.jsonl   （主池，每日增量刷新）
聚类 item_index┘         │
                         ├─→ tier_500 / 1000 / 5000 / 10000
eval_input（取 messages）─┘
```

---

## 1. 按角色看

| 你是 | 该看什么 |
|---|---|
| 跑评测 | `tier_<N>.jsonl`，一行一个 case，`messages` 是完整上下文，直接喂模型 |
| 做可视化 | `stats/*.csv`（长表，见 §5）+ `pool_stats.json`（基准线） |
| 维护数据 | §3 主池、§9 操作手册 |
| 查口径 | §6 字段、§7 铁律、§8 已知边界 |

---

## 2. 目录

```
3_dynamic_bench/
├── README.md                 ← 本文件
├── manifest.json             ← 构建参数、主池状态、每档 requested/actual、质量指标
├── pool/                     ← 主池（唯一真源）
│   ├── aime_expert.jsonl     一行一个 case，只存小字段（无 messages）
│   ├── aime_fast.jsonl
│   ├── state.json            归因文件 size+mtime（增量刷新的依据）
│   └── manifest.json         最近一次刷新的统计
├── aime_expert/              （aime_fast 同结构）
│   ├── pool_stats.json       池子全量分布，bench 是否无偏的基准线
│   ├── tier_500.jsonl        档位：一行一个 case，全内联、自包含
│   ├── tier_1000.jsonl
│   ├── tier_5000.jsonl
│   ├── tier_10000.jsonl
│   ├── long/                 长上下文单独成档（total_tokens ≥ 阈值）
│   │   └── tier_<k>_long.jsonl
│   └── stats/
│       ├── tier_<N>.json                          计数 / 配额完成度
│       ├── tier_<N>.dim_<维度>.csv                8 个维度长表
│       ├── tier_<N>.coverage.csv                  每维偏差 + 是否设计性偏差
│       └── tier_<N>.crosstab_stage_mechanism.csv  环节 × 机制
└── _build/
    ├── trace_loc.tsv          trace_id → eval_input 文件偏移
    ├── trace_loc.state.json   文件 size+mtime
    ├── build_<时间戳>.log     每次构建的日志
    └── pool_refresh.log      每日刷池日志
```

---

## 3. 主池 `pool/`

### 3.1 里面是什么

一行一个 case（= 一个 `trace_id`），**只存小字段，不存 messages**（messages 在档位里才内联）。

| 字段 | 说明 |
|---|---|
| `model` | `aime_expert` / `aime_fast` |
| `trace_id` / `session_id` | 身份；`trace_id` 可回 `eval_input` 还原原文 |
| `question` | 真问句（已切掉系统注入模板） |
| `question_full` | 原始问句；只有被模板污染时才非空，留档可回溯 |
| `request_time` / `batch_id` | 请求时间与批次日 |
| `group` | 来源目录（`0_daily_eval` / `2_task_eval/...`） |
| `session_round` / `turn_index` | 轮次；`session_round > 1` 记为多轮 |
| `total_tokens` / `input_tokens` | 上下文规模（缺失记 0，不参与统计口径） |
| `problems[]` | 归因给出的问题列表：`index / severity / label / component / component_role` |
| `first_seen` / `last_seen` | 首次入池时间 / 最近一次被归因覆盖的时间 |
| `attr_file` / `attr_mtime` | 来源归因文件与 mtime（增量刷新用） |

> 池子里**没有** `l1/l2/l3`。错误模式标签是抽样时实时 join 聚类产物
> `cluster/<model>/item_index.jsonl` 得到的——这样聚类补叶后，老 case 自动获得更细的标签。

### 3.2 怎么更新

- **自动**：服务器上 tmux 会话 `bench-pool-aime`，每 24 小时跑一次 `--pool-only`，只刷池不动档位。
- **手动**：`bash bench_module/pool_daemon.sh once`
- **机制**：`state.json` 记着每个归因文件的 `size+mtime`，只解析新增/变更的文件；同一 trace 以
  **最新一次归因**为准（重跑会修正标签），`first_seen` 保留最早入池时间。刷新幂等，重复跑不会重复计数。

### 3.3 新鲜度自检

```bash
python3 - <<'EOF'
import json
s = json.load(open('/mnt/aime_eval_ssd/3_dynamic_bench/pool/state.json'))
print('归因文件', len(s['files']), '| 池子 case', s['cases'], '| 最近刷新', s['last_refresh'])
EOF
```

---

## 4. 档位 `tier_<N>.jsonl`

### 4.1 配额规则

每档 = **golden 20%** + **badcase 80%**；badcase 内 P0 封顶 25%，其余按 **P1:P2:P3:P4 = 4:3:2:1**；
severity 内部再按 **L2 机制均分**（每机制至少 1 条），机制内部按 **单轮:多轮 = 75:25**。

### 4.2 本次实际

@@TIER_TABLE@@

某池凑不满某档时**能出几档出几档**：`manifest.json` 里如实记 `requested` / `actual` / `shortfall`，
绝不重复计数、绝不拿低严重度硬凑。

### 4.3 三条性质

1. **嵌套**：`tier_500 ⊆ tier_1000 ⊆ tier_5000 ⊆ tier_10000`。所以"加题后掉分"可以直接解读成
   新增题目更难，而不是换了批题。
2. **确定性**：同 `seed` + 同池子 + 同参数 → 结果逐字节一致（排序用 `md5(seed|trace_id)`）。
3. **长上下文单独成档**：`total_tokens ≥ @@LONG_TOKENS@@` 的 case 不进基础档，
   落 `long/tier_<k>_long.jsonl`，避免拉高平均值掩盖问题。

---

## 5. 统计与可视化

### 5.1 长表格式

```
dim,key,bucket,count,share_in_bench,share_in_pool,delta_pp
```

- `dim`：维度名；`key`：机器码；`bucket`：人读名
- `delta_pp` = bench 占比 − 池子占比（百分点）

### 5.2 有哪些维度

| dim | key 示例 | bucket 示例 | 含义 |
|---|---|---|---|
| `severity` | `P0` / `GOLD` | 同 key | 严重度；`GOLD` = 归因未发现任何问题 |
| `stage` | `S9` | 答案合成 | L1 环节（只统计有缺陷的 case） |
| `mechanism` | `M5` | 加工计算错误 | L2 机制（只统计有缺陷的 case） |
| `pattern` | `S9.M5.1` | 叶名 | L3 错误模式（只统计有缺陷的 case） |
| `turn_type` | `single` / `multi` | 同 key | 单轮/多轮 |
| `day` | `2026-11-19` | 同 key | 请求日期 |
| `round` | `1` / `2-3` / `4-10` / `11+` | 同 key | 会话轮次分桶 |
| `group` | `0_daily_eval` | 同 key | 来源 |

**没有兜底桶**：不存在 NA / unknown / "其他"。字段撑不起口径的维度已被删掉
（难度、token 分位、recalled_from），join 不上的 case 不进 bench。

### 5.3 怎么读 `delta_pp`（重要）

`coverage.csv` 里 `by_design=yes` 的维度（severity / stage / mechanism / pattern / turn_type）
**故意**偏离池子分布——bench 就是要多放 P0、每个机制都要有、单多轮按 75:25。
这些维度的 `delta_pp` 是设计结果，不是抽样误差。

真正衡量抽样质量的是**非**设计维度：`day / round / group`，这些应该贴近 0。
如果它们飙了，说明抽样在时间或来源上产生了偏斜。

### 5.4 建议图表

| 文件 | 适合画 |
|---|---|
| `dim_severity.csv` | 堆叠/环形：各严重度占比（含 GOLD 20%） |
| `dim_stage.csv` | 条形：12 个环节的缺陷分布 |
| `dim_mechanism.csv` | 条形：7 种机制 |
| `dim_pattern.csv` | 树图/热力：148 个叶的分布（长尾明显） |
| `crosstab_stage_mechanism.csv` | 热力图：环节 × 机制 |
| `dim_day.csv` + `dim_round.csv` | 折线/条形：时间与轮次覆盖 |
| `coverage.csv` | 表格/雷达：抽样是否偏了 |

---

## 6. 档位行字段

| 字段 | 说明 |
|---|---|
| `trace_id` / `session_id` | 身份；**元字段不含任何绝对路径** |
| `model` | `aime_expert` / `aime_fast` |
| `question` | 本轮问句（多轮时是最后一轮）；`question_full` 仅在被模板污染时出现 |
| `messages` | **完整上下文**：多轮含前序轮次、工具调用与返回 |
| `message_pool` | 会话级 system/背景消息池 |
| `turn` | `single` / `multi` |
| `session_round` / `turn_index` | 轮次 |
| `severity` | `P0`–`P4` / `GOLD` |
| `primary` | 主标签 `{l1, l2, l3}`，形如 `S9` / `M5` / `S9.M5.1` |
| `problems[]` | 全部 problem：`severity / label / path / path_source / component` |
| `stats` | `total_tokens / input_tokens / tool_count / latency / dup_question / short_followup` |
| `meta` | 轨迹元信息白名单（工具名、技能名、时长、来源渠道等） |
| `bench` | `{tier, rank, seed, pool_size, stratum}` —— 复算抽样所需 |

> `messages` / `message_pool` 是模型原始轨迹，**原样保留**（沙箱路径等不做清洗），
> 改了就不是同一条 trace 了。

---

## 7. 口径铁律

1. **case = `trace_id`**，一行一个；多轮的上下文已经在 `messages` 里。
2. **`GOLD` 不等于人工确认无问题**，它是"归因模型没发现 problem"；假阴性靠下游抽检发现。
3. **join 不上就不进 bench**：任意一条 problem 落不到分类法叶，整条 case 剔除（本轮 @@JOIN_FAILED@@ 条）。
4. **去重**：按「规范化问句 + 单/多轮」合并，同一句话只算 1 个 case（本轮合并 @@DUP@@ 条）。
5. **问句质量闸**：单轮问句过短或纯 URL 剔除（本轮 @@Q_SHORT@@ / @@Q_URL@@ 条）；
   多轮短追问（"还有呢？"）有上文，保留并标 `short_followup`（本轮 @@Q_FOLLOW@@ 条）。
6. **模板切分**：问句里被塞进的系统注入块（附件/URL 处理说明）已切掉，原文进 `question_full`。
7. **只增不删**：池子不会因为一次归因没判出问题就丢掉 case；是否进 bench 由抽样时决定。

---

## 8. 已知边界

- **个别 trace 取不到原文**：归因里有、`eval_input` 里没有时，该条会被跳过，档位实际条数小于请求值
  （见 `manifest.json` 的 `shortfall`），不会拿别的 case 顶替。
- **时间分布偏新**：池子按天累积，近期批次量更大，`day` 维度天然偏新（这不算抽样偏差，
  `coverage.csv` 的 `day` 行可以核对）。
- **expert 的 P0 池子上限很低**（百量级），所以 500 档里 P0 占比接近 25% 上限，
  而 10000 档里 P0 会被稀释到 1% 左右——这是池子容量决定的。
- **难度标签已废弃**（大面积缺失），不参与任何分层与统计。

---

## 9. 操作手册

```bash
# 手动刷一次主池（新问句立刻进池）
bash bench_module/pool_daemon.sh once

# 守护状态 / 起 / 停
bash bench_module/pool_daemon.sh status
bash bench_module/pool_daemon.sh start
bash bench_module/pool_daemon.sh stop

# 从现有池子重抽档位（发版前，秒级）
python3 bench_module/build_bench.py \
  --attr-root  /mnt/aime_eval_ssd/1_aime/eval_output \
  --eval-input /mnt/aime_eval_ssd/1_aime/eval_input \
  --out        /mnt/aime_eval_ssd/3_dynamic_bench \
  --no-refresh --tiers 500,1000,5000,10000

# 刷池 + 抽档一次做完（去掉 --no-refresh 即可）
python3 bench_module/build_bench.py --attr-root ... --eval-input ... --out ...
```

读某一条 case（回原文）：

```bash
python3 - <<'EOF'
import json
tid = "2000302017..."
for line in open('/mnt/aime_eval_ssd/3_dynamic_bench/aime_expert/tier_500.jsonl'):
    r = json.loads(line)
    if r['trace_id'] == tid:
        print(r['question']); print(len(r['messages']), 'messages'); break
EOF
```

自检（仓库里）：`python3 bench_module/tests/test_bench.py`

---

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-20 | 主池落盘 + 24h 增量刷新；档位与池子解耦；定位表按 size+mtime 对账 |
| 2026-11-20 | 统计口径去兜底桶（NA / unknown / 其他）；GOLD 退出错误模式维度 |
| 2026-11-19 | 未分类 case 不进 bench；问句模板切分；单轮垃圾问句剔除 |
| 2026-11-18 | 首版：归因 × 聚类 join、分层配额、四档嵌套 |
