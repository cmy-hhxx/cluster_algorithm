# 交接：AIME 的两棵树（弗吉尼亚）

> 快照 **2026-11-24 CST**｜版本 **4.5.0**｜容器 `interactive-s3ynzadcvm9c`
> 写给接手的人：先看这一份。命令可直接复制；数字会随每 2 小时一轮变化，**结构和路径不变**。
> 核对当前数字的方法见 §7；遇到异常先看 [troubleshooting.md](troubleshooting.md)。

## 1. 一分钟结论

AIME 的生产聚类只在**弗吉尼亚一台机器**上跑，当前只有**两棵树**：

| 树 | 是什么 | 条目 | 会话 | 叶子 | L4 子类 | 最近成功一轮 | 校验 |
|---|---|---:|---:|---:|---:|---|---|
| `aime_fast` | 快速模型 | **423,524** | 69,153 | 174 | 42 | 2026-11-23 22:52 | `err=0 / mismatch=0` |
| `aime_expert` | 专家模型 | **147,314** | 22,358 | 176 | 42 | 2026-11-23 22:52 | `err=0 / mismatch=0` |

- 两棵树都由 tmux 守护进程每 **2 小时**自动增量（跑完再睡 7200s），状态 `rc=0`。
- 代码在服务器 `/mnt/aime_eval_ssd/code/2_cluster_algorithm`，数据在 `/mnt/aime_eval_ssd`。
- 交付物在 `<数据根>/cluster/<树>/`，共 11 个文件；业务先看 `cluster_brief.md`。
- 本机负责维护源码与 `./deploy_local.sh aime`；其他业务方若需独立调用聚类能力，直接使用仓库内的可移植模块 [`cluster_pkg/`](../cluster_pkg/README.md)。

## 2. 两棵树是什么

**树 = 一个模型的全量归因**，不是按日期拆的；日常与流失都归到同一个 model 名下，树名就是 model 名。当前输入集中在 `0_daily_eval/`，身份统一为 `trace_id`。

| 项 | `aime_fast` | `aime_expert` |
|---|---|---|
| 输入目录 | `0_daily_eval/aime_fast/` | `0_daily_eval/aime_expert/` |
| 条目 / 会话 | 423,524 / 69,153 | 147,314 / 22,358 |
| 实际用到叶子 / L4 子类 | 174 / 42 | 176 / 42 |
| L4 覆盖率（全部条目 / P0+P1） | 39.1% / 66.7% | 33.5% / 58.1% |

**几个容易误会的点：**

- **“只有两棵树”不等于“只有两个目录”**：`eval_output` 下还有 `2_task_eval/`（流失回测）、`stress-*`（压测）、`0_daily_eval-pre-split-*`（历史备份）。树发现只读 `AIME_GROUPS = ('0_daily_eval', '1_lost_user_eval')`，这些都不进树。
- `1_lost_user_eval/` 在新根下已合并进 `0_daily_eval/`；`discover_trees.py` 仍兼容保留该组名，缺失组会自动跳过。
- **叶子 174 / 176 是“这棵树实际命中多少”**，不是分类法总数。基础分类法当前 **181 叶**，其中 **12 个高影响叶下钻了 42 个 L4 子类**。
- 预检会报全根 `*attribution*.jsonl` 总数（含回测、压测），不等于实际入树文件数。

## 3. 怎么连服务器

```bash
ssh aime_rsi_data          # 别名已在 ~/.ssh/config 配好
```

| 项 | 值 |
|---|---|
| SSH 别名 | `aime_rsi_data` |
| 主机 / 端口 | `10.217.219.2` / **2473**（容器 22 的映射） |
| 用户 / 认证 | `root`，密钥认证 |
| 容器 | `interactive-s3ynzadcvm9c` |
| 代码目录 | `/mnt/aime_eval_ssd/code/2_cluster_algorithm` |
| 数据根 | `/mnt/aime_eval_ssd/1_aime/eval_output` |

- 主机、端口、密钥路径的**唯一来源**是仓库根 `.servers.conf`（已 gitignore，不含私钥内容）。
- SSH 偶发 `Connection refused`，等几秒重试即可；长任务必须放 tmux。
- 若改过端口或容器，先 `ssh -O exit aime_rsi_data` 清理复用连接。

**连上后先看这三样：**

```bash
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
tmux ls                                   # cluster-incr-aime / cluster-watch-aime
cat logs/incr_status_aime.json            # 最近一轮 rc / 时间
tail -n 5 runs/_runs.jsonl                # 两棵树的账本
```

## 4. 文件和代码各自在哪

### 4.1 布局总览

```text
本机（唯一源码，git 仓库）
/Users/cmy/Desktop/cluster_algorithm/
├── README.md                 唯一根入口
├── cluster_pkg/              独立可移植模块（外部项目直接引用）
├── bench_module/             动态评测集抽样模块
├── docs/                     所有项目文档
├── bin/ taxonomy/ prompts/   生产流水线与分类法
├── deploy_local.sh           本机 → 服务器同步
├── .servers.conf             SSH / 端口（gitignore）
└── .deploy/local.conf        网关 key（gitignore）

服务器（生产）
/mnt/aime_eval_ssd/
├── 1_aime/
│   ├── eval_output/                    AIME_ROOT
│   │   ├── 0_daily_eval/aime_fast/     归因输入
│   │   ├── 0_daily_eval/aime_expert/   归因输入
│   │   └── cluster/{aime_fast,aime_expert}/   交付 11 件
│   └── eval_input/                     身份 / 问句（trace 粒度）
├── cases_aime.jsonl                    身份索引（TTL 1h 增量刷新）
├── badcase_aime.jsonl / goldencase_aime.jsonl
└── code/2_cluster_algorithm/
    ├── runs/                           基线 / 账本 / staging
    ├── logs/                           运行日志
    └── .deploy/                        env.sh + 部署指纹 + 字体
```

### 4.2 关键路径说明

| 内容 | 位置 | 说明 |
|---|---|---|
| 当前基线 | `runs/<树>/run_merged/`（或首次全量的 `run/`） | 增量与它合并；**不要手删** |
| 中间态 | `runs/<树>/run_new/`、`run_delta/`、`run_next/` | 跑完自动清理；运行中勿动 |
| 账本 | `runs/_runs.jsonl` | 每轮一行，含 items/delta/verify |
| 日志 | `logs/incremental-aime.log` / `watchdog-aime.log` / `failures_aime.log` | 主循环 / 看门狗 / 失败记录 |
| 缺叶留痕 | `logs/missing_leaf_aime.jsonl` | append-only，记录重问命中与缺叶候选 |
| 交付目录 | `<AIME_ROOT>/cluster/<树>/` | 11 个文件，每轮校验通过后覆盖 |
| 运行环境 | `.deploy/env.sh` + `.deploy/DEPLOYED.json` | 由 `deploy_local.sh` 渲染，勿手改 |

> 本机 `runs/` 仅作历史本地实验存档，生产基线只认服务器上的 `runs/`。

## 5. 自动增量机制

```text
tmux: cluster-incr-aime   bin/channel_daemon.sh aime 7200
        └─ 每 7200s: bin/run_trees.sh --incremental --no-full-fallback
             └─ cases 索引 → 两棵树 ingest/chunk/classify/merge/l4_apply/finish → 交付 → badcase/goldencase

tmux: cluster-watch-aime  bin/watch_daemon.sh aime
        └─ 每 300s 看主循环在不在，被 OOM/异常杀掉就拉起
```

- 无新增时只写一行 `noop`，不重判。
- 并发固定 **4**（托管网关按 key 限流）；请求级 8 轮退避，探活 4 轮重试，单树 ENOMEM 自动重试一次。

**停 / 起（先停看门狗，否则 5 分钟内被拉回来）：**

```bash
tmux kill-session -t cluster-watch-aime
tmux kill-session -t cluster-incr-aime
# 起
tmux new -d -s cluster-incr-aime   "cd $PWD && CLUSTER_INTERVAL=7200 bin/channel_daemon.sh aime 7200 >> logs/incremental-aime.log 2>&1"
tmux new -d -s cluster-watch-aime  "cd $PWD && CLUSTER_INTERVAL=7200 bin/watch_daemon.sh aime >> logs/watchdog-aime.log 2>&1"
```

## 6. 交付物（每棵树 11 件）

| 文件 | 看什么 |
|---|---|
| `cluster_brief.md` | **一页纸结论**（含已下钻 L4 Top 子类），业务先看这个 |
| `cluster_summary.md` | 段/机制/标签/L4 分布，各段 Top 模式 |
| `cluster_tree.json` / `.txt` | 主树；json 给程序，txt 给人 |
| `leaves.tsv` | 叶子明细，可直接进表格 |
| `item_index.jsonl` | 全量索引（seq → path + trace_id + component + label） |
| `syndrome.md` / `.json` | 会话级失效机制共现组合 |
| `cluster_matrix.png` | 环节 × 机制热力图 |
| `MANIFEST.json` / `verify.json` | 版本校验和；`errors=0` 且 `locator_mismatch=0` 才可交付 |

## 7. 三分钟自检

```bash
ssh aime_rsi_data 'cd /mnt/aime_eval_ssd/code/2_cluster_algorithm && source .deploy/env.sh && \
  tmux ls && cat logs/incr_status_aime.json && tail -n 4 runs/_runs.jsonl && bin/preflight.sh'
```

期望：两个 tmux 会话均在；`rc=0`；账本最近两棵树 `err=0,mismatch=0` 或 `noop`；预检 0 FAIL。

## 8. 风险与铁律

- **磁盘**：ceph 剩余空间有限，清理只动 `.archive` / `stress-*`，严禁删 `cluster/` 与 `runs/*/run_merged`。
- **并发与内存**：cgroup 40 GiB，不要把并发调过 4。
- **网关**：换 key 只改本机 `.deploy/local.conf` 再 `./deploy_local.sh aime`。
- **渠道隔离**：不在两个渠道间搬 `runs/`，不在守护进程运行期间手动全量。
