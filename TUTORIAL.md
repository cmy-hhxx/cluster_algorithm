# 使用教程

两条路：**在 DSH 里说一句话**（推荐），或者**自己跑确定性部分、手动派归类代理**。

---

## 0. 你需要准备什么

一批归因数据，格式是评测导出的 `attribution.jsonl`（每天一个文件也行）：

```json
{"session_meta": {"session_id": "...", "request_time": "...", ...},
 "turns": [{"meta": {"trace_id": "...", "task": "...", ...},
            "problem": "[{\"problem_id\":\"p1\",\"component\":\"finquery 取数服务\",
                          \"label\":\"...\",\"definition\":\"...\",\"reason\":\"...\"}]"}],
 "session_problem": "[...]"}
```

必需字段只有三个：`turns[].problem[]` 里的 **component / label / definition**（reason 有则更好）。
`session_meta.session_id`、`turns[].meta.trace_id` 用于计数与回溯；缺了会归到空串，不影响聚类。

可选：`eval_session_inputs/` 目录（补 `case_id` 与用户问题原文）。

---

## 1. 最短路径：在 DSH 里说一句

```
用 cluster_algorithm 聚类：/path/to/attribution.jsonl，输出到 /path/to/out
```

我会做这几步（你也可以照着自己跑）：

1. `./run.sh init <数据> <run目录> [cases目录]` —— 解析 + 按段分片（确定性，秒级）
2. 对每个分片派一个归类代理（提示词 `prompts/01_assign.md`，**并发上限 8**）
3. `./run.sh finish <run目录> <输出目录> <全量会话数> <数据目录>` —— 聚合 + 校验 + 出报告
4. 报三个数：最大单点、最普遍机制、质量刻度

耗时参考：13,000 条约 67 个分片，8 并发跑完约 **40 分钟**。

---

## 2. 自己跑（分步）

```bash
cd ~/Desktop/cluster_algorithm

# ① 解析 + 分片
./run.sh init /path/to/attribution.jsonl ./run /path/to/eval_session_inputs
#    → run/items.jsonl  run/locators.jsonl  run/assign/*.jsonl  run/assign/manifest.json

# ② 归类（对 manifest 里每个分片派一个代理，提示词 prompts/01_assign.md）
#    替占位符：{{ROOT}} 仓库路径、{{RUN}} run 目录、{{L1}}、{{CHUNK}}、{{N}}
#    产出：run/assign/<chunk>.out.jsonl  每行 {"seq":1,"path":"S1.M2.1","edge":false}

# ③ 聚合 + 校验 + 报告
./run.sh finish ./run ./out 3316 /path/to/attributions
```

`finish` 末尾会跑 `verify`，必须看到：

```
leaves=133 total=13002 locator_mismatch=0 errors=0
```

不是这三个数就**不要交付**，先看 §7 排错。

---

## 3. 输出怎么读

| 文件 | 看什么 |
|---|---|
| `cluster_tree.json` | 主产物。每个叶子带 `count / sessions / share / definition / include / exclude / samples` |
| `cluster_tree.txt` | 开会用的树形打印，带条目数与会话数 |
| `cluster_summary.md` | 段分布、机制分布、横切标签、各段 Top 模式 |
| `cluster_matrix.png` | 环节 × 机制热力图，一眼看出哪段压在哪个机制 |
| `item_index.jsonl` | 全量索引：`seq → path + 原文件/行号/JSON路径`，可 join 任何下游 |
| `syndrome.md` | 会话级组合：哪些机制总是一起出现 |
| `leaves.tsv` | 叶子明细表，可直接导进表格 |
| `MANIFEST.json` | 版本、规模、文件校验和 |

**三个读数纪律**：

1. 看**会话数**，不看条目数——条目数会被“问题多的会话”放大。
2. `low_sample: true` 的叶子（条目 <10）不参与排名，只用于定位。
3. `edge: true` 的条目是边界判定，占 17% 左右，结论要绕开它们再做一次敏感性判断。

---

## 4. 召回原始证据

任何一个叶子都能回到原文：

```bash
# 看某个叶子的前 5 条（含定位）
python3 bin/cluster.py recall --node S1.M2.1 --out ./out

# 看全文（definition / reason），定位到 文件:行号 + turns[i].problem[j]
python3 bin/cluster.py recall --node S3.M3.1 --out ./out --limit 10 --full

# 整个段抽样
python3 bin/cluster.py recall --node S5 --out ./out --limit 3
```

输出里的 `attributions/2026-09-17.jsonl:5  turns[0].problem[0]` 就是坐标：
打开该文件第 5 行，取 `turns[0].problem[0]`，即原始归因对象。

---

## 5. 遇到新工具 / 新技能

分类法**不接受“未知/其他”桶**，遇到没登记的工具会直接中断：

```
未登记 component（已写入 unregistered.json）：new_tool_x
请先登记到 taxonomy/components.json 并指定所属段，再重跑。
```

处理三选一：

1. 归到已有段（最常见）——在 `taxonomy/components.json` 加一行：
   `"new_tool_x": {"tool": "new_tool_x", "label": "新工具"}`，
   再把 `new_tool_x` 加进 `taxonomy/stages.json` 里对应段的 `components`；
2. 它属于新的责任域——新增一个段（`S10`），同时升 `VERSION.json`；
3. 先不处理——把未登记条目从输入里剔除，跑其余部分。

登记完重跑 `ingest` + `chunk`，不需要重标已有条目。

---

## 6. 想再拆细（L4）

满足三条才拆：叶子 ≥300 条、能写出互斥 include/exclude、子类对应不同修复动作。

```bash
# 用 prompts/02_l4_induct.md 先产出拆分方案 run/l4/<leaf>.plan.json
# 再用 prompts/03_l4_assign.md 归类，path 写成 S1.M2.1.1
# 重跑 aggregate，第 4 层会自动挂上
python3 bin/cluster.py aggregate --work ./run --out ./out
```

---

## 7. 排错

先跑一遍冒烟测试，确认代码本身没问题：

```bash
./tests/smoke.sh        # 3 条样本走完 init → 归类 → finish → verify，输出 SMOKE OK
```


| 现象 | 原因 | 处理 |
|---|---|---|
| `FileNotFoundError: .../attributions` | 输入路径写错或数据已移动 | 用绝对路径，先 `ls` 确认 |
| `bad_lines > 0` | 源 JSON 有坏行 | `run/stats.json` 里有行号，先修数据或剔除 |
| `未登记 component` | 新工具 | 见 §5 |
| `verify` 报 `index path counts != tree counts` | 有分片没跑完/路径越界 | 看 `assign/manifest.json` 哪些 chunk 缺 `.out.jsonl` |
| `locator_mismatch > 0` | 源文件在跑完之后被改动 | 重跑 `ingest`，不要手工编辑 items |
| 某段叶子全是 `low_sample` | 该段样本太少 | 报表里标注“仅作定性”，不要参与排名 |

---

## 8. 质量抽检（可选，建议每次换数据都做）

```bash
# 抽样：576 条分层（每叶 ≥3），两份独立标注，输出到 audit/a、audit/b
# 提示词见 prompts/04_audit.md（盲法：输入不含原判定）
python3 - <<'PY'   # 抽样脚本见 audit/REPORT.md 或按各叶比例自建
PY

# 算 α
python3 bin/krippendorff.py audit/a/all.jsonl audit/b/all.jsonl
```

判读：**α ≥ 0.75** 合格；0.6–0.75 说明某些叶子定义不够互斥；**< 0.6 的叶子退回重定义**，不重标整棵树。

本次实测：α=0.918，两标注员一致率 91.8%，但与原判只有 82.5%——详见 `audit/REPORT.md`。

---

## 9. 一页速查

```bash
./run.sh init <数据> <run> [cases]        # 解析+分片
#  → 派归类代理（prompts/01_assign.md，8 并发）
./run.sh finish <run> <out> <会话数> <数据目录>   # 聚合+校验+报告

python3 bin/cluster.py recall --node <叶> --out <out> [--full]   # 召回
python3 bin/krippendorff.py <a.jsonl> <b.jsonl>                  # 信度
```

产物 8 个文件，主看 `cluster_tree.json` 与 `cluster_summary.md`。
