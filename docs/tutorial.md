# 使用教程

> 第一次接触这个仓库从这里开始。外部项目接入优先用 [`cluster_pkg`](../cluster_pkg/README.md)；
> 生产运维看 [runbook.md](runbook.md)，弗吉尼亚现状看 [handoff.md](handoff.md)。

## 0. 输入长什么样

每条归因是一个 JSONL 行，支持标准多轮归因格式：

```json
{"session_meta": {"session_id": "...", "request_time": "..."},
 "turns": [{"meta": {"trace_id": "...", "task": "..."},
            "problem": [{"problem_id": "p1", "component": "finquery",
                         "label": "...", "definition": "...", "reason": "...", "severity": "P1"}]}]}
```

- 必需字段：`component / label / definition`（`reason` 有则更准，`problem` 为数组或 JSON 字符串均可）。
- 身份主键为 **`trace_id`**；可选传入 `eval_input`（或 `cases_*.jsonl`）按 `trace_id` 补齐 `question` 与 `session_id`。
- `cluster_pkg` 同时兼容单行扁平问题格式（每行直接含 `trace_id / component / label / definition / reason`）。

## 1. 方式一：使用独立模块 `cluster_pkg`（推荐外部项目调用）

无需特定环境、零第三方强依赖。准备一份极简 `config.yaml`：

```yaml
api:
  base_url: "http://10.217.220.51/ai-gateway/v1"   # OpenAI-Responses 协议端点
  api_key: "${EVAL_GATEWAY_API_KEY}"
  model: "deepseek-v4.1-flash"

tree_dir: "./my_cluster_tree"                      # 既有聚类树目录（空目录则自动初始化）
input: "./data/attribution"                        # 归因文件或目录
```

一条命令运行：

```bash
python3 cluster_pkg/run_cluster.py -c config.yaml
```

- 若 `tree_dir` 已有历史聚类树，自动按 `(trace_id, problem_index)` 增量复用，只对新增条目调用模型；
- 遇到未登记 `component` 或分类法缺叶（重问判 `NONE`），自动完成责任段路由或归纳新叶，通过分类法自检后写回 `tree_dir/taxonomy/`。
- 其他 Python 项目也可以直接 `from cluster_pkg import run_cluster` 调用，详见 [`cluster_pkg/README.md`](../cluster_pkg/README.md)。

## 2. 方式二：生产服务器跑批

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
tmux new -s cluster

bin/preflight.sh                                      # 8 项预检；有 FAIL 或 BLOCKED 不开跑
bin/run_trees.sh --incremental --no-full-fallback     # 日常增量
bin/run_trees.sh --only aime_fast --incremental       # 只跑一棵
bin/run_trees.sh --dry-run                            # 只 ingest+chunk，不调模型
```

> 守护进程在跑时不要手动并发全量；先按 [runbook.md](runbook.md) §6 停掉看门狗和主循环。

手动分步（单批调试用）：

```bash
./run.sh init /path/to/attribution.jsonl ./run /path/to/eval_input aime
./run.sh classify ./run --concurrency 4 --effort none
./run.sh finish ./run ./out 0
```

`finish` 内部自动执行 `verify`，必须看到 `locator_mismatch=0 errors=0` 才算有效交付。

## 3. 输出怎么读

每棵树产出 **11 个交付文件**（`cluster_pkg` 若无 Pillow/中文字体则额外提供无依赖矢量图 `cluster_matrix.svg`）：

| 文件 | 看什么 |
|---|---|
| `cluster_brief.md` | 一页纸结论：最该先修的 3 件事、L4 高优子类、失效层、可信度。**给业务看这份** |
| `cluster_tree.json` / `.txt` | 主产物树（含样本与统计）及终端可读树形图 |
| `cluster_summary.md` | 段分布、机制分布、横切标签、各段 Top 模式、L4 汇总 |
| `cluster_matrix.png` | 环节 × 机制热力图 |
| `item_index.jsonl` | 全量条目索引：`seq → path + trace_id + session_id + component + label` |
| `syndrome.md` / `.json` | 会话级失效机制组合 |
| `leaves.tsv` | 叶子明细表，可直接导入表格工具 |
| `MANIFEST.json` / `verify.json` | 版本指纹与结构自洽校验结果 |

**三个读数纪律**：
1. 排序看**会话数**，不看条目数——避免“单会话多报错”放大。
2. `low_sample: true`（条目 <10）只用于定位，不参与排名。
3. `edge: true` 为边界样本，做排名前优先复核。

## 4. 召回原始证据与 L4 下钻

```bash
# 按节点召回样本（支持 L1 / L2 / L3 / L4 任意前缀）
python3 bin/cluster.py recall --out ./out --node S1.M2.1 --limit 10 --full

# 对满足准入的高影响叶子执行 L4 第四层下钻
python3 bin/l4_apply.py --run ./run --leaves S9.M6.4,S3.M3.2 --concurrency 4
./run.sh finish ./run ./out 0
```

拿召回结果里的 **`trace_id`** 回 `eval_input` 即可还原完整对话上下文。

## 5. 质量抽检（换领域或大改分类法时）

```bash
python3 bin/taxonomy_lint.py
python3 bin/krippendorff.py audit/a/all.jsonl audit/b/all.jsonl
```

标准：`taxonomy_lint` 必须 `errors=0`；双盲 **α ≥ 0.75** 合格（历史实测 α=0.918，见 `audit/REPORT.md`）。
