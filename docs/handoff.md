# 交接：AIME 的两棵树（弗吉尼亚｜PG 16 原生版）

> 快照 **2026-11-27 CST**｜版本 **4.7.0**｜后端 **PG 16 + 多帧 Zstd**
> 写给接手的人：先看这一份。日常操作见 [runbook.md](runbook.md)，异常排查见 [troubleshooting.md](troubleshooting.md)。

## 1. 一分钟结论

AIME 的生产聚类只在**弗吉尼亚一台机器（`aime_rsi_data`）**运行，数据已切换至 **PostgreSQL 16（`127.0.0.1:5432/aime_eval`）+ 多帧 Zstd（`/mnt/aime_eval_ssd/1_aime`）**：

| 模型 | PG 最新有效条目 | 已下钻 L4 | 交付路径 | 校验 |
|---|---:|---:|---|---|
| `aime_fast` | **515,799** | 199,060（42 子类） | `/mnt/aime_eval_ssd/1_aime/cluster_tree/aime_fast/current` | `err=0 / mismatch=0` |
| `aime_expert` | **173,319** | 59,119（42 子类） | `/mnt/aime_eval_ssd/1_aime/cluster_tree/aime_expert/current` | `err=0 / mismatch=0` |

- **唯一状态真源是 PG `public.eval_problems`**（不再扫旧 `.jsonl`、不再维护本地 `run_merged` 基线、不再生成 `cases/badcase/goldencase_aime.jsonl`）。
- **高频增量回填**：tmux 守护进程每 **15 分钟（900s）**查 `cluster_code IS NULL`，按 O(1) Zstd `pread_json` 补 `definition`/`reason`，跑完 L3 + L4 后分批 `POST http://127.0.0.1:5003/api/v1/data-core/clusters/bind` 回填 PG（同步写 `public.changes` 与推进 `data_cutoff`）。
- **按日/新叶整树发版**：每日 **CST 04:00** 窗口（及增量首次命中 `cluster_nodes` 尚无的休眠叶时）流式聚合 PG 全量最新有效数据，生成 **10 件交付物**并调 `pg_data_core.writers publish-cluster --skip-bind` 发布 `v<ver>_<YYYYMMDD>` 与切换 `current` 软链。

## 2. 连接服务器与三分钟自检

```bash
ssh aime_rsi_data
cd /mnt/aime_eval_ssd/code/2_cluster_algorithm
source .deploy/env.sh
tmux ls                                   # cluster-incr-aime / cluster-watch-aime
bin/preflight.sh                          # 8 项预检：0 FAIL / 0 BLOCKED
python3 bin/pg_cluster.py status          # 5003 health + 两模型绑定进度 + current tag
cat logs/incr_status_aime.json            # 最近一轮 rc / 时间
tail -n 6 runs/_runs.jsonl                # 最近增量与发版账本
```

连接信息唯一来源为仓库根 `.servers.conf`（已 gitignore）；端口 `2473`，容器重启或换端口后先 `ssh -O exit aime_rsi_data`。

## 3. 目录与关键路径

```text
/mnt/aime_eval_ssd/
├── 1_aime/                                         AIME_DATA_ROOT（唯一冷数据根）
│   ├── eval_input/<model>/<YYYY>/<MMDD>/*.jsonl.zst
│   ├── eval_output/<model>/<YYYY>/<MMDD>/{attribution,compact,human_read}_part_*.jsonl.zst
│   └── cluster_tree/<model>/
│       ├── current -> v<ver>_<YYYYMMDD>            原子软链
│       └── v<ver>_<YYYYMMDD>/                      10 个交付文件（不含 item_index.jsonl）
└── code/2_cluster_algorithm/
    ├── bin/pg_cluster.py                           PG-Native 主入口（status/incremental/publish/backfill-l4/recall）
    ├── runs/_runs.jsonl                            增量与发版统一账本
    ├── runs/_pg_delta/<model>/                     增量临时工作目录（成功后自动清理）
    ├── runs/_pg_publish/<model>/out/               发版暂存目录
    ├── logs/                                       incremental/watchdog/failures/missing_leaf/unregistered
    └── .deploy/                                    env.sh + DEPLOYED.json + 字体
```

> `/opt/cluster-venv/pyvenv.cfg` 已开启 `include-system-site-packages = true`，同时具备 `PIL`（画图）与系统 Python 的 `psycopg` / `zstandard` / `orjson`。

## 4. 自动增量与看门狗

```text
tmux: cluster-incr-aime   bin/channel_daemon.sh aime
        └─ 每 900s: python3 bin/pg_cluster.py incremental --concurrency 4 --daily-publish-hour 4
             ├─ 查 PG (is_latest AND NOT is_deleted AND cluster_code IS NULL, LIMIT 5000)
             ├─ O(1) Zstd pread 补 definition/reason，隔离未登记组件（不阻塞合法行）
             ├─ L3 归类 → L4 下钻 → 分批 POST /api/v1/data-core/clusters/bind
             └─ 命中休眠新叶 / 到达 CST 04:00 窗口 → publish-cluster --skip-bind

tmux: cluster-watch-aime  bin/watch_daemon.sh aime
        └─ 每 300s 检查 cluster-incr-aime，缺失即拉起
```

**停 / 起（维护期先放暂停标记，避免 `eval_supervisor.sh` 30s 内自动自愈拉回）：**

```bash
# 停（维护期间）
touch runs/.daemon_pause
tmux kill-session -t cluster-watch-aime
tmux kill-session -t cluster-incr-aime

# 恢复（或一键自愈：自动补齐 /opt/cluster-venv + tzdata + 双守护）
rm -f runs/.daemon_pause
bash bin/ensure_aime_pg_daemon.sh
```

> 容器重建自愈：`/opt/eval_supervisor.sh`（已同步备份至 `/mnt/aime_eval_ssd/.service_backup/` 与快照）每 30 秒巡检，若检测到 `/opt/cluster-venv` 或聚类 tmux 会话缺失且无 `runs/.daemon_pause`，在 `5003` 就绪后自动调用 `bin/ensure_aime_pg_daemon.sh` 恢复环境与双守护。

## 5. 交付物（每棵树 10 件）

| 文件 | 说明 |
|---|---|
| `cluster_brief.md` | **一页纸结论**（Top 问题、L4 高优子类、机制层分布），业务先看这个 |
| `cluster_summary.md` | 段 / 机制 / 标签 / L4 分布及各段 Top 模式 |
| `cluster_tree.json` / `.txt` | 结构化主树与终端树形打印（PG `cluster_trees` / `cluster_nodes` 同步入库） |
| `leaves.tsv` | 叶子明细表 |
| `syndrome.md` / `.json` | 会话级失效机制共现组合 |
| `cluster_matrix.png` | 环节 × 机制热力图 |
| `MANIFEST.json` / `verify.json` | 产物校验和与自检报告（强制 `errors=0` 且 `locator_mismatch=0`） |

> 原 1GB+ 的 `item_index.jsonl` 已由 PG `public.eval_problems` 取代；按节点召回样本请用 `python3 bin/pg_cluster.py recall --model aime_fast --node S9.M5.1 --limit 5 --full`。

## 6. 风险与铁律

- **严格分类法门禁**：未登记组件写入 `logs/unregistered_<model>.json` 并返回非零告警，失败分片写入 `logs/<model>_*_unresolved_*.json` 与 `logs/missing_leaf_aime.jsonl`；坏行保留 `cluster_code IS NULL` 待登记/补叶后自动补齐，不阻塞其余合法条目回填。
- **并发与网关**：LiteLLM 托管网关并发保持 **4**；换 key 只改本机 `.deploy/local.conf` 再 `./deploy_local.sh aime`。
- **代码同步**：服务器无 `.git`，本机改完后统一 `./deploy_local.sh aime`，版本以 `.deploy/DEPLOYED.json` 为准。
