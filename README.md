# cluster_algorithm

把「流失 / 失败归因」条目聚成可下钻、可回溯、可复算的树。
输入：`*attribution*.jsonl`（问题）+ 可选 `eval_input`（身份 / 问句）；
输出：每棵树 11 个交付文件。

- **独立可移植模块**：[`cluster_pkg/`](cluster_pkg/README.md) 零第三方强依赖，外部项目只需配置 OpenAI 协议端点（兼容 Responses / Chat Completions）与既有聚类树目录，一条命令 `python cluster_pkg/run_cluster.py -c config.yaml`（或直接 `import cluster_pkg`）即可运行全量/增量聚类与 `--prune` 脏数据清理，并支持未知组件与新叶自适应创建；运行 `python3 bin/pack_cluster_pkg.py` 一键生成可分发的 `.whl` 与 `.tar.gz`。
- **双机生产渠道**：AIME 在**弗吉尼亚**（`aime_rsi_data`，`aime_fast` / `aime_expert`），问财在**乌兰察布**（`eval_data`，4 + 6 棵树）；两个渠道不交叉、不合并。

## 当前状态（2026-11-27 CST）

| 树 | 条目（PG 最新有效） | 叶子 | L4 | 后端 | 校验 |
|---|---:|---:|---:|---|---|
| `aime_fast` | 515,799 | 177 | 42 | PG 16 + Zstd | `err=0 / mismatch=0` |
| `aime_expert` | 173,319 | 178 | 42 | PG 16 + Zstd | `err=0 / mismatch=0` |

- 版本 **4.7.0**；基础分类法 **181 叶**，**12 个高影响叶子已下钻到 L4（42 个子类）**。
- **AIME 弗吉尼亚已切至 PG-Native**：以 PostgreSQL 16 `public.eval_problems` 为唯一状态真源，不再扫旧 `.jsonl` 与维护本地 `run_merged` 基线；守护进程每 **15 分钟**增量回填（`POST /api/v1/data-core/clusters/bind`），每日 CST 04:00 窗口（及新命中休眠叶时）自动发布整树快照到 `/mnt/aime_eval_ssd/1_aime/cluster_tree/<model>/v<ver>_<date>/`（维护 `current` 软链）；接手生产先看 **[docs/handoff.md](docs/handoff.md)**。

## 文档

| 文档 | 什么时候看 |
|---|---|
| [cluster_pkg/README.md](cluster_pkg/README.md) | **外部业务接入 / 独立模块**：`config.yaml`、CLI 与 Python 调用、自适应建叶 |
| [docs/handoff.md](docs/handoff.md) | **生产交接第一份**：两棵树现状、SSH、PG 布局、三分钟自检 |
| [docs/runbook.md](docs/runbook.md) | 日常运维：增量回填、整树发版、L4 补钻、召回、停/起守护 |
| [docs/pg_data_core_changes.md](docs/pg_data_core_changes.md) | `pg_data_core` 配合聚类 PG-Native 化的接口与发版契约 |
| [docs/deploy.md](docs/deploy.md) | 从零部署、换容器、环境初始化、代码怎么上去 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 出问题：网关 401/429、ENOMEM、缺叶、SSH、verify |
| [docs/methodology.md](docs/methodology.md) | 分类法：12 环节、7 机制、计数口径、质量协议、自适应演进 |
| [docs/tutorial.md](docs/tutorial.md) | 第一次用：输入格式、模块/脚本跑法、读结果、召回证据 |
| [docs/optimization.md](docs/optimization.md) | 优化实验记录与最终准确率/吞吐指标 |
| [docs/changelog.md](docs/changelog.md) | 版本线、网关/容器变更、事故与修复 |

## 30 秒上手

**外部项目直接调用模块（推荐）：**

```bash
python3 cluster_pkg/run_cluster.py -c cluster_pkg/config.example.yaml
```

**生产服务器查看与跑批（AIME / PG-Native）：**

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
bin/preflight.sh
python3 bin/pg_cluster.py status
python3 bin/pg_cluster.py incremental
python3 bin/pg_cluster.py publish
```

**本机同步代码到服务器：**

```bash
./deploy_local.sh aime
```

连接信息在 `.servers.conf`，网关 key 在 `.deploy/local.conf`（均已 gitignore，不入库）。

## 仓库结构

```text
cluster_algorithm/
├── README.md                 唯一根入口
├── cluster_pkg/              独立可移植聚类模块（内置分类法、自适应建叶、CLI + Python API）
├── bench_module/             动态评测集分层抽样与发版前 AB 切片模块（独立只读）
├── docs/                     项目文档（每份 ≤300 行）
├── bin/                      生产流水线：归类 / 聚合 / L4 / 预检 / 守护 / 索引
├── taxonomy/                 分类法真源：inventory / stages / l4_nodes / INV_S*.md / MATRIX
├── prompts/                  归类、L4 与审计提示词规格
├── run.sh                    原位流水线确定性分步入口（init / classify / merge / finish）
├── deploy_local.sh           本机 → 服务器：rsync + 渲染 env.sh + 部署指纹 + 预检
├── tests/                    仓库回归与冒烟测试套件
├── VERSION.json              流水线与分类法版本记录
├── examples/                 历史参考产物样例
├── experiments/              提示词与推理模式离线评估实验
└── audit/                    双盲标注一致性基准（576 条）
```

## 约定

- 根目录只放 `README.md`；项目文档全部在 `docs/`，单份不超过 **300 行**。
- 生产一台机器只跑一个渠道；不共享 `runs/`、不跨机器搬基线。
- 分类法禁止「其他 / 未知」兜底桶；新叶必须满足三轴互斥与 `taxonomy_lint` 零错误。
- 交付必须满足 `verify` 的 `errors=0` 且 `locator_mismatch=0`。
- 服务器没有 `.git`，版本以 `.deploy/DEPLOYED.json` 的 `code_sha256` 为准。
