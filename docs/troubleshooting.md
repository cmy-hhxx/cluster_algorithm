# 排错：AIME / 弗吉尼亚

> 先定位，再动手。本文只列会在这台机器上真实发生的失败；部署问题看 [deploy.md](deploy.md)，
> 日常操作看 [runbook.md](runbook.md)。

## 1. 先看这五处

```bash
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh

cat logs/incr_status_aime.json        # 最近一轮 rc / note / 时间
tail -n 5 runs/_runs.jsonl            # 哪棵树、items/delta/verify
tail -n 60 logs/incremental-aime.log  # 这一轮的完整输出
tail -n 10 logs/failures_aime.log     # 失败只记“退出码 1”，原因在上面日志
cat /mnt/aime_eval_ssd/1_aime/eval_output/cluster/aime_fast/verify.json
```

判定顺序：**预检 → 网关 → 这一片/这棵树 → 整批**。不要一上来重跑。

## 2. 快查表

| 现象 | 最可能原因 | 先做什么 |
|---|---|---|
| 预检有 `FAIL` | 环境没装好（Pillow / 字体 / 路径） | 按 §7 逐项修，别开跑 |
| 预检只有 `BLOCKED` | 网关不通 / 数据没到 | 等外部条件，或按 §3 查网关 |
| 整轮 `rc=1`、两棵树都没交付 | 网关 / cases 索引 / 预检之后的外部故障 | 看日志最后一个 `ERROR` / `Traceback` |
| 只有一棵树没交付 | 该树某片非法 path / 缺叶 / ENOMEM | 看 `logs/tree-<树>.log` 与 `unresolved_*.json` |
| `429 Too Many Requests` | 网关高峰、并发叠加 | 等 5–10 分钟，按并发 2 补跑一次；**不要加并发** |
| `401` / `all gateways failed: 401` | 缺 key 或 key 错 | 改本机 `.deploy/local.conf` 的 `VA_GATEWAY_KEY`，重新部署 |
| `Cannot allocate memory` / `Errno 12` | 页缓存顶满 40 GiB cgroup | 已自动重试一次；仍失败等下一轮，别调高并发 |
| 日志只有 `noop` | 没有新增条目 | 正常，不是故障 |
| 守护不在 | tmux 被杀 / 容器重建 | 按 [runbook.md](runbook.md) §6 重拉 |
| `Connection refused` | SSH 端口映射抖动 | 等几秒重试；见 §6 |
| 图上中文是方框 | 字体没加载 | 检查 `CLUSTER_CHART_FONT` 与字体文件 |
| 预检计数 188 ≠ 入树 101 | 预检扫全根，树只读 `0_daily_eval` | 不是故障，别拿 188 报输入量 |

## 3. 网关

当前网关：`http://10.217.220.51/ai-gateway/v1`（LiteLLM 托管，按 key 限流）。
key 只存在本机 `.deploy/local.conf` 的 `VA_GATEWAY_KEY`，由部署渲染成远端 `EVAL_GATEWAY_API_KEY`。

```bash
# 在服务器上探活（必须带 key）
curl -s -o /dev/null -w '%{http_code}\n' -m 8 \
  -H "Authorization: Bearer $EVAL_GATEWAY_API_KEY" \
  http://10.217.220.51/ai-gateway/v1/models
```

| 返回 | 含义 | 处理 |
|---|---|---|
| `200` | 网关正常 | 查别的（缺叶 / 内存 / 数据） |
| `401` | key 缺失或失效 | 更新 `.deploy/local.conf` → `./deploy_local.sh aime` |
| `429` | 限流 | 等高峰过去；按并发 2 补跑；不要换更高并发 |
| `403` / squid 页面 | 请求走了公司代理 | 确认 `NO_PROXY` 含 `10.217.220.51`，`source .deploy/env.sh` 后重试 |
| 超时 / 000 | 网关或路由故障 | 先确认不是本机网络；再等/上报 |

**429 的处理原则**：4.4.10 已把请求级重试提到 8 轮（退避 2/4/8/16/32/60/60/60s，约 4 分钟）。
仍失败时，用并发 2 重跑那一棵，通常几十秒就过；继续加并发只会把高峰叠得更高。

## 4. 内存（ENOMEM）

这台容器 cgroup **40 GiB**、10 核，eval 服务常驻占一部分。快照时 `memory.current` 已约 36 GB，
`memory.reclaim` 是只读的，容器内无法主动回收页缓存。

```bash
cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max
grep -n "Cannot allocate memory\|Errno 12" logs/incremental-aime.log logs/tree-*.log
```

- `run_trees.sh` 对**单棵树**的 ENOMEM 会自动 `sync` + 等 10s 重跑一次
  （`CLUSTER_ENOMEM_RETRY=0` 可关）。这只救偶发，不救持续不足。
- 仍失败：等下一轮（失败后缓存会回落），或先跑另一棵、把大的留到低峰。
- 不要用“提高并发/多开一棵”来抢时间，也不要删 `run_merged` / `run` 试图省内存。
- 要根治只能让平台加内存；`build_cases_index.py` 在全量重扫时也可能 ENOMEM，
  它会 WARN 后降级为“归因自带 question/session”，**不影响条目和判定**。

## 5. 缺叶 / 非法 path

4.4.4 起**不兜底**：模型给不出本环节合法 path，该片直接失败，整棵树当轮不交付，`rc=1`。
这是刻意的——兜底会把“缺叶”稀释成 `edge`，没人认领。

```bash
ls runs/aime_fast/run_delta/unresolved_*.json runs/aime_expert/run_delta/unresolved_*.json 2>/dev/null
python3 - <<'PY'
import glob, json
for f in glob.glob('runs/*/run_*/unresolved_*.json'):
    for r in json.load(open(f)):
        print(f, r.get('component'), r.get('seq'), r.get('model_path'), r.get('repair'))
PY
```

- `model_path` 是模型坚持要的路径；`repair` 为 `none` 表示带清单重问也没收敛，
  基本可判定为**真缺叶**；`repaired` 表示已修好（该行标 `edge` 进复核）。
- 处理：在 `taxonomy/inventory.json` 补叶 → `taxonomy/leaf_tags.json` 补标签 →
  `bin/export_inventory.py` 重导 → `python3 bin/taxonomy_lint.py` 到 `errors=0` →
  部署 → 只补跑那棵树。不要改 `items.jsonl`，不要把非法 path 手工塞进树。
- 留痕：`logs/missing_leaf_aime.jsonl`（append-only，不随 run 清理）记录
  `repaired|none`、`rejected → chosen`；`run_trees.sh` 每轮会打印新增条数。

## 6. SSH / 连接

```bash
ssh aime_rsi_data          # 10.217.219.2:2473
ssh -O exit aime_rsi_data  # 改了端口/容器后，清掉旧的长连接
```

- 端口 2026-11-17 从 2225 改为 2473；配置在 `~/.ssh/config` 与 `.servers.conf`。
- 偶发 `Connection refused`：等 3–5 秒重试；连续多次再看容器是否在。
- 长任务必须进 tmux；SSH 断线会带走前台进程（`ControlPersist` 只保连接，不保任务）。
- `Permission denied (publickey)`：key 路径/权限不对，按 `.servers.conf` 核对，不要新生成 key 覆盖。

## 7. 环境（Pillow / 字体 / 时区）

| 现象 | 检查 | 处理 |
|---|---|---|
| `No module named 'PIL'` | 没 `source .deploy/env.sh` | `source .deploy/env.sh`；或 `/opt/cluster-venv/bin/pip install pillow` |
| `OSError: cannot open resource` | 字体路径不对 | 确认 `.deploy/fonts/wqy-zenhei.ttc` 存在，`CLUSTER_CHART_FONT` 指向它 |
| 图上中文全是方框 | 字体没被 PIL 加载 | 同上；重跑 `chart` 或整棵树 |
| 日志时间差 8 小时 | tzdata 缺失 / 没 source | `apt-get install -y tzdata`；`TZ=Asia/Shanghai date` 应为 CST |
| 预检第 7 项资源不符 | 容器重建后配额变了 | 以 `nproc` 和 `memory.max` 为准，更新文档/预检提示 |

不要在 ceph 上装 `site-packages`（慢到不可用）；venv 固定放 `/opt/cluster-venv`，重建即丢、重装即可。

## 8. 数据 / 路径 / 基线

- **预检 188 个文件，树里 101 个**：预检 `find` 扫全根，包含 `2_task_eval/`、
  `stress-*`、`0_daily_eval-pre-split-*` 备份；树只读 `0_daily_eval/<model>`。
- `1_lost_user_eval` 已不在新根下；`discover_trees.py` 仍列着它，缺失组自动跳过。
  **不要**为了“文件数对得上”去手工建空目录。
- `AIME_ROOT` 指错会表现为“找不到树”或“0 个 attribution”；只改 `deploy_local.sh` /
  `.deploy/local.conf`，不要手改 `.deploy/env.sh`。
- 增量提示“没有基线，退 3”：说明 `runs/<树>/run_merged` 与 `run` 都不在。确认不是路径错后，
  跑一次全量；**不要**用 `--incremental` 硬跑。
- `verify` 的 `locator_mismatch > 0`：4.4 起 verify 不回放源文件，查的是 sample 的
  `trace_id/session_id` 完整性；先确认 `cases_aime.jsonl` 是完整的，再重跑该树。
- 本机 `runs/aime_*` 是 11-01 的旧实验残留，不要拿它做增量或对照。

## 9. 守护进程

```bash
tmux ls
ps -eo pid,etime,cmd | grep -E 'channel_daemon|watch_daemon' | grep -v grep
tail -n 20 logs/watchdog-aime.log
```

- 主循环不在：按 [runbook.md](runbook.md) §6 重拉；先起主循环，再起看门狗。
- 主循环在但一直失败：看门狗只检查进程存在，不会救“跑一次失败一次”；看 `failures_aime.log`。
- 要停：先 `cluster-watch-aime`，再 `cluster-incr-aime`；顺序反了会被 5 分钟内拉回。
- 容器重建后 tmux 全丢，这是预期行为，不是崩溃。

## 10. 不要做的事

1. 不要用更高并发“压过”429。
2. 不要给非法 path 加兜底或手工改树。
3. 不要删 `runs/<树>/run_merged` / `run`、`cluster/` 交付目录来“重来”。
4. 不要在守护进程跑的时候手动全量。
5. 不要在两个渠道之间搬 `runs/`，也不要在乌兰跑 AIME。
6. 不要把 `.deploy/env.sh`、`.servers.conf`、key 提交到仓库。

修完一个真实故障，在 [changelog.md](changelog.md) 记一行“现象 / 根因 / 改法”，
下次不用重新查。
