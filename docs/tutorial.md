# 使用教程

> 第一次接触这个仓库从这里开始。想直接跑生产看 [runbook.md](runbook.md)，
> 想知道现在两棵树什么状态看 [handoff.md](handoff.md)。

## 0. 输入长什么样

每条归因是一个 JSONL 行，核心结构：

```json
{"session_meta": {"session_id": "...", "request_time": "..."},
 "turns": [{"meta": {"trace_id": "...", "task": "..."},
            "problem": "[{\"problem_id\":\"p1\",\"component\":\"finquery 取数服务\",
                          \"label\":\"...\",\"definition\":\"...\",\"reason\":\"...\"}]"}],
 "session_problem": "[...]"}
```

- 必需字段：`turns[].problem[]` 里的 **component / label / definition**（`reason` 有则更好）。
- `session_meta.session_id`、`turns[].meta.trace_id` 用于计数与回溯；缺了会归到空串，不影响聚类。
- 2026-11-11 起身份以 **`trace_id`** 为准；问句/会话 id 优先从 `eval_input` join，
  归因里没有也能跑，只是清单里少部分身份信息。

## 1. 最短路径

模型网关只有内网可达，真正的聚类在服务器上跑；本机只负责发起和看结果。

如果你在 DSH 里，直接说：

```text
把 AIME 这批聚一下
```

对应动作：弗吉尼亚 `aime_rsi_data` 上 `bin/run_trees.sh`，跑完给 11 个文件和三个数。
背后是：`run.sh init`（解析+分片）→ `run.sh classify`（直调网关）→ `run.sh finish`
（聚合+校验+报告）。

## 2. 自己跑（服务器上）

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
tmux new -s cluster

bin/preflight.sh          # 8 项；有 FAIL 不许跑，只有 BLOCKED 也不许跑
bin/run_trees.sh          # 本渠道全部树（全量）
bin/run_trees.sh --incremental --no-full-fallback    # 日常增量
bin/run_trees.sh --only aime_fast --incremental       # 只跑一棵
bin/run_trees.sh --dry-run                            # 只 ingest+chunk
```

> 守护进程正在跑一轮时不要手动再开；先按 [runbook.md](runbook.md) §6 停掉主循环和看门狗。

单文件临时跑一棵（调脚本用）：

```bash
./run_cluster.sh /path/to/attribution.jsonl runs/<批次>/run runs/<批次>/out [会话数] [cases 目录]
```

手动分步（调试用）：

```bash
./run.sh init /path/to/attribution.jsonl ./run /path/to/eval_input
#    → run/items.jsonl  run/locators.jsonl  run/assign/manifest.json

./run.sh classify ./run --concurrency 4 --effort none
#    → run/assign/<chunk>.out.jsonl  每行 {"seq":1,"path":"S1.M2.1","edge":false}

./run.sh finish ./run ./out <全量会话数>
```

`finish` 末尾会跑 `verify`，必须看到：

```text
leaves=<N> total=<M> locator_mismatch=0 errors=0
```

不是 `errors=0 / locator_mismatch=0` 就**不要交付**；先看 [troubleshooting.md](troubleshooting.md)。

> 并发：AIME 当前 4，问财 12。不要用 24/32 去“提速”——AIME 的托管网关按 key 限流。

## 3. 输出怎么读

每棵树 11 个文件，在 `<AIME_ROOT>/cluster/<树>/`：

| 文件 | 看什么 |
|---|---|
| `cluster_tree.json` | 主产物。每个叶子带 `count / sessions / share / definition / include / exclude / samples` |
| `cluster_tree.txt` | 开会用的树形打印，带条目数与会话数 |
| `cluster_brief.md` | 一页纸：最该先修的 3 件事、失效层、可信度。**给业务看这一份** |
| `cluster_summary.md` | 段分布、机制分布、横切标签、各段 Top 模式 |
| `cluster_matrix.png` | 环节 × 机制热力图，一眼看出哪段压在哪个机制 |
| `item_index.jsonl` | 全量索引：`seq → path + trace_id + session_id`，可 join 任何下游 |
| `syndrome.md` / `syndrome.json` | 会话级组合：哪些机制总是一起出现 |
| `leaves.tsv` | 叶子明细表，可直接导进表格 |
| `MANIFEST.json` | 版本、规模、文件校验和 |
| `verify.json` | 校验结果（必须 `errors=0 / locator_mismatch=0`） |

**三个读数纪律**：

1. 看**会话数**，不看条目数——条目数会被“问题多的会话”放大。
2. `low_sample: true` 的叶子（条目 <10）不参与排名，只用于定位。
3. `edge: true` 是边界标记，先复核再做排名。

## 4. 召回原始证据

任何一个叶子都能回到原文：

```bash
# 看某个叶子的前 5 条
python3 bin/cluster.py recall --node S1.M2.1 --out ./out

# 看全文（definition / reason）
python3 bin/cluster.py recall --node S3.M3.1 --out ./out --limit 10 --full

# 整个段抽样
python3 bin/cluster.py recall --node S5 --out ./out --limit 3
```

输出里会带 `case / trace / session / src_rel / question / component / path`。
4.4 起不再回放文件行号：拿 **`trace_id`** 去 `eval_input` 查完整 messages，才是唯一可靠坐标。

## 5. 遇到新工具 / 新技能

分类法**不接受“未知/其他”桶**，遇到没登记的工具会直接中断：

```text
未登记 component（已写入 unregistered.json）：new_tool_x
请先登记到 taxonomy/components.json 并指定所属段，再重跑。
```

处理三选一：

1. **归到已有段**（最常见）：在 `taxonomy/components.json` 加一行，带 `tool / label / role / stage`；
   再把工具加进 `taxonomy/stages.json` 对应段的 `components` 列表。
2. **它属于新的责任域**：新增段（如 `S13`），同步 `inventory.json`、`stages.json`，升 `VERSION.json`。
3. **先不处理**：把未登记条目从输入剔除，跑其余部分。

登记后重跑 `ingest` + `chunk`，不需要重标已有条目。

## 6. 想再拆细（L4）

满足三条才拆：叶子 ≥300 条、能写出互斥 include/exclude、子类对应不同修复动作。

```bash
# 用 prompts/02_l4_induct.md 产出拆分方案
# 用 prompts/03_l4_assign.md 归类，path 写成 S1.M2.1.1
python3 bin/cluster.py aggregate --work ./run --out ./out
```

L4 节点元数据写 `taxonomy/l4_nodes.json`，重跑 `aggregate` 会自动挂到第 4 层。
当前 AIME 没有达标叶子，L4 为空。

## 7. 质量抽检（可选，建议换数据时做）

```bash
# 分层抽 576 条（每叶 ≥3），两份独立标注，提示词见 prompts/04_audit.md
python3 bin/krippendorff.py audit/a/all.jsonl audit/b/all.jsonl
```

判读：**α ≥ 0.75** 合格；0.6–0.75 说明某些叶子定义不够互斥；**< 0.6 的叶子退回重定义**，
不重标整棵树。历史实测 α=0.918，详见 `audit/REPORT.md`。

## 8. 一页速查

```bash
./run_cluster.sh <数据> <run> <out> [会话数] [cases]   # 一条命令跑完
./run.sh init <数据> <run> [cases]                     # 解析+分片
./run.sh classify <run> --concurrency 4 --effort none  # 归类
./run.sh finish <run> <out> <会话数>                   # 聚合+校验+报告
python3 bin/cluster.py recall --node <叶> --out <out> [--full]
python3 bin/krippendorff.py <a.jsonl> <b.jsonl>
```

业务主看 `cluster_brief.md`，分析主看 `cluster_tree.json` 与 `cluster_summary.md`；
完整交付清单见 [handoff.md](handoff.md) §6。
