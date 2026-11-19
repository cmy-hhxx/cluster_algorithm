# 运行手册（AIME / 弗吉尼亚）

> 收到「跑一下 AIME」时按本文执行。部署与换容器看 [deploy.md](deploy.md)，
> 异常排查看 [troubleshooting.md](troubleshooting.md)，现状与路径看 [handoff.md](handoff.md)。

## 0. 固定前提

| 项 | 值 |
|---|---|
| 渠道 | `aime`（由服务器 `.deploy/env.sh` 固定） |
| 机器 | 弗吉尼亚 `aime_rsi_data` |
| 代码目录 | `/mnt/aime_eval_ssd/code/2_cluster_algorithm` |
| 数据根 | `/mnt/aime_eval_ssd/1_aime/eval_output` |
| 树 | `aime_fast` / `aime_expert`（自动发现，不用手填） |
| 并发 | **4** |
| 分片 | 全量 200 条/片；增量 60 条/片（都按环节分组） |
| 自动节奏 | 每 7200 秒一轮增量（守护进程） |

**不要在本机跑生产**：本机连不到内网网关；本机只改代码，然后 `./deploy_local.sh aime`。

## 1. 开始前

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh          # 必须；渠道根 / 网关 / PATH / TZ 都在里面
tmux new -s manual             # 长任务必须进 tmux
bin/preflight.sh               # 8 项：OK / BLOCKED / FAIL
```

- 有 **FAIL**：先修，不许开跑。
- 只有 **BLOCKED**：环境没问题，在等外部条件（网关 / 数据），也不许开跑。
- 全 OK：可以开跑。

如果守护进程正在跑一轮，**不要手动再开**：`runs/.lock` 会拦住，或两边互相踩基线。
要手动，先按 §6 停掉主循环和看门狗。

## 2. 跑法

### 2.1 增量（日常，守护进程就是这么跑）

```bash
bin/run_trees.sh --incremental --no-full-fallback
```

- `--no-full-fallback`：没有基线就直接退 3，**绝不悄悄全量**。
- 无新增时两棵树都写 `noop`，不调模型。
- 只判新增条目，去重键 `(trace_id, problem_index)`。

### 2.2 只跑一棵 / 先看不动

```bash
bin/run_trees.sh --only aime_fast --incremental --no-full-fallback
bin/run_trees.sh --dry-run          # 只 ingest + chunk，不归类、不写交付
```

### 2.3 全量 / 重建

```bash
# 先停：tmux kill-session -t cluster-watch-aime; tmux kill-session -t cluster-incr-aime
bin/run_trees.sh                    # 两棵树全量；耗时远大于增量，别在高峰期做
```

- 全量会先清 `runs/<树>/run`，成功后用新 `run` 取代旧 `run_merged`；
  **失败时旧 `run_merged` 仍在**（不会先删），但别把“全量”当常规操作。
- `--rebuild` 是“全量重新 ingest，但按 `(trace_id, problem_index)` 复用上一版判定”，
  用于换交付格式/身份口径，避免 20 万条重判漂移；它不是无脑重判。
- 真要重新调模型判全部条目，才用 `--rebuild` 并接受成本；否则不要动历史判定。

### 2.4 临时降并发

```bash
bin/run_trees.sh --incremental --concurrency 2
```

只在 429 持续时用；不要往上调（上限 32，当前 4 是网关限流后的值）。

## 3. 一轮内部做了什么

```text
cases 索引（cases_aime.jsonl）
  → 扫 0_daily_eval/<model>，软链到 runs/_src/<树>/{daily,lost}/
  → ingest（归因 + eval_input 按 trace_id join）
  → make_delta（只取未判条目）
  → chunk（全量 200/片；增量 60/片；都按环节）
  → classify（直调网关；非法 path 先重试，再带清单重问一次）
  → merge（增量并回 run_merged；全量产出 run）
  → finish（aggregate / verify / report / chart / syndrome / manifest）
  → 拷 11 个文件到 <AIME_ROOT>/cluster/<树>/
  → 重建 badcase_aime / goldencase_aime
```

当前实测：`aime_fast` 增量 4,748 条约 5–6 分钟；`aime_expert` 189 条约 1 分钟。
耗时主要看新增条目数和网关状态，不看总条目数。

## 4. 监控

```bash
tmux ls
tmux attach -t cluster-incr-aime          # Ctrl-b d 退出，不要 Ctrl-c
tail -f logs/incremental-aime.log
tail -n 5 runs/_runs.jsonl
cat logs/incr_status_aime.json
```

正常的一轮在日志里长这样：

```text
chunks=83/83 items=4748 calls=83 failed=0 elapsed=196s rate=1455 items/min
items=333811 sessions=59347 L1=12 L3=173 L4=0 edge=5894
leaves=173 total=333811 locator_mismatch=0 errors=0
  交付：.../cluster/aime_fast（11 个文件）
全部完成。
```

## 5. 什么算成功

同时满足四条：

1. `runs/_runs.jsonl` 最后两行分别是 expert / fast，`verify` 为 `err=0,mismatch=0` 或 `noop`；
2. `runs/<树>/out/verify.json` 里 `errors=0`、`locator_mismatch=0`；
3. `<AIME_ROOT>/cluster/<树>/` 有 11 个文件，`MANIFEST.json` 的 mtime 是刚才；
4. `logs/incr_status_aime.json` 的 `rc=0`。

任何一条不满足，都不要对外说“跑完了”；按 [troubleshooting.md](troubleshooting.md) 查。

## 6. 停 / 起守护

```bash
# 停（先看门狗，后主循环）
tmux kill-session -t cluster-watch-aime
tmux kill-session -t cluster-incr-aime

# 起（先主循环，后看门狗）
tmux new -d -s cluster-incr-aime  "cd $PWD && CLUSTER_INTERVAL=7200 bin/channel_daemon.sh aime 7200 >> logs/incremental-aime.log 2>&1"
tmux new -d -s cluster-watch-aime "cd $PWD && CLUSTER_INTERVAL=7200 bin/watch_daemon.sh aime >> logs/watchdog-aime.log 2>&1"
```

看门狗每 300 秒检查主循环，被 OOM 杀掉会拉起；**只判断进程在不在，不判断任务成没成功**。
所以主循环“在跑但一直失败”时，看门狗不会救，要看 `failures_aime.log`。

## 7. 交付与话术

每棵树 11 个文件（见 [handoff.md](handoff.md) §6）。对外只说三个数，不要只丢文件：

1. **最大单点**：会话数最高的 1–3 个叶子（读 `cluster_brief.md`）；
2. **最普遍的机制**：机制排名，以及它和环节的交叉；
3. **诚实刻度**：`edge` 比例、`verify` 结果、用的是快速还是质量模式。

当前口径：排序按**会话数**，不按条目数；`low_sample` 不参与排名；`edge` 先复核。

## 8. 什么时候停手

- 预检 `BLOCKED`：等外部条件，不开跑。
- 连续 429：按并发 2 补跑一次；还失败就等网关高峰过去，**不要加并发**。
- 同一棵树连续 ENOMEM：4.4.9 已自动重试一次；仍失败说明这一轮太大，等下一轮或先清页缓存。
- 缺叶 / 非法 path：该片不交付、整批 `rc=1`，看 `unresolved_*.json` 的 `model_path` 补叶，
  **不要加兜底**，也不要手工改 `items.jsonl`。
- 磁盘告警：先清 `.archive` / `stress-*`，不要删 `cluster/`、`runs/*/run_merged` / `run`。

## 9. 变更后

- 代码改了：本机 `./deploy_local.sh aime`，看远端预检；再用 §2.1 跑一轮验证。
- 分类法改了：先 `python3 bin/taxonomy_lint.py` 到 `errors=0`，再部署。
- 口径 / 版本变了：更新 `VERSION.json`，在 [changelog.md](changelog.md) 记一行，
  并刷新 [handoff.md](handoff.md) 的快照日期。
