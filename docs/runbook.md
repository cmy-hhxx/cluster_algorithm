# 运行手册（AIME / 弗吉尼亚｜PG 16 原生版）

> 收到「跑一下 AIME / 查一下聚类」时按本文执行。架构现状见 [handoff.md](handoff.md)，异常排查见 [troubleshooting.md](troubleshooting.md)。

## 0. 固定前提

| 项 | 值 |
|---|---|
| 渠道 / 后端 | `aime` / `pg`（由 `.deploy/env.sh` 固定） |
| 机器 | 弗吉尼亚 `aime_rsi_data` |
| 代码目录 | `/mnt/aime_eval_ssd/code/2_cluster_algorithm` |
| 数据根 | `/mnt/aime_eval_ssd/1_aime`（PG `127.0.0.1:5432/aime_eval` + 多帧 Zstd） |
| 写入接口 | `http://127.0.0.1:5003/api/v1/data-core/clusters/bind` |
| 模型 | `aime_fast`、`aime_expert` |
| 并发 / 节奏 | 并发 **4**；守护每 **900 秒（15 分钟）**增量回填 + 每日 **CST 04:00** 自动发版 |

**不要在本机跑生产**：本机改代码，然后 `./deploy_local.sh aime` 同步并预检。

## 1. 开始前

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh          # 必须：加载 venv / PYTHONPATH / PG / 网关 / TZ
bin/preflight.sh               # 8 项：0 FAIL 且 0 BLOCKED 才可开跑
python3 bin/pg_cluster.py status
```

与守护进程共享 `runs/.lock` 并发锁；手动做大批次发版或补钻时建议先按 §5 暂停守护。

## 2. 常用命令

### 2.1 手动触发一轮增量回填（L3 + L4 → `/clusters/bind`）

```bash
python3 bin/pg_cluster.py incremental
python3 bin/pg_cluster.py incremental --only aime_fast --limit 2000
python3 bin/pg_cluster.py incremental --dry-run          # 只查 PG + Zstd pread + 分片，不调模型不写库
```

- 无待聚类行（`cluster_code IS NULL = 0`）秒级 `noop`，不探活归类网关。
- 遇到未登记组件或个别失败分片时：合法行照常回填 PG，坏行保留 `NULL` 并落盘 `logs/unregistered_<model>.json` / `logs/missing_leaf_aime.jsonl`，命令返回 `rc=1` 触发告警。

### 2.2 手动整树发版（`cluster_tree/<model>/v<ver>_<date>/` + `current`）

```bash
python3 bin/pg_cluster.py publish                        # 默认 --skip-bind，不重复写 515k 行
python3 bin/pg_cluster.py publish --only aime_expert --dry-run
python3 bin/pg_cluster.py publish --rebind-all           # 仅大改分类法/全量重绑时使用
```

### 2.3 新增 L4 叶子后补钻历史条目

```bash
python3 bin/pg_cluster.py backfill-l4 --leaves S9.M5.1,S9.M5.2 --limit 5000
# 重判已下钻条目加 --force；补钻完执行一次 publish 刷新整树报告
```

### 2.4 按节点召回原始样本（直查 PG + O(1) Zstd `pread_json`）

```bash
python3 bin/pg_cluster.py recall --model aime_fast --node S9.M5.1 --limit 5 --full
```

## 3. 一轮内部做了什么

```text
增量（每 15 分钟）
  PG SELECT eval_problems (is_latest AND NOT is_deleted AND cluster_code IS NULL, LIMIT 5000)
    → 按 (attr_rel, offset, len) 去重做 O(1) Zstd pread 补 definition/reason
    → 校验组件（未登记隔离至 logs/unregistered_<model>.json）
    → 分片（60/片）→ classify.py（L3）→ l4_apply.py（命中 12 个 L4 父叶时）
    → 分批（500/批）POST /api/v1/data-core/clusters/bind（带 tree_id + cluster_version + data_cutoff）
    → 首次命中休眠叶 或 到达 CST 04:00 窗口 → 自动触发整树发版

整树发版（每日 / 手动 / 新叶触发）
  PG 服务端游标流式聚合全量最新有效 eval_problems
    → 仅对各叶抽样（~1,000 条）做 Zstd pread 补 definition
    → 产出 10 件交付物并严格自检（verify.json: errors=0, locator_mismatch=0）
    → python3 -m pg_data_core.writers publish-cluster --skip-bind
       （写入 cluster_tree/<model>/v<ver>_<YYYYMMDD>/、原子切换 current 软链、更新 cluster_trees/cluster_nodes）
```

## 4. 监控与验收口径

```bash
tmux ls
tail -f logs/incremental-aime.log
cat logs/incr_status_aime.json
tail -n 6 runs/_runs.jsonl
python3 bin/pg_cluster.py status
```

满足以下四条即正常：
1. `tmux ls` 同时存在 `cluster-incr-aime` 与 `cluster-watch-aime`；
2. `logs/incr_status_aime.json` 中 `rc=0`；
3. `pg_cluster.py status` 显示 `5003 health: ok=True`，两模型 `unclustered` 维持低位或归零，`current` 指向最新日快照；
4. `/mnt/aime_eval_ssd/1_aime/cluster_tree/<model>/current/verify.json` 满足 `errors=0`、`locator_mismatch=0`。

## 5. 停 / 起守护

```bash
# 停（先看门狗，后主循环）
tmux kill-session -t cluster-watch-aime
tmux kill-session -t cluster-incr-aime

# 起（先主循环，后看门狗；间隔自动读 .deploy/env.sh 的 CLUSTER_INTERVAL=900）
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm && source .deploy/env.sh
tmux new -d -s cluster-incr-aime  "cd $PWD && bin/channel_daemon.sh aime >> logs/incremental-aime.log 2>&1"
tmux new -d -s cluster-watch-aime "cd $PWD && bin/watch_daemon.sh aime >> logs/watchdog-aime.log 2>&1"
```

## 6. 变更与回滚

- 代码或分类法变更：本地 `python3 bin/taxonomy_lint.py` + `python3 bin/pack_cluster_pkg.py --sync` + `python3 -m unittest discover -s tests -p "test_*.py"` 通过后，执行 `./deploy_local.sh aime`。
- 切换分类法或大版本发版：跑一次 `python3 bin/pg_cluster.py publish --reason "release-<ver>"` 即可生成新快照并切换 `current`。
