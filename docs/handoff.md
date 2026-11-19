# 交接：AIME 的两棵树（弗吉尼亚）

> 快照 **2026-11-19 12:30 CST**｜版本 **4.4.10**｜容器 `interactive-s3ynzadcvm9c`
> 写给接手的人：先看这一份。命令可直接复制；数字会随每 2 小时一轮变化，**结构和路径不变**。
> 核对当前数字的方法见 §7；遇到异常先看 [troubleshooting.md](troubleshooting.md)。

## 1. 一分钟结论

AIME 的聚类只在**弗吉尼亚一台机器**上跑，当前只有**两棵树**：

| 树 | 是什么 | 条目 | 会话 | 叶子 | 最近成功一轮 | 校验 |
|---|---|---:|---:|---:|---|---|
| `aime_fast` | 快速模型 | **333,811** | 59,347 | 173 | 2026-11-19 11:50 | `err=0 / mismatch=0` |
| `aime_expert` | 专家模型 | **95,124** | 17,260 | 171 | 2026-11-19 11:44 | `err=0 / mismatch=0` |

- 两棵树都由 tmux 守护进程每 **2 小时**自动增量（跑完再睡 7200s），最近状态 `rc=0`，
  下一轮约 **13:50**。
- 代码在服务器 `/mnt/aime_eval_ssd/code/2_cluster_algorithm`，数据在 `/mnt/aime_eval_ssd`。
- 交付物在 `<数据根>/cluster/<树>/`，共 11 个文件；业务先看 `cluster_brief.md`。
- 本机只负责改代码和 `./deploy_local.sh aime`；**不要在本机跑生产**。

## 2. 两棵树是什么

**树 = 一个模型的全量归因**，不是按日期拆的；日常与流失都归到同一个 model 名下，树名就是
model 名。当前输入只剩 `0_daily_eval/`，旧组 `1_lost_user_eval/` 已不在新根下；
2026-11-11 起身份统一为 `trace_id`。

| 项 | `aime_fast` | `aime_expert` |
|---|---|---|
| 输入目录 | `0_daily_eval/aime_fast/` | `0_daily_eval/aime_expert/` |
| 输入文件 | 51 个 `*attribution*.jsonl`（2026-09-01 → 11-18） | 50 个（同区间） |
| 条目 / 会话 | 333,811 / 59,347 | 95,124 / 17,260 |
| 实际用到叶子 | 173 | 171 |
| 边界标记 `edge` | 5,894（1.8%） | 2,630（2.8%） |
| 当前最大问题（按会话） | 取数查询照抄原句（26,553） | 生成物不可执行（4,641） |
| 第 2 / 第 3 位 | SQL 执行报错（19,323）/ SQL 字段被静默丢弃（15,343） | 请求结构不合契约（4,014）/ 渲染 spec 不合契约（3,835） |

**几个容易误会的点：**

- **“只有两棵树”不等于“只有两个目录”**：`eval_output` 下还有 `2_task_eval/`（流失回测，10 个
  attribution）、`stress-*`（压测）、`0_daily_eval-pre-split-20261111T164410Z/`（拆分前备份）。
  树发现只读 `AIME_GROUPS = ('0_daily_eval', '1_lost_user_eval')`，这些都不进树。
- `1_lost_user_eval/` 在当前新根下**已不存在**（2026-11-11 规整后统一到 `0_daily_eval/`）；
  `discover_trees.py` 里仍列着它，缺失的组会自动跳过，属无害的历史残留。
- **叶子 173 / 171 是“这棵树实际用到多少”**，不是分类法总数。分类法当前 **181 叶**；
  没被用到的叶子不表示不存在。
- 预检会报“188 个 attribution 文件”，那是**全根 find**（含备份、回测、压测），
  真正入树是 **101 个**（51 + 50）。报数时别拿 188 当输入量。

## 3. 怎么连服务器

```bash
ssh aime_rsi_data          # 别名已在 ~/.ssh/config 配好
```

| 项 | 值 |
|---|---|
| SSH 别名 | `aime_rsi_data` |
| 主机 / 端口 | `10.217.219.2` / **2473**（容器 22 的映射） |
| 用户 / 认证 | `root`，密钥认证 |
| 容器 | `interactive-s3ynzadcvm9c`（hostname 带 pod 后缀） |
| 代码目录 | `/mnt/aime_eval_ssd/code/2_cluster_algorithm` |
| 数据根 | `/mnt/aime_eval_ssd/1_aime/eval_output` |

- 主机、端口、密钥路径、端口映射的**唯一来源**是仓库根的 `.servers.conf`（本机文件，已 gitignore；
  只有路径、不含私钥内容；服务器上可能有历史副本，以本机为准）。换电脑时按它重建 `~/.ssh/config`，
  不要把密钥写进仓库。
- 2026-11-17 容器重建过：SSH 端口 **2225 → 2473**，容器名从 `interactive-npasm9ch66eb`
  变为 `interactive-s3ynzadcvm9c`。旧文档里的旧名字/旧端口已失效。
- SSH 偶发 `Connection refused`，等几秒重试即可；长任务必须放 tmux，断线不会带走进程。
- 本机的 `ControlMaster` 会复用连接 8 小时；如果改了端口/容器，先 `ssh -O exit aime_rsi_data`。

**连上后先看这三样：**

```bash
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
tmux ls                                   # cluster-incr-aime / cluster-watch-aime
cat logs/incr_status_aime.json            # 最近一轮 rc / 时间
tail -n 5 runs/_runs.jsonl                # 两棵树的账本
```

## 4. 文件和代码各自在哪

### 4.1 一张图

```text
本机（唯一源码，git 仓库）
/Users/cmy/Desktop/cluster_algorithm/
├── README.md                 唯一入口
├── docs/                     所有项目文档（本文在这）
├── bin/ taxonomy/ prompts/   代码与分类法
├── deploy_local.sh           本机 → 服务器
├── .servers.conf             SSH / 端口（gitignore）
└── .deploy/local.conf        网关 key（gitignore）

服务器（生产）
/mnt/aime_eval_ssd/
├── 1_aime/
│   ├── eval_output/                    AIME_ROOT
│   │   ├── 0_daily_eval/aime_fast/     51 个归因文件
│   │   ├── 0_daily_eval/aime_expert/   50 个归因文件
│   │   ├── 2_task_eval/                回测，不入树
│   │   └── cluster/{aime_fast,aime_expert}/   交付 11 件
│   └── eval_input/                     身份 / 问句（trace 粒度）
├── cases_aime.jsonl                    身份索引
├── badcase_aime.jsonl / goldencase_aime.jsonl
└── code/2_cluster_algorithm/
    ├── runs/                           基线 / 账本 / staging
    ├── logs/                           运行日志
    └── .deploy/                        env.sh + 部署指纹 + 字体
```

### 4.2 逐项位置

| 内容 | 位置 | 说明 |
|---|---|---|
| 代码（本机） | `/Users/cmy/Desktop/cluster_algorithm` | 唯一源码，`main` 分支 |
| 代码（服务器） | `/mnt/aime_eval_ssd/code/2_cluster_algorithm` | rsync 上来的，**没有 `.git`** |
| 归因输入 | `<AIME_ROOT>/0_daily_eval/<树>/*attribution*.jsonl` | 树发现只扫这一层 |
| 身份/问句 | `/mnt/aime_eval_ssd/1_aime/eval_input/` | `eval_input`，一行一 trace |
| staging | `runs/_src/<树>/{daily,lost}/` | 软链；`lost/` 目前为空 |
| 当前基线 | `runs/<树>/run_merged/`（跑过增量）或 `runs/<树>/run/`（只跑过全量） | 增量与它合并；**不要手删** |
| 中间态 | `runs/<树>/run_new/`、`run_delta/`、`run_next/` | 跑完自动清理；有进程在跑时别动 |
| 账本 | `runs/_runs.jsonl` | 每轮一行，含 items/delta/verify |
| 运行日志 | `logs/incremental-aime.log` | 主循环输出 |
| 看门狗日志 | `logs/watchdog-aime.log` | 拉起记录 |
| 失败记录 | `logs/failures_aime.log` | 只在失败时追加 |
| 单树日志 | `logs/tree-<树>.log` | ENOMEM 等单树失败时看 |
| 缺叶留痕 | `logs/missing_leaf_aime.jsonl` | append-only，不随 run 清理 |
| 交付 | `<AIME_ROOT>/cluster/<树>/` | 11 个文件，每轮覆盖 |
| 身份索引 | `/mnt/aime_eval_ssd/cases_aime.jsonl` | 约 155k case |
| badcase | `/mnt/aime_eval_ssd/badcase_aime.jsonl` | P0/P1，26,443 case |
| goldencase | `/mnt/aime_eval_ssd/goldencase_aime.jsonl` | 无 problem，17,398 case |
| 运行环境 | `.deploy/env.sh` | 由 `deploy_local.sh` 渲染，**不要手改** |
| 部署指纹 | `.deploy/DEPLOYED.json` | 版本 / 代码 SHA / 数据根 |
| 中文图字体 | `.deploy/fonts/wqy-zenhei.ttc` | 缺了图上全是方框 |

> 本机也有 `runs/aime_fast`、`runs/aime_expert`，那是 **2026-11-01 的旧实验残留**，
> 不是当前基线。生产基线只认服务器上的 `runs/`。

### 4.3 代码版本

- 版本 **4.4.10**，分类法 **181 叶**（12 环节 × 7 机制，矩阵 67/84 有叶、17 格登记不适用）。
- 服务器没有 `.git`，版本凭证是 `.deploy/DEPLOYED.json` 的 `code_sha256`：
  `e2e286f4…`，与本机 `bin/ taxonomy/ prompts/` 的指纹一致。
- `src_commit` 字段只是部署时的 HEAD 快照（`a21e6b5`），**以指纹为准**。
- 改完代码：本机 `./deploy_local.sh aime`，看远端预检通过即可；不要登录服务器手改文件。

## 5. 它是怎么自动跑的

```text
tmux: cluster-incr-aime   bin/channel_daemon.sh aime 7200
        └─ 每 7200s: bin/run_trees.sh --incremental --no-full-fallback
             └─ cases 索引 → 两棵树 ingest/chunk/classify/merge/finish → 交付 → badcase/goldencase

tmux: cluster-watch-aime  bin/watch_daemon.sh
        └─ 每 300s 看主循环在不在，被 OOM/异常杀掉就拉起
```

- 当前两棵树的增量耗时：`aime_fast` 约 5–6 分钟（本轮 delta 4,748），`aime_expert` 约 1 分钟。
- 无新增时只写一行 `noop`，不重判。
- 并发固定 **4**（托管网关按 key 限流，调高只会更容易撞 429）。
- 失败不会自动重试整轮（ENOMEM 例外：单树自动重试一次）；下一轮再补。
- 最近一次失败是 2026-11-18 21:08 的 429 风暴，4.4.10 把请求重试提到 8 轮后未再复发。

**停 / 起（先停看门狗，否则 5 分钟内被拉回来）：**

```bash
tmux kill-session -t cluster-watch-aime
tmux kill-session -t cluster-incr-aime
# 起
tmux new -d -s cluster-incr-aime   "cd $PWD && CLUSTER_INTERVAL=7200 bin/channel_daemon.sh aime 7200 >> logs/incremental-aime.log 2>&1"
tmux new -d -s cluster-watch-aime  "cd $PWD && CLUSTER_INTERVAL=7200 bin/watch_daemon.sh aime >> logs/watchdog-aime.log 2>&1"
```

## 6. 交付物

每棵树 **11 个文件**（`cluster_tree` 与 `syndrome` 各算 2 个），全部在 `<AIME_ROOT>/cluster/<树>/`：

| 文件 | 看什么 |
|---|---|
| `cluster_brief.md` | **一页纸结论**，业务先看这个 |
| `cluster_summary.md` | 段/机制/标签分布，各段 Top 模式 |
| `cluster_tree.json` / `.txt` | 主树；json 给程序，txt 给人 |
| `leaves.tsv` | 叶子明细，可直接进表格 |
| `item_index.jsonl` | 全量索引（seq → path + trace_id） |
| `syndrome.md` / `.json` | 会话级组合（哪些机制总一起出现） |
| `cluster_matrix.png` | 环节 × 机制热力图 |
| `MANIFEST.json` | 版本、规模、校验和 |
| `verify.json` | `errors=0`、`locator_mismatch=0` 才可交付 |

badcase / goldencase 不在交付目录，在索引根（见 §4.2）。它们按 `trace_id` 回 `eval_input`
就能还原完整 messages；P2–P4 不进这两份清单。

## 7. 三分钟自检

```bash
ssh aime_rsi_data 'cd /mnt/aime_eval_ssd/code/2_cluster_algorithm && source .deploy/env.sh && \
  tmux ls && cat logs/incr_status_aime.json && tail -n 4 runs/_runs.jsonl'
```

期望：两个 tmux 会话都在；status 的 `rc=0`；账本最后四行是 fast/expert 交替、verify 为
`err=0,mismatch=0` 或 `noop`。

```bash
ssh aime_rsi_data 'cd /mnt/aime_eval_ssd/1_aime/eval_output/cluster && \
  for m in aime_fast aime_expert; do echo "== $m"; cat $m/verify.json; done'
```

期望：两棵树 `errors=0`、`locator_mismatch=0`，`total` 与 §1 同量级。

```bash
ssh aime_rsi_data 'cd /mnt/aime_eval_ssd/code/2_cluster_algorithm && source .deploy/env.sh && bin/preflight.sh'
```

期望：8 项里 0 FAIL / 0 BLOCKED（第 8 项“不是 git 仓库”是正常的 WARN）。

## 8. 风险与待办

| 事项 | 现状 | 建议 |
|---|---|---|
| **ceph 容量** | 222T 用了 93%，剩 17T；`.archive` 15G | 优先清理 `eval_output/0_daily_eval/.archive` 与 `stress-*`，别动 `cluster/` 和基线 |
| **内存** | cgroup 40 GiB，快照时约 36 GB；10 核 | 别再调高并发；大轮次可能 ENOMEM，已有单树自动重试 |
| **网关** | `10.217.220.51/ai-gateway/v1`，key 只在本机 `.deploy/local.conf` | 换 key 只改 local.conf 再部署；429 时降并发补跑，不要硬顶 |
| **回测数据未入树** | `2_task_eval/` 10 个文件被 `AIME_GROUPS` 排除 | 产品若要，先改 `discover_trees.py` 的组和 staging，再验证树名不重复 |
| **历史叶子不回溯** | 新增叶子只对以后条目生效 | 需要重判才用 `--rebuild`，会重新调模型，代价大 |
| **本机基线是旧的** | 本机 `runs/` 停在 11-01 | 不要用它做增量；以服务器 `run_merged` / `run` 为准 |
| **服务器无 git** | 只有代码指纹 | 改代码必须走 `deploy_local.sh`，不要 scp 单文件 |

## 9. 交接清单

接手需要：`.servers.conf`（SSH）、`.deploy/local.conf`（网关 key）、本机仓库写权限。

建议阅读顺序：**本文 → [runbook.md](runbook.md) → [deploy.md](deploy.md) → [methodology.md](methodology.md)**。

第一天的三个动作：

1. 按 §7 跑一遍自检，确认两棵树 `err=0`；
2. 打开两棵树的 `cluster_brief.md`，看当前最大单点；
3. 检查 `logs/failures_aime.log` 最后一条是否已处理。

不要做：在两个渠道之间搬 `runs/`；手改 `.deploy/env.sh`；在守护进程跑的时候手动全量；
把并发调到 4 以上“试试”；删除 `runs/<树>/run_merged` / `run`。

本文只写“现在是什么样”。改动后请更新快照日期，并在 [changelog.md](changelog.md) 记一行。
