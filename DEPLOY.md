# 部署与运行手册

这套东西有**两种跑法**，分类法、提示词、交付物完全一致，只有「归类那一步由谁执行」不同：

```
① 服务器（正式口径）
   本地改代码 → git push 内网仓库 → 服务器 ./deploy.sh 拉取
   → run_cluster.sh：init → classify（bin/classify.py 直调网关，24–32 并发）→ finish

② 本地（2026-11-01 这几轮的实际做法，见 §4.0）
   run.sh init → 归类改由 DSH 子代理直判（本机连不上 interactive-*:5001 网关）→ run.sh finish
```

服务器只拉取、只运行，**不要在服务器上改代码**（`deploy.sh` 会因工作区有改动直接停下）。
本地能跑只是为了出数与联调，口径必须与服务器一致：同一份 `taxonomy/`、同一份 `prompts/`。
**切回服务器时第一件事是核对分类法版本**（`python3 bin/taxonomy_lint.py` 必须 `errors=0`），原因见 §4.0。

---

## 1. 环境事实

| 项 | 值 |
|---|---|
| 服务器 | `117.50.195.94:3929`，root，key 认证（见 `.servers.conf`） |
| 代码目录 | `/mnt/fin_bench/sync_datasets/code/2_cluster_algorithm` |
| 数据目录 | `/mnt/fin_bench/sync_datasets/datasets` |
| 仓库 | `ssh://git@interactive-n01laf763gwn:22/chenmingyang2/cluster_algorithm.git`（Forgejo） |
| 规格 | 16 核 / 约 2TB 内存（cgroup 上限 120GiB）/ 无 swap |
| 内核 | 5.4（**没有 Landlock**），容器内**没有 `CAP_SYS_ADMIN`** |
| 存储 | `sync_datasets` 是 **ceph-fuse** 网络盘；`/`、`/opt`、`/tmp` 是本地盘 |
| 运行时 | node 24.14 / npm 11 / python 3.12 / tmux |
| 代理 | 全局 `HTTP_PROXY`/`HTTPS_PROXY` 指向公司 squid |

后四条决定了下面几乎所有的"为什么"。**ceph-fuse 慢、没有 CAP_SYS_ADMIN、有全局代理**——这三件事各踩过一次坑。

---

## 2. 目录布局

| 路径 | 用途 | 入库 |
|---|---|---|
| `.`（仓库根） | main 分支 checkout | 是 |
| `skill/SKILL.md` | skill 定义（DSH 从这里读） | 是 |
| `run_cluster.sh` | 服务器上跑一次聚类的入口 | 是 |
| `deploy.sh` | 服务器拉取 + 冒烟 | 是 |
| `.deploy/git-readonly(.pub)` | 仓库级**只读**部署密钥 | 否 |
| `.deploy/git-known-hosts` | 固定主机指纹 | 否 |
| `.deploy/fonts/wqy-zenhei.ttc` | 中文字体（服务器没有 CJK 字体） | 否 |
| `.deploy/env.sh` | PATH / 字体 / DSH_HOME / NO_PROXY | 否 |
| `.deploy/dsh-home/` | DSH 配置与会话（`DSH_HOME`） | 否 |
| `.venv/` | Python 3.12 + Pillow | 否 |
| `runs/` | 聚类产物 | 否 |
| `/opt/dsh/` | DSH 安装（**本地盘**） | 否 |
| `~/.agents/skills/attribution-cluster` | 指向 `skill/` 的软链 | 否 |

---

## 3. 日常开发与发布

```bash
# 本地
git commit -am "..." && git push origin main

# 服务器
ssh cluster_agent_server 'cd /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm && ./deploy.sh'
```

`deploy.sh` 做三件事：`git fetch` → `git merge --ff-only` → 跑 `tests/smoke.sh`。只接受快进合并；服务器和远端一旦分叉就停下，不会自动 merge。

冒烟通过的标准输出：

```
leaves=3 total=3 locator_mismatch=0 errors=0
SMOKE OK
updated to <sha>
```

---

## 4. 在服务器上跑聚类

### 4.0 现状：先在本地跑通了，正式要上服务器

**这批为什么在本地跑**

- 本机连不上归类网关：`interactive-djahh4fczrhq:5001` 解析不到（NXDOMAIN），经 `127.0.0.1:7897` 代理也是 502，`bin/classify.py` 不可用；
- 本机没有 `.deploy/env.sh`，`run_cluster.sh` 在 `source` 那一步就失败；
- 于是本地改成：`run.sh init` → DSH 子代理按 `prompts/01_assign.md` + `taxonomy/INV_S<x>.md` 逐片归类（并发 8–12）→ `run.sh finish`。

**本地实测（2026-11-01，v4.1 12 段口径）**

| 批次 | 条目 | 分片 | 耗时（每批 12 片并行） |
|---|---|---|---|
| 6 棵树全量 | 2,640 | 75 | ≈20 min（7 批） |
| 6 棵树增量 | 1,136 | 54 | ≈10 min（5 批） |

服务器 `classify.py` 同样的量约 1 分钟（实测 4,700 条/分）。**差的只是速度，不是口径**——分类法、提示词、交付物、计数口径完全一致。

**上服务器前逐项确认**

| 项 | 怎么查 | 通过标准 |
|---|---|---|
| 分类法版本 | `python3 bin/taxonomy_lint.py` | `errors=0`，且 `VERSION.json` 是 4.1.1（12 段 / 171 叶） |
| 网关可达 | `curl -s -o /dev/null -w '%{http_code}\n' $CLUSTER_GATEWAYS/models` | 200 |
| `.deploy/env.sh` | `ls .deploy/env.sh` | 存在且能 `source`（本地缺的就是这个） |
| API key | `echo $EVAL_GATEWAY_API_KEY` | 内网网关默认 `dummy` 可用 |
| 中文字体 | `fc-list :lang=zh` 或设 `CLUSTER_CHART_FONT` | chart 用 PIL 画，缺字体图上是方框 |
| 并发 | `CLUSTER_CONCURRENCY` | 24–32，别超 32 |

**一键跑（推荐）**

```bash
cd /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm
tmux new -s cluster
bin/preflight.sh /mnt/fin_bench/sync_datasets/datasets        # 7 项预检，任一 FAIL 就别开跑
bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets        # 6 棵树全量
bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets --incremental   # 新数据并入既有 6 棵树
bin/run_trees.sh /mnt/fin_bench/sync_datasets/datasets --only aime_expert   # 只跑一棵
```

`bin/run_trees.sh` 会把 AIME 的「日常 + 流失」软链到 `runs/_src/<树>/{daily,lost}/`（`ingest` 只吃单个目录），逐棵跑完 `init → classify → finish → make_src_map`，再把 12 个文件拷进 `cluster/<model>/`。

**手工分步（排错用）**

```bash
./run_cluster.sh <输入 jsonl 或目录> <run_dir> <out_dir> [全量会话数] [cases 目录]
```

**增量（不要全量重跑）**

```bash
python3 bin/cluster.py ingest --input <数据目录> --out runs/<树>/run_new
python3 bin/make_delta.py --old runs/<树>/run_merged --new runs/<树>/run_new --out runs/<树>/run_delta
python3 bin/cluster.py chunk --work runs/<树>/run_delta --size 60
./run.sh classify runs/<树>/run_delta --concurrency 24
python3 bin/merge_runs.py --out runs/<树>/run_next runs/<树>/run_merged runs/<树>/run_delta
./run.sh finish runs/<树>/run_next <out_dir> 0 <src_root>
mv runs/<树>/run_next runs/<树>/run_merged          # 基线前移，下次增量从这里比
```

- **基线**是 `runs/<树>/run_merged`（跑过增量后）或 `runs/<树>/run`（只跑过全量），每棵树只有一个。
- 去重键 `(session_id, trace_id, json_path)`：已判过的条目**不会重判**，只判 `run_delta` 里的新增条目；重复记录被丢弃，数量写进 `run_delta/stats.json` 的 `already_known`。
- 关键在只对 `run_delta` 跑 `chunk` + `classify`，整棵树不参与。`--dry-run` 一律写 `runs/<树>/run_dry`，不会碰基线。

**换机器后的一个坑**：`merge_runs` 会把 `file` / `src_path` 统一写成本机绝对路径，所以 `item_index.jsonl` 里的 `src_file` 在跑批机器上可直接打开、换机器后失效。每次交付都要重跑 `bin/make_src_map.py` 生成 `src_map.json`，它是"相对路径 ↔ 原始文件"的唯一索引。

### 4.1 跑一次

```bash
cd /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm
tmux new -s cluster          # 长任务必须放 tmux，否则 SSH 断线就没了
./run_cluster.sh <输入 jsonl 或目录> <run_dir> <out_dir> [全量会话数] [cases 目录]
```

`run_cluster.sh` 默认走 **script 引擎**：`init` → `classify`（直调网关，`CLUSTER_REASONING` 选 none/low，默认 none）→ `finish`。只有 `CLUSTER_ENGINE=dsh` 时才用 `dsh --profile headless --json`，事件流写到 `<out_dir>/dsh-<时间>.jsonl`。

看进度和结果：

```bash
tail -f <out_dir>/dsh-*.jsonl                                  # 实时事件流
grep '"type":"final"' <out_dir>/dsh-*.jsonl | tail -1          # 最终答复
grep -o 'leaves=[0-9]* total=[0-9]* locator_mismatch=[0-9]* errors=[0-9]*' <out_dir>/dsh-*.jsonl | tail -1
```

交付物是 12 个文件：9 个标准件（`cluster_tree.json/txt`、`cluster_summary.md`、`cluster_brief.md`、`cluster_matrix.png`、`item_index.jsonl`、`syndrome.json/md`、`leaves.tsv`、`MANIFEST.json`）加 `verify.json`、`src_map.json`。`MANIFEST.json` 只登记 9 个标准件的校验和。`verify` 必须 `errors=0` 且 `locator_mismatch=0`。

> 时间预期：script 引擎快速模式实测 6,243 条 / 79s（32 并发，约 4,700 条/分），35,000 条约 8 分钟；质量模式约慢 5–6 倍。

### 4.2 DSH 配置

| 项 | 值 |
|---|---|
| 版本 | `@deepseek-ai/dsh@0.2.1-rc.4`，装在 `/opt/dsh` |
| DSH_HOME | `.deploy/dsh-home` |
| profile | `$DSH_HOME/profiles/headless/cordis.patch.yml` |
| 模型 | `eval-gw1`（默认）/ `eval-gw2`，`deepseek-v4.1-flash` |
| 协议 | `openai-responses` |
| 容量 | `contextWindow: 524288`、`maxTokens: 32768` |
| 子代理 | `maxActiveSubagents: 32`（DSH 默认 8） |
| 权限 | 沙箱 `workspace-write` + `approval: never` |
| 凭据 | `$DSH_HOME/.env` 里 `EVAL_GATEWAY_API_KEY=dummy`（网关不校验 key，随便填） |

profile patch 全文（丢了就照这个重建）：

```yaml
- id: llm-pi-ai
  config:
    providers:
      eval-gw1:
        apiKeyEnv: EVAL_GATEWAY_API_KEY
        api: openai-responses
        baseURL: http://interactive-s4lvy0mmvu32:5001/v1/
        models:
          - id: deepseek-v4.1-flash
            name: DeepSeek V4.1 Flash
            contextWindow: 524288
            maxTokens: 32768
      eval-gw2:
        apiKeyEnv: EVAL_GATEWAY_API_KEY
        api: openai-responses
        baseURL: http://interactive-djahh4fczrhq:5001/v1/
        models:
          - id: deepseek-v4.1-flash
            name: DeepSeek V4.1 Flash (备用)
            contextWindow: 524288
            maxTokens: 32768

- id: agent-default-model
  config:
    provider: eval-gw1
    model: deepseek-v4.1-flash

- id: subagent
  config:
    maxActiveSubagents: 32

- id: approval
  config:
    policy: never
- id: permission
  config:
    defaultPreset: workspace-write-auto
    presets:
      read-only: {sandbox: read-only, approval: ask}
      workspace-write: {sandbox: workspace-write, approval: ask}
      workspace-write-auto: {sandbox: workspace-write, approval: never}
      danger-full-access: {sandbox: danger-full-access, approval: never}
```

换默认网关：改 `agent-default-model.provider` 为 `eval-gw2` 即可，下一次请求生效。

### 4.3 skill 怎么被加载

DSH 扫 `~/.agents/skills`。服务器上：

```bash
ln -sfn /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm/skill ~/.agents/skills/attribution-cluster
```

`SKILL.md` 里写的是"仓库 = 本文件所在目录的上一级"，所以必须通过这个软链，仓库挪了位置也不用改 skill。

验证：

```bash
cd /mnt/fin_bench/sync_datasets && source code/2_cluster_algorithm/.deploy/env.sh
dsh --profile headless "只回答：你当前可用的 skill 名称列表"
# 应输出 attribution-cluster
```

---

## 5. 踩过的坑（现象 / 原因 / 处理）

### 5.1 ceph-fuse 上装 node_modules

- **现象**：`npm i @deepseek-ai/dsh` 或 `cp -a node_modules` 在 `/mnt/fin_bench` 下十几分钟写不完。
- **原因**：`sync_datasets` 是 ceph-fuse，几万个小文件的元数据操作极慢；同一份拷贝到本地盘只要 2 秒。
- **处理**：DSH 装 `/opt/dsh`，只在 ceph 上放体积小的配置（`DSH_HOME`）。**任何 `node_modules`、`site-packages` 都不要放 `/mnt/fin_bench`。**

### 5.2 全局代理把网关请求拦成 403

- **现象**：headless 报错，正文是 `proxy.ucwlcc.com (squid) ... ERR_ACCESS_DENIED`。
- **原因**：服务器全局有 `HTTP_PROXY`，请求 `interactive-*` 走了 squid。
- **处理**：`.deploy/env.sh` 里已设 `NO_PROXY`（含两个网关和 `interactive-n01laf763gwn`）。手工跑 DSH 前记得 `source .deploy/env.sh`。

### 5.3 沙箱后端不可用，所有 bash 调用被拒

- **现象**：agent 一步都跑不了，报
  `sandbox mode "workspace-write" is requested but no sandbox backend is usable on this host`。
- **原因**：Linux 上 `workspace-write` 走 bubblewrap；这台容器内核 5.4（无 Landlock）且没有 `CAP_SYS_ADMIN`，bwrap 直接跑会 `Creating new namespace failed: Operation not permitted`。
- **处理**：装 bubblewrap + 放一个 shim（补 `--unshare-user`、去掉 `--proc`，因为 seccomp 拒绝在 user namespace 里挂 proc）：

```bash
apt-get install -y bubblewrap
cat > /usr/local/bin/bwrap <<'EOF'
#!/usr/bin/env bash
# 容器没有 CAP_SYS_ADMIN：bwrap 必须自建 user namespace，且 seccomp 拒绝在其中挂 /proc
args=(); skip=0
for a in "$@"; do
  if (( skip )); then skip=0; continue; fi
  if [[ $a == --proc ]]; then skip=1; continue; fi
  args+=("$a")
done
exec /usr/bin/bwrap --unshare-user "${args[@]}"
EOF
chmod 755 /usr/local/bin/bwrap
```

验证：`bwrap --ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent -- true`。

退路是改成 `danger-full-access`（无沙箱、root 全盘可写），不推荐。

### 5.4 rc.4 的 headless 没有 `--permission-mode` / `--approval-policy`

- **现象**：`error: unknown option '--permission-mode'`。
- **原因**：官网文档跟的是 master，rc.4 的 headless 只有 `--json` 和 `--session-id`；权限只能通过 profile patch 配。
- **处理**：见 §4.2 里的 `approval` / `permission` 两段。升级到带这两个参数的版本后再改回命令行传参。

### 5.5 `permission: composed sandbox and approval defaults match no preset`

- **现象**：启动打一行 warning，`permission` 插件没激活（但任务还能跑）。
- **原因**：`sandbox=workspace-write` + `approval=never` 这个组合不在预设表里，且没指定 `defaultPreset`。
- **处理**：`permission.defaultPreset: workspace-write-auto`，并在 `presets` 里补上这个组合（见 §4.2）。

### 5.6 DSH 看不到 skill

- **现象**：问 agent 有哪些 skill，答不出来。
- **原因**：`~/.agents/skills/attribution-cluster` 没建、软链断了，或 `SKILL.md` 不在目标目录里。
- **处理**：`ls -l ~/.agents/skills/attribution-cluster/` 应能看到 `SKILL.md`；软链目标必须是仓库里的 `skill/` 目录。

### 5.7 冒烟测试在服务器上卡死

- **现象**：`deploy.sh` 卡在 `cluster.py verify` 不动。
- **原因**：`tests/smoke.sh` 原来用 `--src "$ROOT/.."`，`verify` 找不到文件时会递归 glob 整个父目录；在 Mac 上父目录是 Desktop，在服务器上是整个 `code/`（含 0_frontend），直接扫爆。
- **处理**：已改成 `--src "$ROOT/tests"`（`eeef87f`），直接命中，不触发 glob。

### 5.8 归类自检永远误报 path 非法

- **现象**：子代理跑 `prompts/01_assign.md` §8 的自检，9 个 INV 文件全部 0 命中。
- **原因**：正则 `^\| ({{L1}}\.[A-Z0-9.]+) \|` 没考虑 INV 表首列的反引号（实际是 `` | `S3.M3.2` | ``）。
- **处理**：已改成 `` ^\| `({{L1}}\.[A-Z0-9.]+)` \| ``（`2aaddc8`）。

### 5.9 部署密钥 push 被拒

- **现象**：服务器上 `git push` 报权限错误。
- **原因**：`.deploy/git-readonly` 是**只读**部署密钥，这是预期行为。
- **处理**：不用管；服务器只拉不推。

### 5.10 SSH 断线把长任务带走

- **现象**：聚类跑到一半没了。
- **原因**：任务挂在 SSH 会话下。
- **处理**：`tmux new -s cluster` 里跑；重连用 `tmux a -t cluster`。事件流已经落盘，断了也能从日志判断进度。

### 5.11 实例重建后 `/opt/dsh` 消失

- **现象**：`dsh: command not found`。
- **原因**：`/opt` 是容器本地盘，重建即丢；ceph 上的 `.deploy/` 不会丢。
- **处理**：按 §6 重建。

---

## 6. 重建清单（实例重建 / 换机器时照做）

```bash
cd /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm

# 1) Python 运行时
python3 -m venv .venv && .venv/bin/pip install pillow

# 2) 中文字体（chart 需要）
mkdir -p .deploy/fonts
apt-get download fonts-wqy-zenhei && dpkg-deb -x fonts-wqy-zenhei_*.deb /tmp/wqy
cp /tmp/wqy/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc .deploy/fonts/

# 3) DSH（本地盘）
mkdir -p /opt/dsh && cd /opt/dsh && npm i --no-audit --no-fund @deepseek-ai/dsh@0.2.1-rc.4

# 4) 沙箱
apt-get install -y bubblewrap   # 再按 §5.3 写 /usr/local/bin/bwrap

# 5) 环境变量
#    .deploy/env.sh 应包含：
#    PATH（.venv + /opt/dsh/node_modules/.bin）、CLUSTER_CHART_FONT、
#    DSH_HOME=$D/.deploy/dsh-home、NO_PROXY（两个网关 + interactive-n01laf763gwn）

# 6) DSH 配置
#    $DSH_HOME/profiles/headless/cordis.patch.yml（见 §4.2）
#    $DSH_HOME/.env: EVAL_GATEWAY_API_KEY=dummy

# 7) skill
mkdir -p ~/.agents/skills
ln -sfn "$PWD/skill" ~/.agents/skills/attribution-cluster

# 8) 代码与部署密钥（.deploy 还在就跳过）
git init -q
git remote add origin ssh://git@interactive-n01laf763gwn:22/chenmingyang2/cluster_algorithm.git
git config core.sshCommand "ssh -F /dev/null -i $PWD/.deploy/git-readonly -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=$PWD/.deploy/git-known-hosts -o StrictHostKeyChecking=yes"
git fetch origin main && git checkout -f -B main origin/main
```

`.deploy/git-readonly` 丢了就重新生成，把 `.pub` 加到 Forgejo 仓库 `Settings → Deploy keys`（只读）：

```bash
ssh-keygen -q -t ed25519 -N "" -C "ths_cluster_agent readonly" -f .deploy/git-readonly
ssh-keyscan -p 22 interactive-n01laf763gwn > .deploy/git-known-hosts
```

---

## 7. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `Permission denied (publickey)` | 部署公钥没加 / 加错仓库 | 重新加 deploy key |
| `Host key verification failed` | `git-known-hosts` 缺失或主机指纹变了 | `ssh-keyscan -p 22 interactive-n01laf763gwn > .deploy/git-known-hosts` |
| `merge --ff-only` 失败 | 服务器和远端分叉 | 确认服务器没改代码后 `git reset --hard origin/main` |
| `No module named 'PIL'` | 没加载 venv | `source .deploy/env.sh` |
| `OSError: cannot open resource` | 字体路径不存在 | 检查 `CLUSTER_CHART_FONT` 和 `.deploy/fonts/` |
| 网关请求 403 / squid 页面 | 走了全局代理 | 确认 `NO_PROXY` 含网关主机名 |
| `no sandbox backend is usable` | bwrap 没装或 shim 没生效 | 见 §5.3 |
| `unknown option '--permission-mode'` | rc.4 不支持该参数 | 用 profile patch 配，见 §5.4 |
| DSH 答不出 skill | 软链缺失/断开 | 见 §5.6 |
| 冒烟卡在 `verify` | `--src` 指向了巨大目录 | 已在仓库里修好，`./deploy.sh` 拉最新即可 |
