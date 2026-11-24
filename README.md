# cluster_algorithm

把「流失 / 失败归因」条目聚成可下钻、可回溯、可复算的树。
输入：`*attribution*.jsonl`（问题）+ `eval_input`（身份 / 问句）；
输出：每棵树 11 个交付文件。

- AIME 的聚类只在**弗吉尼亚**（`aime_rsi_data`）跑，两棵树：`aime_fast` / `aime_expert`。
- 问财的聚类只在**乌兰察布**（`eval_data`）跑，4 + 6 棵树。
- 两个渠道不交叉、不合并；一台机器只跑自己的渠道。

## 当前状态（2026-11-23 21:30 CST）

| 树 | 条目 | 会话 | 叶子 | L4 | 最近一轮 | 校验 |
|---|---:|---:|---:|---:|---|---|
| `aime_fast` | 423,524 | 69,153 | 174 | 42 | 22:52 | `err=0 / mismatch=0` |
| `aime_expert` | 147,314 | 22,358 | 176 | 42 | 22:52 | `err=0 / mismatch=0` |

- 版本 **4.4.18**；分类法 **181 叶**（树里实际用到 174 / 176），其中 **12 个高影响叶子已下钻到 L4（42 个子类）**。
- L4 覆盖：fast 39.1% 条目 / **66.7%** 的 P0/P1；expert 33.5% / **58.1%**。
- 守护进程每 **2 小时**一轮增量；最近状态 `rc=0`，下一轮约 **23:24**。
- 接手先看 **[docs/handoff.md](docs/handoff.md)**：连服务器、文件与代码位置、风险、自检。

## 文档

| 文档 | 什么时候看 |
|---|---|
| [docs/handoff.md](docs/handoff.md) | **交接第一份**：两棵树现状、SSH、文件/代码位置、三分钟自检 |
| [docs/runbook.md](docs/runbook.md) | 日常：跑一轮、监控、交付检查、停/起守护 |
| [docs/deploy.md](docs/deploy.md) | 从零部署、换容器、环境初始化、代码怎么上去 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 出问题：网关 401/429、ENOMEM、缺叶、SSH、verify |
| [docs/methodology.md](docs/methodology.md) | 分类法：12 环节、7 机制、计数口径、质量协议 |
| [docs/tutorial.md](docs/tutorial.md) | 第一次用：输入格式、跑法、读结果、召回证据 |
| [docs/optimization.md](docs/optimization.md) | 三轮以上优化实验与最终指标 |
| [docs/changelog.md](docs/changelog.md) | 版本线、网关/容器变更、事故与修复 |

## 30 秒上手

看现状（服务器）：

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
tmux ls
cat logs/incr_status_aime.json
tail -n 5 runs/_runs.jsonl
```

手动跑一轮：

```bash
bin/preflight.sh
bin/run_trees.sh --incremental --no-full-fallback
```

改完代码（本机，不是服务器）：

```bash
./deploy_local.sh aime
```

连接信息在仓库根 `.servers.conf`（本机文件，已 gitignore）；
网关 key 在 `.deploy/local.conf`。两者都不入库。

## 仓库结构

```text
cluster_algorithm/
├── README.md                 唯一入口（根目录只有这一个 md）
├── docs/                     项目文档（每份 ≤300 行）
├── bin/                      聚类 / 归类 / 预检 / 守护 / 索引脚本
├── taxonomy/                 分类法：inventory / stages / INV_S*.md / MATRIX
├── prompts/                  归类与审计提示词
├── deploy_local.sh           本机 → 服务器：rsync + 渲染 env.sh + 预检
├── run.sh / run_cluster.sh   init / classify / finish 入口
├── VERSION.json              流水线与分类法版本
├── examples/                 参考产物
├── experiments/              优化实验与结果
├── audit/                    双盲审计
└── runs/                     本机旧实验残留；权威基线在服务器
```

`taxonomy/INV_*.md`、`taxonomy/MATRIX.md`、`prompts/*.md`、`examples/*.md` 是
**生成物 / 规格 / 样例**，不是项目文档，保持在原位。

## 约定

- 根目录只放 `README.md`；项目文档全部在 `docs/`，单份不超过 **300 行**。
- 代码/脚本里若出现旧文档名 `DEPLOY.md` / `RUNBOOK.md` / `TUTORIAL.md`，对应
  `docs/deploy.md` / `runbook.md` / `tutorial.md`（历史引用）。
- 一台机器只跑一个渠道；不共享 `runs/`、不跨机器搬基线。
- 分类法不接受「其他 / 未知」；未登记组件直接中断。
- 归类不兜底：非法 path 该片失败、整棵树当轮不交付，留 `unresolved_*.json` 供补叶。
- 交付必须 `verify` 的 `errors=0` 且 `locator_mismatch=0`。
- 服务器没有 `.git`，版本以 `.deploy/DEPLOYED.json` 的 `code_sha256` 为准。
