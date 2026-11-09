# 部署与运行手册（双机：乌兰 = 问财 / 弗吉尼亚 = AIME）

**运行位置（以后固定，不再逐次讨论）**：AIME 的聚类只在**弗吉尼亚**（`aime_rsi_data`）跑，问财的聚类只在**乌兰察布**（`eval_data`）跑；两台机器各跑各的渠道，不交叉、不合并，也不把两个渠道塞到同一台机器上（数据根、网关、存储、配额都是按渠道分开的）。

**服务器信息在哪**：两台机器的 host、SSH 端口、容器名、对外端口映射、用户与密钥路径，全部记在仓库根的 **`.servers.conf`**。该文件只在本机存在（已 gitignore，不随代码 rsync 下发，仓库里没有副本）；本文档只写部署相关的差异项，连接细节以 `.servers.conf` 为准。

两台机器各跑一个渠道、各自只写自己的数据根：

```
                  本机（Mac，唯一中转）
                /                        \
      ./deploy_local.sh wencai     ./deploy_local.sh aime
                ↓                          ↓
      乌兰察布 eval_data          弗吉尼亚 aime_rsi_data
      117.50.195.94:2834          10.217.219.2:2225
      interactive-i6piqwvlnsr5    interactive-npasm9ch66eb
      问财渠道（4 + 6 棵树）       AIME 渠道（2 棵树，日常 + 流失合并）
      网关：两个内网 vLLM 通        网关：10.99.116.223:8888 通
      数据：ceph eval_output       数据：SSD /mnt/aime_eval_ssd/1_aime/eval_output
      8 核 / 16 GiB               6 核 / 64 GiB
```

三条硬事实决定了下面几乎所有做法：

1. **代码不走 git 下发**：乌兰能到内网仓库，弗吉尼亚到内网完全不通（117.50.195.94 全端口 timeout），所以两台统一由本机 rsync 上去，版本一致性靠 `.deploy/DEPLOYED.json` 里的代码指纹。
2. **两台机器之间不互通**：乌兰 → 10.217.219.2 不通，弗吉尼亚 → 117.50.195.94 不通。跨机器的任何动作（传代码、搬基线）都必须经过本机。
3. **两台机器的环境不一样**：乌兰是"旧容器同款 ceph 存储换了个入口"（代码/基线/产物都还在），弗吉尼亚是全新容器（从零装）。资源配额也不同，并发必须分开定。

---

## 0. 两台机器的差异对照

连接细节（host / SSH 端口 / 容器名 / 密钥）见 `.servers.conf`；下表只列与本仓库部署相关的差异项。

| 项 | 乌兰（问财） | 弗吉尼亚（AIME） |
|---|---|---|
| 渠道 `CLUSTER_CHANNEL` | `wencai` | `aime` |
| SSH（`~/.ssh/config`） | `eval_data` | `aime_rsi_data` |
| 容器名 | `interactive-i6piqwvlnsr5` | `interactive-npasm9ch66eb` |
| 代码目录 | `/mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm` | `/mnt/fin_bench/aime_rsi_data/code/2_cluster_algorithm` |
| 数据根 `*_ROOT` | `/mnt/fin_bench/code_and_clean_data/datasets/0_wencai/eval_output` | **`/mnt/aime_eval_ssd/1_aime/eval_output`**（2026-11-09 起，SSD） |
| 索引目录 `CASE_INDEX_ROOT` | `/mnt/fin_bench/code_and_clean_data/datasets` | **`/mnt/aime_eval_ssd`**（`bad_case_index_aime.jsonl` 落在 `1_aime` 的上一级） |
| Python venv | 仓库内 `.venv`（历史遗留，能用就不动） | `/opt/cluster-venv`（本地盘，ceph 上装 site-packages 极慢） |
| 网关 `CLUSTER_GATEWAYS` | `http://interactive-djahh4fczrhq:5001/v1,http://interactive-s4lvy0mmvu32:5001/v1` | `http://10.99.116.223:8888/v1`（vLLM `deepseek-v4.1-flash`，2026-11-09 开通） |
| 并发 `CLUSTER_CONCURRENCY` | 12 | 8 |
| 容器配额 | 8 核 / 16 GiB / 无 swap | 6 核 / 64 GiB / 无 swap |
| 存储 | ceph-fuse，与旧容器**同一份**（同一 inode） | ceph-fuse 挂到 `/mnt/aime_eval_ssd`（2026-11-09 从 `/mnt/fin_bench/aime_rsi_data` 迁来，旧目录只读保留） |
| 公网 | pypi 不通（DNS 只给 IPv6），tuna/aliyun 通 | pypi / aliyun / github 都通 |
| 部署 | `./deploy_local.sh wencai` | `./deploy_local.sh aime` |

### 0.1 弗吉尼亚存储布局（2026-11-09 起：数据在 SSD 挂载）

AIME 的**输入和输出整体迁到 `/mnt/aime_eval_ssd`**，相对结构与旧位置完全一致（`1_aime/eval_output/...`），所以代码只认根、不认前缀：

| 用途 | 新路径（SSD） | 旧路径（`/mnt/fin_bench/aime_rsi_data`，只读保留，勿再写入） |
|---|---|---|
| 输入根 `AIME_ROOT` | `/mnt/aime_eval_ssd/1_aime/eval_output` | `.../aime_rsi_data/1_aime/eval_output` |
| 交付 `cluster/<model>/` | `/mnt/aime_eval_ssd/1_aime/eval_output/cluster/<model>/` | 同左（旧树上还有一份 2026-11-09 前的副本） |
| bad_case 索引 | `/mnt/aime_eval_ssd/bad_case_index_aime.jsonl` | `.../aime_rsi_data/bad_case_index_aime.jsonl` |
| 代码目录（**未迁**） | `/mnt/fin_bench/aime_rsi_data/code/2_cluster_algorithm` | 同左（仍用旧 mount；以后要搬时只改 `VA_REPO`） |

迁移不影响换机器：`bin/discover_trees.py` 只扫 `AIME_ROOT` 下的 `0_daily_eval/` 与 `1_lost_user_eval/`，`cluster/` 输出永远写回 `<AIME_ROOT>/cluster/`。**改路径只改 `deploy_local.sh` 的 `VA_ROOT` / `VA_INDEX` 默认值**（见 §5.3），不要手改 `.deploy/env.sh`。

---

## 1. 渠道、树与数据清单

树**不再写死在代码里**，由 `bin/discover_trees.py` 扫目录自动发现，新增 model 自动进树。

| 渠道 | 树名 | 输入 | 交付 |
|---|---|---|---|
| 问财 | `wencai_<model>` | `<WENCAI_ROOT>/0_daily_eval/<model>/` | `<组目录>/cluster/<model>/` |
| 问财 | `wencai_<model>` | `<WENCAI_ROOT>/1_task_eval/0_search_eval/<model>/` | 同上（在 `0_search_eval/cluster/`） |
| AIME | `<model>`（如 `aime_fast`） | `<AIME_ROOT>/{0_daily_eval,1_lost_user_eval}/<model>/` 合成一棵 | `<AIME_ROOT>/cluster/<model>/` |

扫描规则：`<根>` 下一层的每个目录当一个「组」，组里直接含 `*attribution*.jsonl` 的子目录当一个 model；`0_daily_eval` 这类一层组和 `1_task_eval/0_search_eval` 这类两层组都支持。跳过 `.runs` / `.delivery` / `.archive` / `cluster` / `comparison` / `criteria` 等非归因目录。

**乌兰（问财）实况**

| 组 | 树 | attribution | 现状 |
|---|---|---|---|
| `0_daily_eval` | `a_fast` / `a_expert` / `b_fast` / `b_expert` | 851 / 817 / 815 / 816 行，2026-10-01 单日 | 已有 `cluster/`：1393 / 2077 / 1702 / 2727 条，verify 全 0 错 |
| `1_task_eval/0_search_eval` | 6 个 `aa-*` search model | 208–340 行，2026-10-30 | **零产物**（2026-11-03 才纳入树表） |

`comparison/`（7 组两两模型对比）和 `criteria/`（判据表）不是归因数据，不参与聚类。

**弗吉尼亚（AIME）实况（2026-11-09 20:05 快照，数据已迁到 SSD）**：`AIME_ROOT=/mnt/aime_eval_ssd/1_aime/eval_output`，`0_daily_eval`（`aime_fast` / `aime_expert` 各 20 个 attribution）与 `1_lost_user_eval`（fast 5 / expert 16），**真正入树 61 个文件**；`2_task_eval/`（`0_lost_user_back_test`，fast 5 + expert 5）也在，但 AIME 的组名写死在 `bin/discover_trees.py` 的 `AIME_GROUPS = ('0_daily_eval', '1_lost_user_eval')`，**这两个目录不会被读**；预检第 4 项用 `find` 扫全树计数（SSD 根下 71 个 = 入树 61 + `2_task_eval` 10），比真正入树的 61 个多，别拿它当输入量；`0_daily_eval_atomic-shards/` 在迁移后的新根下已不存在（2026-11-09 20:00 实测）。`cluster/` 下现有 `aime_fast`（127,041 条 / 188 叶子）与 `aime_expert`（45,614 条 / 178 叶子）的 12 件交付，是**从旧布局（乌兰 `sync_datasets` / `code_and_clean_data`）拷来的历史产物**：`item_index.jsonl` 的 `file` 与 `src_map.json` 里还是旧机器绝对路径，和当前 61 个文件不是同一批数据；本机 `runs/` 也没有基线（只剩 `_src` 软链）。**这些旧产物不做路径修补**——第一次全量跑会直接覆盖它们；自 2026-11-09 起，交付物里的 `src_file` / `file` 一律存相对路径（`daily/xxx.jsonl`），绝对路径只出现在 `src_map.json` 的映射值和 `src_root` 里（见 §12）。

**本仓库不搬数据、不解包**。数据到齐后：预检第 4 项的 `BLOCKED` 会自动变成 `OK`；树发现会自动把 lost 那一路并进同一棵树（AIME 的树名 = model 名，`0_daily_eval` 和 `1_lost_user_eval` 合成一棵），不需要改任何配置。

**bad_case 索引**：`bin/build_case_index.py --channel <渠道> --out <CASE_INDEX_ROOT>`，一台机器只建自己渠道那一份，不会产出空文件。字段与生成逻辑见 README §4.0。

---

## 2. 代码怎么上去（不用 git）

```bash
# 本机，仓库根
./deploy_local.sh wencai      # 乌兰：同步代码 + 渲染 env.sh + 远端预检
./deploy_local.sh aime        # 弗吉尼亚
./deploy_local.sh wencai --dry-run     # 只看目标/版本/指纹，不传
./deploy_local.sh aime --no-preflight  # 只同步，不跑预检
```

它做四件事：

1. 远端建 `{runs,logs,.deploy/fonts}`；
2. `rsync -a` 同步代码（排除 `.git` / `runs` / `.venv` / `.deploy` / `__pycache__` / `logs`，**不带 `--delete`**——服务器上的 `.deploy`、`runs` 本地没有，删了就是事故）；
3. 把 `.deploy/env.sh` 按这台机器渲染上去（路径、渠道、网关、并发、`TZ`、`NO_PROXY` 全在这里）；
4. 写 `.deploy/DEPLOYED.json`（渠道 / 主机 / 版本 / **代码指纹** / 时间 / 数据根），并跑一次远端预检。

**怎么确认两台是同一版**：比较两台的 `.deploy/DEPLOYED.json` 里的 `code_sha256`（对 `bin/ taxonomy/ prompts/` 下所有源码文件取哈希）。弗吉尼亚没有 `.git`，这是唯一可靠的版本凭证。

```bash
ssh eval_data      'cat /mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm/.deploy/DEPLOYED.json' | grep code_sha256
ssh aime_rsi_data  'cat /mnt/fin_bench/aime_rsi_data/code/2_cluster_algorithm/.deploy/DEPLOYED.json' | grep code_sha256
```

目标机器/路径可以覆盖：在本机建 `.deploy/local.conf`（已 gitignore，无密钥），变量名见 `deploy_local.sh` 头部注释（`WL_*` / `VA_*`）。

> `deploy.sh`（服务器自己 `git fetch` 后快进）只在能访问内网仓库的机器上还有意义；乌兰现在也走 rsync，两台口径统一。

---

## 3. 跑一次

```bash
# 服务器上（两台都一样）
cd <代码目录>
source .deploy/env.sh
tmux new -s cluster           # 长任务必须进 tmux，SSH 断线不会带走
bin/preflight.sh              # 8 项预检：OK / BLOCKED / FAIL 三种结论
bin/run_trees.sh              # 跑本渠道的全部树（渠道来自 env.sh）
```

预检现在分三种结果，退出码只对 FAIL 敏感：

| 结论 | 含义 | 退出码 |
|---|---|---|
| `OK` | 这一项没问题 | — |
| `BLOCKED` | 外部条件没到（网关没开、数据还没搬来），环境本身是对的 → **不许开跑** | 0 |
| `FAIL` | 环境有问题，必须修 | 1 |

所以"环境部署完成"是可验证的：**有 FAIL 就是没装好；只剩 BLOCKED 就是装好了在等外部条件。**

常用变体：

```bash
bin/run_trees.sh --only wencai_a_fast            # 只跑一棵
bin/run_trees.sh --incremental                   # 增量（只判新增条目）
bin/run_trees.sh --incremental --no-full-fallback # 没基线就报错退 3，绝不悄悄全量
bin/run_trees.sh --dry-run                       # 只 ingest + chunk，不归类、不碰基线
bin/run_trees.sh --concurrency 8                 # 临时降并发
bin/run_trees.sh --channel wencai                # 临时指定渠道（覆盖 env.sh）
```

跑之前 `bin/run_trees.sh` 会自己再查一次网关：**`CLUSTER_GATEWAYS` 为空或一个都不通 → 直接退 3**，不会浪费几分钟跑到 `classify` 才炸。

**基线**：每棵树一个 `runs/<树>/run_merged`（跑过增量）或 `runs/<树>/run`（只跑过全量）。增量只对 `run_delta` 做 chunk+classify，去重键 `(session_id, trace_id, json_path)`，已判过的条目不会重判。

**deliver 约定**：`finish` 产出 12 个文件到 `runs/<树>/out`，再整目录拷进交付目录。`MANIFEST.json` 只登记 9 个标准件；`verify.json` 必须 `errors=0` 且 `locator_mismatch=0`。

---

## 4. 守护进程（**默认不启动**）

两台机器都没有 crontab / systemd（`crontab: command not found`、PID 1 不是 init），所以只能用 tmux 常驻。

```bash
# 启动（在各自机器上，只跑本渠道）
tmux new -d -s cluster-incr-wencai "bin/channel_daemon.sh wencai >> logs/incremental-wencai.log 2>&1"
tmux new -d -s cluster-incr-aime   "bin/channel_daemon.sh aime   >> logs/incremental-aime.log 2>&1"
tmux new -d -s cluster-watch-wencai "bin/watch_daemon.sh wencai >> logs/watchdog-wencai.log 2>&1"
tmux new -d -s cluster-watch-aime   "bin/watch_daemon.sh aime   >> logs/watchdog-aime.log 2>&1"
```

- `bin/channel_daemon.sh <渠道> [间隔秒]`：默认 1800 秒一轮，`--incremental --no-full-fallback`，无新增只写一行 noop；每轮写 `logs/incr_status_<渠道>.json`，失败追加 `logs/failures_<渠道>.log`。**网关为空的机器不刷失败日志**，只写 `blocked-no-gateway`。
- `bin/watch_daemon.sh <渠道> [检查秒]`：每 5 分钟看 `cluster-incr-<渠道>` 在不在，被 OOM 杀掉就拉起。
- **要停先停看门狗**，否则主循环 5 分钟内会被拉回来：

```bash
tmux kill-session -t cluster-watch-wencai; tmux kill-session -t cluster-incr-wencai
tmux kill-session -t cluster-watch-aime;   tmux kill-session -t cluster-incr-aime
```

2026-11-03 起两台都**只装不启动**，等通知再开。

---

## 5. 环境初始化（每台机器只做一次）

乌兰是老环境（`.venv`、字体、`.deploy` 都还在，只做校验，别重装）。弗吉尼亚是全新容器，照下面做。

### 5.1 Python（弗吉尼亚：装本地盘，别装 ceph）

```bash
python3 -m venv /opt/cluster-venv
/opt/cluster-venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ --upgrade pip
/opt/cluster-venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ pillow
/opt/cluster-venv/bin/python -c "import PIL;print(PIL.__version__)"   # 期望 13.0.0
```

只有 `chart` 需要 Pillow（画 `cluster_matrix.png`），其余都是标准库。

### 5.1.1 tzdata（镜像里没有，容器重建必补）

这个 pod 的镜像不带 tzdata：`TZ=Asia/Shanghai` 会静默退化成 UTC（`date` 打出 `... Asia`，时间差 8 小时），日志和 `runs/_runs.jsonl` 的 `at` 全错但不报错。

```bash
apt-get install -y --no-install-recommends tzdata
TZ=Asia/Shanghai date '+%F %T %Z'        # 期望 ... CST
```

2026-11-09 实测：apt 下载可能长时间无输出，中断后跑 `dpkg --configure -a` 收尾，`dpkg -l tzdata` 应为 `ii`（tzdata 2026e）。若 apt 完全不可用，可从本机拷一份：

```bash
scp /usr/share/zoneinfo/Asia/Shanghai aime_rsi_data:/usr/share/zoneinfo/Asia/
```

### 5.2 中文字体（服务器没有 CJK 字体，缺了图上全是方框）

```bash
mkdir -p /tmp/font && cd /tmp/font
apt-get download fonts-wqy-zenhei
dpkg-deb -x fonts-wqy-zenhei_*.deb /tmp/font/x
cp /tmp/font/x/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc <代码目录>/.deploy/fonts/
```

弗吉尼亚已装好：16,791,251 字节，`sha256 79c18ebe7b81…`，实测 `ImageFont.truetype(...,16)` 可加载且字形不是 `.notdef` 方框。

### 5.3 `env.sh` 与渠道根

**不要手写**，用 `./deploy_local.sh <渠道>` 渲染。它等价于：

```bash
export PATH="<venv>/bin:$PATH"
export CLUSTER_CHART_FONT="<代码目录>/.deploy/fonts/wqy-zenhei.ttc"
export TZ=Asia/Shanghai
export CLUSTER_CHANNEL=wencai            # 弗吉尼亚是 aime
export WENCAI_ROOT="<...>/0_wencai/eval_output"   # 另一渠道留空
export AIME_ROOT="/mnt/aime_eval_ssd/1_aime/eval_output"   # 2026-11-09 起在 SSD，结构仍是 1_aime/eval_output
export CASE_INDEX_ROOT="/mnt/aime_eval_ssd"                # bad_case_index_aime.jsonl 落在这一级
export CLUSTER_CONCURRENCY=12            # 弗吉尼亚 8
export CLUSTER_GATEWAYS="<网关，逗号分隔；弗吉尼亚是 http://10.99.116.223:8888/v1>"
export EVAL_GATEWAY_API_KEY=dummy
export NO_PROXY="interactive-s4lvy0mmvu32,interactive-djahh4fczrhq,interactive-n01laf763gwn,10.99.116.223,localhost,127.0.0.1"
```

四个必须项，缺一个就会踩坑：

- `PATH` 必须指到带 Pillow 的 venv —— `preflight.sh` / `run.sh` 调的是裸 `python3`；
- `CLUSTER_CHART_FONT` —— 代码默认值还是 macOS 的 `Hiragino Sans GB.ttc`；
- `TZ=Asia/Shanghai` —— 两台容器默认 UTC，日志/ledger 时间会和本地差 8 小时，两台之间也对不上；
- `NO_PROXY` —— 乌兰有全局 `HTTP_PROXY`（`10.240.24.13:3129`），不排除网关会被 squid 拦成 403。

---

## 6. 重建清单（实例重建 / 换机器时照做）

**乌兰（新容器但共享旧 ceph）**

```bash
# 1) 先确认仓库还在（code_and_clean_data 与旧容器的 sync_datasets 是同一份 ceph，
#    旧容器 interactive-m8jqioclnl 与新容器看到的文件 inode 相同）
ls /mnt/fin_bench/code_and_clean_data/code/2_cluster_algorithm/taxonomy/inventory.json
# 2) 本机推代码 + env.sh + 预检
./deploy_local.sh wencai
# 3) 校验：Pillow 可用、字体存在、两个网关 200
```

原来 `sync_datasets` 下的一切（`runs/` 基线、已交付产物）在新容器里都是**同一份文件**，不用搬。
**旧容器 `interactive-m8jqioclnm20` 已弃用**：它和新乌兰共享同一份 `runs/`，两边同时跑会互相踩锁和复写产物，只在需要考古历史时只读访问。

**弗吉尼亚（全新容器）**

```bash
# 1) 装环境（§5.1 + §5.1.1 + §5.2）
python3 -m venv /opt/cluster-venv && /opt/cluster-venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ pillow
apt-get install -y --no-install-recommends tzdata   # 缺它 TZ 会静默退化成 UTC，见 §5.1.1
# 2) 拉代码（本机执行）
./deploy_local.sh aime
# 3) 数据到位后确认根下有 attribution 文件
find /mnt/aime_eval_ssd/1_aime/eval_output -name '*attribution*.jsonl' | wc -l
```

这台机器：`/opt` 与 `/` 都是本地 overlay（重建即丢，venv 重装）；`/mnt/fin_bench/aime_rsi_data` 是 ceph（重建保留）。

---

## 7. 踩过的坑（现象 / 原因 / 处理）

### 7.1 ceph-fuse 上装 node_modules / site-packages
- **现象**：`npm i` 或 `cp -a node_modules` 在 `/mnt/fin_bench` 下十几分钟写不完。
- **原因**：ceph-fuse 几万个小文件元数据操作极慢，同一份拷贝到本地盘只要 2 秒。
- **处理**：venv / `node_modules` 一律放本地盘（弗吉尼亚 `/opt/cluster-venv`）；ceph 上只放代码、配置、产物。乌兰的老 `.venv` 在 ceph 上但已可用，不折腾。

### 7.2 全局代理把网关请求拦成 403
- **现象**：请求网关报 `proxy.ucwlcc.com (squid) ... ERR_ACCESS_DENIED`。
- **原因**：乌兰有全局 `HTTP_PROXY`。
- **处理**：`.deploy/env.sh` 的 `NO_PROXY` 已含两个网关和 `interactive-n01laf763gwn`；`bin/classify.py` 自己用 `ProxyHandler({})`，双保险。

### 7.3 `python3` 没 Pillow（新机器必踩）
- **现象**：`No module named 'PIL'`，或预检第 6 项 FAIL。
- **原因**：系统 python3 没装 Pillow，而脚本调的是裸 `python3`。
- **处理**：`env.sh` 里 `PATH=<venv>/bin:$PATH`；先 `source .deploy/env.sh` 再跑任何脚本。

### 7.4 图表中文变方框
- **现象**：`cluster_matrix.png` 标题/机制名全是 □。
- **原因**：服务器没有 CJK 字体，代码默认字体路径是 macOS 的。
- **处理**：装 `fonts-wqy-zenhei`，`CLUSTER_CHART_FONT` 指过去；预检第 6 项会真加载一次字体来验。

### 7.5 内存/CPU 口径误判
- **现象**：`free -g` 显示 2 TB，以为能开 32 并发。
- **原因**：容器里的 `free` 是宿主机视角；真实配额在 cgroup（乌兰 16 GiB / 8 核，弗吉尼亚 64 GiB / 6 核）。
- **处理**：看 `/sys/fs/cgroup/{memory/memory.limit_in_bytes,cpu/cpu.cfs_quota_us}`；预检第 7 项会读 cgroup 并在并发超过 2×核数时告警。

### 7.6 沙箱脚本模式登记不全
- **现象**：ingest 报 `unregistered components`。
- **原因**：数据里出现新工具（`.sh` 脚本、`view_skill`、`pptx` 等）没有登记进 `taxonomy/components.json`。
- **处理**：`bin/cluster.py` 会把未登记的 raw component 写进 `<run>/unregistered.json` 并中止——按提示登记，别加 catch-all 桶。2026-11-03 全量扫描已 0 阻塞。

### 7.7 换了数据集路径后 `src_map.json` 溯源断裂
- **现象**：`cluster/<model>/src_map.json` 的 `src_root`/映射值指向一个已经不存在的快照目录；`cluster_tree.json` 里全是旧机器的绝对路径。
- **原因**：旧版 `merge_runs` 会把绝对路径写进基线并带进交付物；数据一换挂载点（2026-11-09 从 `/mnt/fin_bench/aime_rsi_data` 迁到 `/mnt/aime_eval_ssd`）就全断。
- **处理**：
  - 交付物自 2026-11-09 起只存相对路径：`item_index.jsonl.file`、`cluster_tree.json` 的 `src_file` 都是 `daily/xxx.jsonl` 这种「父目录名/文件名」，绝对路径只保留在 `src_map.json`（`src_root` = 源文件公共根 + `files` 映射）里，换机器只改 `AIME_ROOT` 即可。
  - 老基线换机器时仍先跑 `bin/rebase_paths.py --run runs --datasets <新根> --repo <新仓库>`（它会把 `src_path` 重映射、把老 `file` 绝对路径压回相对形式），再增量；每次交付都会重跑 `bin/make_src_map.py`。

### 7.8 继承别台的基线会 `verify` 断链
- **现象**：拿乌兰的 AIME 基线到弗吉尼亚做增量，`verify` 报 `src not found`。
- **原因**：两台的源数据不是同一批（tar 只到 2026-11-02，乌兰基线含 11-03 的条目），基线引用了本机不存在的文件。
- **处理**：**基线必须在数据所在地建**。弗吉尼亚用自己的数据全量建一次基线，之后靠增量跟上。

### 7.9 重跑时复用了旧判定
- **现象**：全量重跑后叶子分布和上次一模一样，快得离谱。
- **原因**：`classify.py` 见到 `assign/<chunk>.out.jsonl` 且 seq 齐全就跳过；旧文件的 seq 恰好对得上。
- **处理**：`run_trees.sh` 全量前 `rm -rf runs/<树>/run`，增量前清 `run_delta`（已内建）。

### 7.10 SSH 断线带走长任务
- **现象**：聚类跑到一半没了。
- **原因**：任务挂在 SSH 会话下。
- **处理**：一律 `tmux new -s cluster`；重连 `tmux a -t cluster`。

### 7.11 一个渠道的守护去动了另一个渠道的数据
- **现象**：乌兰的守护跑到 AIME 树，或反向。
- **原因**：树表以前写死 6 棵、不分渠道。
- **处理**：`CLUSTER_CHANNEL` 限定渠道 + 自动发现；乌兰的 `AIME_ROOT` 留空，`--channel wencai` 就只会发现问财的树。**乌兰不再处理 AIME 数据**，留存的 `1_aime` 与 `bad_case_index_aime.jsonl` 是拆分前副本，只读。

### 7.12 `flock` 不存在导致「另一批正在运行」
- **现象**：本地跑报"另一批聚类正在运行"，其实没有。
- **原因**：macOS 没 `flock`。
- **处理**：脚本检测到没有 `flock` 会跳过锁并提示；服务器有 `flock`，正常生效。

### 7.13 SSH 到弗吉尼亚间歇 `Connection refused`
- **现象**：`ssh aime_rsi_data` 时通时不通（2026-11-09 实测约一半新连接被拒），`rsync`/`scp` 同样受影响；容器本身活着，重试往往就能连上。
- **原因**：`10.217.219.2:2225` 的端口映射/入口对短时间内的多次新连接有丢包，不是鉴权问题。
- **处理**：给这台机器开连接复用，后续命令都走同一条长连接：

  ```bash
  ssh -o ControlMaster=auto -o ControlPath=/tmp/ssh-va-%r@%h:%p -o ControlPersist=8h aime_rsi_data true
  # 之后 ssh/rsync/scp 带上同一组 -o（或把 ssh 包一层 PATH wrapper）即可复用，不再新建连接
  ```

  跑 `./deploy_local.sh aime` 时用一个带上述参数的 `ssh` wrapper 放 PATH 前面，rsync/scp 也走复用。

### 7.14 预检 `find` 全树遍历被 ceph 拖死
- **现象**：`bin/preflight.sh` 第 4 项卡住几分钟不返回（2026-11-09 复验时 >5 分钟），`run_trees.sh` 启动前也会调它。
- **原因**：预检用 `find <渠道根> -name '*attribution*.jsonl'` 扫全树，而 `1_aime/eval_output/0_daily_eval_atomic-shards/` 下有数据搬运工具写的 `.delivery/claims/...` 状态目录（小文件极多），ceph-fuse 上逐个 stat 很慢；这些文件本来就会被后面的 `grep -v` 过滤掉，纯属白扫。
- **处理**：已改成先 `-prune` 掉 `.*` / `cluster` / `comparison` / `criteria` 再数，计数不变（AIME 仍 149），耗时从 >5 分钟降到约 40s。以后再有重型非输入目录，加进同一个 prune 列表。

---

## 8. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `BLOCKED CLUSTER_GATEWAYS 为空` | 该机器没配网关 | 重跑 `./deploy_local.sh <渠道>` 渲染 env.sh（弗吉尼亚网关 2026-11-09 已开通） |
| `BLOCKED ... 没有 attribution 文件` | 数据还没搬到位 | 等数据；或检查 `*_ROOT` 是否指对 |
| `No module named 'PIL'` | 没 source env.sh | `source .deploy/env.sh` |
| `OSError: cannot open resource` | 字体路径不对 | 检查 `CLUSTER_CHART_FONT` 与 `.deploy/fonts/` |
| 预检第 6 项 FAIL | Pillow 或字体 | 见 §5.1 / §5.2 |
| `树发现失败` | 渠道根没设 / 目录不存在 | 看 `env.sh` 的 `WENCAI_ROOT`/`AIME_ROOT` |
| 网关请求 403 / squid 页面 | 走了全局代理 | 确认 `NO_PROXY` 含网关主机名 |
| `Permission denied (publickey)` | SSH key 不对 | 检查 `~/.ssh/config` 里 `eval_data` / `aime_rsi_data` |
| 远端预检退出码 1 | 有 FAIL | 按输出修；BLOCKED 不会让退出码变 1 |
| 两台 `code_sha256` 不同 | 有一台没同步 | 两台各跑一次 `./deploy_local.sh <渠道>` |
| 冒烟卡在 `verify` | `--src` 指到巨大目录 | `tests/smoke.sh` 已固定 `--src tests`，别改回去 |

---

## 9. 每轮操作 checklist

1. 本机 `./deploy_local.sh <渠道>`（有代码改动时）→ 看远端预检结论。
2. 服务器 `cd <代码目录> && source .deploy/env.sh && bin/preflight.sh` → 不允许 FAIL；有 BLOCKED 就别跑。
3. `tmux new -s cluster` → `bin/run_trees.sh`。
4. 看结果：`runs/_runs.jsonl` 最后几行、`runs/<树>/out/verify.json`、交付目录 12 个文件。
5. 索引：`bin/build_case_index.py --channel <渠道> --out $CASE_INDEX_ROOT`（`run_trees.sh` 结束会自动做）。

---

## 10. 部署验收记录（2026-11-03 21:20）

| 验收项 | 乌兰（问财） | 弗吉尼亚（AIME） |
|---|---|---|
| 代码指纹 `code_sha256` | `b9948aa05e9dd9d5…`（commit `b7cee44`） | `b9948aa05e9dd9d5…`（同 commit，一致 ✅） |
| `bin/preflight.sh` | 7 项 OK + 1 WARN，**可以开跑** | 6 项 OK + 1 BLOCKED（网关）+ 1 WARN，**环境就绪、不许开跑** |
| 树发现 | **10 棵**（4 棵 `0_daily_eval` + 6 棵 `1_task_eval/0_search_eval`） | **2 棵**（`aime_fast` / `aime_expert`；lost 未搬，先只 stage daily） |
| 网关 | 两个内网网关 `/v1/models` 都 200 | 空（等开通）；跑 `run_trees` 立刻退 3，不产生任何产物 |
| venv / Pillow | 仓库内 `.venv`，Pillow 13.0.0 | `/opt/cluster-venv`，Pillow 13.0.0 |
| 字体 | `.deploy/fonts/wqy-zenhei.ttc` | 同左，16,791,251 B，`sha256 79c18ebe…` |
| 配额 / 并发 | 8 核 / 16 GiB / 12 并发 | 6 核 / 64 GiB / 8 并发 |
| 守护进程 | 未启动（按约定只装不启动） | 未启动 |
| 已交付产物 | `cluster/` 4 棵树未被本次部署触碰（mtime 仍是 11-02 14:34–14:35） | 无（`cluster/` 未生成） |

## 11. 部署验收记录（2026-11-09 19:31，弗吉尼亚重装 + 网关开通）

> 本节是 19:31 的旧根（`/mnt/fin_bench/aime_rsi_data`）记录；20:00 数据根已迁到 SSD，最新状态见 §12。

本次只重装了代码与环境，**没有跑聚类**（不覆盖已有 `cluster/`）。

| 验收项 | 结果 |
|---|---|
| 代码 | 本机 rsync，commit `b1058fd`，`code_sha256 f7845cde16120abd…`（含 `bin/preflight.sh` 的 find prune 修复，见 §7.14；以 `.deploy/DEPLOYED.json` 为准） |
| `bin/preflight.sh` | 7 项 OK + 1 WARN（非 git 仓库），**0 FAIL / 0 BLOCKED → 可以开跑**；耗时 39s（修复前会卡在 `atomic-shards/.delivery` 上 >5 分钟） |
| Python | `/opt/cluster-venv` 重建：Python 3.12.12 + Pillow 13.0.0（aliyun 源，`/opt` 本地盘） |
| 字体 | `.deploy/fonts/wqy-zenhei.ttc` 在 ceph 上，未重装，预检第 6 项真加载通过 |
| 时区 | pod 缺 tzdata，已装 `tzdata 2026e`（`ii`），`source env.sh` 后 `date` = `… CST` |
| 网关 | `http://10.99.116.223:8888/v1`（vLLM `deepseek-v4.1-flash`）：`/v1/models` 200；`/v1/responses` 200 返回 `pong`；`classify.py` 跑 3 条 fixture 分片 **3/3 成功、0 失败、3 次调用 3.9s** |
| 树发现 | 2 棵：`aime_fast`（25 个 attribution / 36,255 行）、`aime_expert`（36 个 / 11,257 行） |
| 数据根 | `.../1_aime/eval_output`；预检 `find` 计数 149（含 atomic-shards 等不入树的目录），真正入树 61 |
| 已有产物 | `cluster/aime_fast` 127,041 条 / 188 叶子、`cluster/aime_expert` 45,614 条 / 178 叶子，verify 全 0 错；均为 **11-09 04:11 / 05:21 生成**，`item_index.jsonl` 指向拆分前旧布局，非本机当前数据产物 |
| 基线 | `runs/` 无 `run`/`run_merged`，只有陈旧 `_src` 软链 → **第一次跑必须全量**；`--incremental --no-full-fallback` 会退 3 |
| bad_case 索引 | 未生成（`/mnt/fin_bench/aime_rsi_data/bad_case_index_aime.jsonl` 不存在），跑完 `run_trees.sh` 会自动建 |
| 守护进程 | 未启动（按约定只装不启动） |


## 12. 2026-11-09 20:00 迁移记录（AIME 数据 → SSD；交付物改存相对路径）

| 项 | 结果 |
|---|---|
| 数据根 | `AIME_ROOT=/mnt/aime_eval_ssd/1_aime/eval_output`，`CASE_INDEX_ROOT=/mnt/aime_eval_ssd`（相对结构不变）；旧根 `/mnt/fin_bench/aime_rsi_data` 只读保留，勿再写入 |
| 代码/部署 | commit `2e398db`，`code_sha256 ce97575f9860e6c5…`；`./deploy_local.sh aime` 成功（`DEPLOYED.json` 已是新根） |
| 预检 | 7 项 OK + 1 WARN（非 git 仓库），0 FAIL / 0 BLOCKED；网关 `/v1/models` 200 |
| 树发现 | 2 棵：`aime_fast`（25 个 attribution）、`aime_expert`（36 个）；交付目标 `<AIME_ROOT>/cluster/<model>/` |
| 旧 staging | 已清掉 `runs/_src`（旧根软链），下次 `run_trees.sh` 按新根重建 |
| 冒烟 | 服务器 `tests/smoke.sh` OK（verify 0 错）；真实网关跑 3 条 fixture 分片 **3/3 成功、0 失败** |
| 交付物路径 | 自本版起 `item_index.jsonl.file` 与 `cluster_tree.json` 的 `src_file` 只存相对路径（`daily/xxx.jsonl`、`lost/xxx.jsonl`）；`src_map.json` 的 `src_root` = 源文件真实公共根（AIME 即 `<AIME_ROOT>`），`files` 值为 realpath；换机器只改 `AIME_ROOT` |
| 老基线 | `rebase_paths.py` 会把老 `file` 的绝对路径压回相对形式、重映射 `src_path` |
| 已有 `cluster/` | 仍是旧布局拷来的历史产物（旧绝对路径、数据批次不同），**本次未改**；第一次全量跑会覆盖，跑前确认可以覆盖 |

改动点（对应 commit）：`deploy_local.sh`（新根默认值 + 用 `ssh cat >` 写 env/fingerprint，绕开 scp 间歇断连）、`bin/merge_runs.py`（`file` 固定相对）、`bin/make_src_map.py`（`src_root` 改真实公共根）、`bin/rebase_paths.py`（老基线归一化）、`bin/preflight.sh`（find prune）、`run_cluster.sh`（src 根校验）、`DEPLOY.md`/`README.md`/`RUNBOOK.md`。

> 未迁项：代码目录仍在 `/mnt/fin_bench/aime_rsi_data/code/2_cluster_algorithm`（旧 mount）。要搬到 `/mnt/aime_eval_ssd/code/2_cluster_algorithm` 只改 `VA_REPO`（那边已有一份 11-03 的旧副本，部署会覆盖）。
