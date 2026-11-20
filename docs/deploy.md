# 部署：双机（弗吉尼亚 = AIME / 乌兰 = 问财）

> 目标：把仓库代码变成一台机器上可跑的聚类环境。
> 日常怎么跑看 [runbook.md](runbook.md)，出事怎么查看 [troubleshooting.md](troubleshooting.md)。

## 1. 拓扑与三条硬约束

```text
                    本机 Mac（唯一源码 + 唯一中转）
                  /                              \
     ./deploy_local.sh aime            ./deploy_local.sh wencai
                ↓                                  ↓
      弗吉尼亚 aime_rsi_data              乌兰察布 eval_data
      10.217.219.2:2473                   117.50.195.94:2834
      容器 interactive-s3ynzadcvm9c        容器 interactive-i6piqwvlnsr5
      AIME 渠道：2 棵树                   问财渠道：4 + 6 棵树
      数据 /mnt/aime_eval_ssd             数据 ceph eval_output
      10 核 / 40 GiB / 并发 4             8 核 / 16 GiB / 并发 12
```

1. **代码不走 git 下发**：弗吉尼亚到内网 git 完全不通，两台统一由本机 rsync 上去；
   版本一致性靠 `.deploy/DEPLOYED.json` 的 `code_sha256`。
2. **两台机器互不通**：跨机器的动作（传代码、搬基线）都必须经过本机。
3. **两台环境不同**：乌兰是旧 ceph 环境，弗吉尼亚是 2026-11-17 重建的新容器；
   路径、venv、网关、并发都要分开，不能照抄。

> 连接细节（host / SSH 端口 / 容器名 / 密钥路径 / 端口映射）只认仓库根 **`.servers.conf`**。
> 该文件已 gitignore、不入库（只有路径，不含私钥内容）；rsync 目前未排除它，
> 服务器上可能有一份副本，以本机这份为准。

## 2. 当前参数（2026-11-19）

| 项 | 弗吉尼亚（AIME） | 乌兰（问财） |
|---|---|---|
| 渠道 `CLUSTER_CHANNEL` | `aime` | `wencai` |
| SSH 别名 | `aime_rsi_data` | `eval_data` |
| 容器 | `interactive-s3ynzadcvm9c` | `interactive-i6piqwvlnsr5` |
| 代码目录 | `/mnt/aime_eval_ssd/code/2_cluster_algorithm` | `/mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm` |
| 数据根 `*_ROOT` | `/mnt/aime_eval_ssd/1_aime/eval_output` | `/mnt/fin_bench/code_and_clean_data/datasets/0_wencai/eval_output` |
| 身份输入 `*_CASES_ROOT` | `/mnt/aime_eval_ssd/1_aime/eval_input` | `<...>/0_wencai/eval_input` |
| 索引根 `CASE_INDEX_ROOT` | `/mnt/aime_eval_ssd` | `/mnt/fin_bench/code_and_clean_data/datasets` |
| Python venv | `/opt/cluster-venv`（本地盘） | 仓库内 `.venv`（历史遗留） |
| 网关 | `http://10.217.220.51/ai-gateway/v1`（要 key） | 两个内网 vLLM |
| 并发 | **4** | 12 |
| 配额 | 10 核 / 40 GiB | 8 核 / 16 GiB |
| 存储 | ceph 挂 `/mnt/aime_eval_ssd` | ceph-fuse |
| 部署命令 | `./deploy_local.sh aime` | `./deploy_local.sh wencai` |

弗吉尼亚的 SSH 端口 2026-11-17 从 **2225 改为 2473**，容器从 `interactive-npasm9ch66eb`
重建为 `interactive-s3ynzadcvm9c`；旧名字只出现在历史记录里。

## 3. 弗吉尼亚存储布局

AIME 的输入、输出、代码、基线**全部**在 `/mnt/aime_eval_ssd`，相对结构固定：

```text
/mnt/aime_eval_ssd/
├── 1_aime/
│   ├── eval_output/                 AIME_ROOT
│   │   ├── 0_daily_eval/<model>/    归因输入（当前只此一个组）
│   │   ├── 2_task_eval/             流失回测，树发现不读
│   │   └── cluster/<model>/         交付（每轮覆盖）
│   └── eval_input/                  trace 粒度身份 + 问句
├── cases_aime.jsonl                 身份索引
├── badcase_aime.jsonl               P0/P1
├── goldencase_aime.jsonl            无 problem
└── code/2_cluster_algorithm/        代码 + runs/ + logs/ + .deploy/
```

- 旧根 `/mnt/fin_bench/aime_rsi_data` 的 `1_aime` 与旧仓副本**已删除**；不要再去那里找数据。
- 输出永远写回 `<AIME_ROOT>/cluster/<model>/`；换挂载点只改 `deploy_local.sh` 的
  `VA_ROOT` / `VA_INDEX`（或 `.deploy/local.conf`），**不要手改 `env.sh`**。
- `0_daily_eval/.archive/` 是 11-11 规整前的备份（约 15G），不被扫描，可另行清理。

## 4. 代码怎么上去

```bash
# 本机，仓库根
./deploy_local.sh aime                 # 同步代码 + 渲染 env.sh + 写指纹 + 远端预检
./deploy_local.sh aime --dry-run       # 只打印目标 / 版本 / 指纹
./deploy_local.sh aime --no-preflight  # 只同步，不跑预检
./deploy_local.sh aime --no-code       # 只渲染 env + 指纹
```

它按顺序做五件事：

1. 远端建 `runs/`、`logs/`、`.deploy/fonts/`；
2. `rsync -a` 同步代码，排除 `.git` / `runs` / `.venv` / `.deploy` / `__pycache__` / `logs`；
   **不带 `--delete`**——服务器上的 `runs/`、`.deploy/` 本地没有，删了就是事故；
3. 按这台机器渲染 `.deploy/env.sh`（路径、渠道、网关、并发、`TZ`、`NO_PROXY`）；
4. 写 `.deploy/DEPLOYED.json`（渠道 / 主机 / 版本 / 代码指纹 / 时间 / 数据根）；
5. 远端跑一次 `bin/preflight.sh`。

**确认本地与服务器同版**：比较 `code_sha256`（对 `bin/ taxonomy/ prompts/` 下源码取哈希）。
弗吉尼亚没有 `.git`，这是唯一可靠的版本凭证。

```bash
ssh aime_rsi_data 'cat /mnt/aime_eval_ssd/code/2_cluster_algorithm/.deploy/DEPLOYED.json'
ssh eval_data 'cat /mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm/.deploy/DEPLOYED.json'
```

> `docs/` 不参与代码指纹。服务器上的旧 `.md` 是历史副本，文档以本机 `docs/` 为准。

## 5. 首次 / 重建后的环境

容器重建会丢本地盘（`/opt`、`/`），ceph（`/mnt/aime_eval_ssd`）保留。按顺序补：

```bash
# 1) venv（只有 chart 需要 Pillow，其余标准库）
python3 -m venv /opt/cluster-venv
/opt/cluster-venv/bin/pip install -q -i https://mirrors.aliyun.com/pypi/simple/ --upgrade pip
/opt/cluster-venv/bin/pip install -q -i https://mirrors.aliyun.com/pypi/simple/ pillow
/opt/cluster-venv/bin/python -c "import PIL;print(PIL.__version__)"     # 期望 13.0.0

# 2) tzdata（镜像里没有；缺了 TZ=Asia/Shanghai 静默退化成 UTC）
apt-get install -y --no-install-recommends tzdata
TZ=Asia/Shanghai date '+%F %T %Z'                                      # 期望 CST

# 3) 中文字体（缺了 cluster_matrix.png 上全是方框）
#    已有 .deploy/fonts/wqy-zenhei.ttc 就跳过；没有则：
mkdir -p /tmp/font && cd /tmp/font
apt-get download fonts-wqy-zenhei
dpkg-deb -x fonts-wqy-zenhei_*.deb /tmp/font/x
cp /tmp/font/x/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc <代码目录>/.deploy/fonts/
ls -l <代码目录>/.deploy/fonts/wqy-zenhei.ttc

# 4) 部署 + 预检
./deploy_local.sh aime
```

`env.sh` 的四个必须项：`PATH` 指到 venv、`CLUSTER_CHART_FONT` 指到字体、
`TZ=Asia/Shanghai`、`NO_PROXY` 含网关地址。任何一项缺了都会以“看起来像代码 bug”的方式失败。

## 6. 守护进程

服务器没有 crontab / systemd，只能用 tmux：

```bash
# 主循环：每 2 小时一轮增量
tmux new -d -s cluster-incr-aime \
  "cd $PWD && CLUSTER_INTERVAL=7200 bin/channel_daemon.sh aime 7200 >> logs/incremental-aime.log 2>&1"

# 看门狗：每 5 分钟检查，主循环被 OOM 杀掉就拉起
tmux new -d -s cluster-watch-aime \
  "cd $PWD && CLUSTER_INTERVAL=7200 bin/watch_daemon.sh aime >> logs/watchdog-aime.log 2>&1"
```

- `channel_daemon.sh <渠道> [间隔秒]`：跑 `run_trees.sh --incremental --no-full-fallback`；
  无新增只写 `noop`；每轮写 `logs/incr_status_<渠道>.json`，失败追加 `logs/failures_<渠道>.log`。
- `watch_daemon.sh <渠道>`：只看 `cluster-incr-<渠道>` 在不在，不判断任务成败。
- **要停先停看门狗**，否则主循环 5 分钟内被拉回来：

```bash
tmux kill-session -t cluster-watch-aime
tmux kill-session -t cluster-incr-aime
```

- 容器重建后 tmux 全部消失，必须重拉；乌兰按约定只装不启动。

## 7. 重建 / 换机器清单

**弗吉尼亚（新容器）**

```bash
# 1) 确认 ceph 还在
ls /mnt/aime_eval_ssd/1_aime/eval_output/cluster
ls /mnt/aime_eval_ssd/code/2_cluster_algorithm/runs/<树>/{run_merged,run}

# 2) 按 §5 装 venv / tzdata / 字体
# 3) 本机部署
./deploy_local.sh aime

# 4) 预检 + 手动补跑一轮（确认能交付）
ssh aime_rsi_data 'cd /mnt/aime_eval_ssd/code/2_cluster_algorithm && source .deploy/env.sh && bin/preflight.sh && bin/run_trees.sh --incremental --no-full-fallback'

# 5) 起 tmux（见 §6）
```

**乌兰（新容器但共享旧 ceph）**

```bash
ls /mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm/taxonomy/inventory.json
./deploy_local.sh wencai
# 校验 Pillow / 字体 / 两个网关 200
```

旧乌兰容器已弃用；它和新乌兰共享同一份 `runs/`，同时跑会互相踩锁。

## 8. 密钥与配置

| 内容 | 放在哪 | 是否入库 |
|---|---|---|
| SSH host / 端口 / 密钥路径 | `.servers.conf`（仓库根） | 否，gitignore |
| 网关 API key | `.deploy/local.conf` 的 `VA_GATEWAY_KEY` | 否，gitignore |
| 渲染后的运行环境 | 服务器 `.deploy/env.sh` | 否 |
| 版本 / 代码指纹 | 服务器 `.deploy/DEPLOYED.json` | 服务器本地 |
| 分类法与版本 | `VERSION.json` / `taxonomy/` | 是 |

- 不要打印 key、不要把 `env.sh` 提交。
- 换网关：改 `deploy_local.sh` 默认值或 `.deploy/local.conf`，再 `./deploy_local.sh aime`；
  代码里不写死网关。
- `.servers.conf` 里的容器名/端口若与 `ssh aime_rsi_data hostname` 不一致，先改配置再排障。

## 9. 验收

`bin/preflight.sh` 三种结论：`OK` / `BLOCKED`（等外部条件，不许开跑）/ `FAIL`（必须修）。
**有 FAIL 就是没装好；只剩 BLOCKED 就是装好了在等外部条件。**

2026-11-19 的实测基线：

```text
[1/8] taxonomy: leaves=181 errors=0 warnings=28
[2/8] VERSION 4.4.10
[3/8] .deploy/env.sh 存在且可 source
[4/8] AIME 数据根 / 输入根 / 索引目录 均 OK
[5/8] http://10.217.220.51/ai-gateway/v1/models -> 200
[6/8] 字体 OK
[7/8] 容器配额 10 核 / 40 GiB，当前并发 4
[8/8] WARN 不是 git 仓库（正常）
预检通过，可以开跑
```

首次部署完不要直接开守护：先手动 `bin/run_trees.sh --incremental --no-full-fallback`
看两棵树都能交付，再按 §6 起 tmux。

## 10. session_id 兜底（4.4.11）

上游回补（归因文件里 `fill_source=session_fill`、`fetched_by=v2only` 那批）会把缺失的
`session_id` 写成字符串 `"None"`。这种记录原样进树，`verify` 会判
`item_index 缺 trace_id/session_id` 并让整棵树 rc=1；因为失败树不推进基线，下一轮
又会重新摄入同一批坏数据 → **同一棵树会永久卡住**（2026-11-20 12:44 aime_expert，119 条）。

现在 `ingest` 里 `''` / `"None"` / `"null"` / `"nan"` / `"n/a"` / `"-"` 都视为缺失，
退用 `trace_id` 作为 `session_id`：

- 条目不丢、聚类标签不受影响；代价是这些 trace 各自算一个会话，会话数会略高。
- `stats.json` 记 `session_filled=N`，日志打一行 `WARN session_id 缺失 N 条`；
  账本 `_runs.jsonl` 仅在 N>0 时写 `session_filled`，便于回查是哪几轮补的。
- `verify` 规则不变：兜底后 `trace_id` 与 `session_id` 仍必须非空，真缺 `trace_id` 照样失败。

想确认上游又灌了多少坏数据：`grep -c '"session_id": "None"' <归因文件>`，
再对照账本最近的 `session_filled`。

## 11. 清理上游判定的脏数据（4.4.12）

上游把某批记录判为脏数据、并从归因/输入里清掉后，我们要把这批从树里删掉。**只删不重判**：
条目的分类结果原样保留，删完重新聚合交付。

```bash
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm && source .deploy/env.sh
# 1) 先看要删多少（不动盘）
python3 bin/prune_items.py --work runs/<树>/run_merged --self-session --dry-run
# 2) 真删：items / locators / assign 三处同步删，自动备份 + 留痕
python3 bin/prune_items.py --work runs/<树>/run_merged --self-session --reason "上游脏数据 <日期>"
#    也可以按清单删：--drop-ids /tmp/dirty_case_ids.txt
# 3) 重新聚合、校验、出报告（不调用模型）
./run.sh finish runs/<树>/run_merged runs/<树>/out 0 runs/_src/<树>
# 4) 交付 + 重建 badcase/goldencase
cp runs/<树>/out/* "$AIME_ROOT/cluster/<树>"/
python3 bin/build_case_lists.py --channel aime --attr-root "$AIME_ROOT" --out "$CASE_INDEX_ROOT" --cases-root "$CASES_INDEX"
```

- `--self-session` 选的是 `session_id == trace_id` 的条目，即 ingest 对上游 `session_id=null`
  做兜底留下的痕迹；上游把数据清掉后，这批就是脏数据（2026-11-20 用这条清了 expert 119 条 / 33 个 case）。
- 备份在 `runs/<树>/run_merged/.prune-<ts>/`（含原 items/locators/assign），留痕在
  `runs/<树>/run_merged/prune.jsonl`。确认无问题后备份可删。
- `prune_items.py` 会断言 `assign` 行数 == `items` 行数；不一致直接退出，不会留下半删状态。
- 身份索引（`cases_<渠道>.jsonl`）不跟着删：它按 TTL 重建，脏 trace 只是查不到新条目，
  不影响已有条目。要立刻反映可在下一轮 `--rebuild` 前重建索引。
