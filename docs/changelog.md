# 版本与事故记录

> 只记会影响当前运行的变更，不写流水账。当前版本 **4.6.0**（2026-11-26）。

## 1. 版本线

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-30 | 3.0.0 | 提示词重写、分类法自检、快速/质量双模式、一页纸 `cluster_brief.md` |
| 2026-11-01 | 4.1.0 | 第一轴扩到 12 段，按 `component_role` 切 |
| 2026-11-02 | 4.2.0 | `bad_case_id`（渠道+问句+分钟级时间哈希） |
| 2026-11-03 | 4.3.0 | 聚类拆到两台机器；树表改扫目录；代码改 rsync + 指纹 |
| 2026-11-11 | 4.4.0 | 输入规整为 `eval_input`（trace 粒度）；身份改 `trace_id`；交付去掉行号/绝对路径/`src_map.json`；新增 `cases/badcase/goldencase` |
| 2026-11-16 | 4.4.4 | **取消 classify 兜底**：非法叶直接失败，写 `unresolved_*.json` |
| 2026-11-18 | 4.4.7 | 12×7 矩阵体检，补叶至 **181 叶** + `matrix_na.json` / `MATRIX.md` |
| 2026-11-18 | 4.4.8 | 非法 path「带清单重问」；`missing_leaf` 留痕 |
| 2026-11-18 | 4.4.10 | 单树 ENOMEM 自动重试（4.4.9）；请求重试提至 **8 轮**扛 429 高峰 |
| 2026-11-20 | 4.4.12 | `session_id="None"` 退用 `trace_id` 兜底（4.4.11）；新增 `prune_items.py` 脏数据同步清理 |
| 2026-11-20 | 4.4.14 | 身份索引 TTL 12h→1h 增量刷新（4.4.13）；内存闸门改看 cgroup `anon` |
| 2026-11-22 | 4.4.17 | `classify` 与 `run_trees` 网关探活均加 4 次退避重试（4.4.15–4.4.16）；登记 `understand-image-10` |
| 2026-11-23 | 4.4.19 | L4 第四层下钻上线：12 个高影响叶子 / **42 个子类**，每轮增量自动补跑 `l4_apply` |
| 2026-11-24 | 4.5.0 | 清理过期迁移脚本、旧代理提示词与死代码，修复 `item_index` 字段投影与 `S10–S12` 机制汇总；新增独立可移植模块 `cluster_pkg`（支持 `config.yaml`、OpenAI-Responses 协议端点、既有树增量演进、新组件/新叶自适应创建） |
| 2026-11-26 | 4.6.0 | `cluster_pkg` 分发升级：兼容 Chat Completions 自动降级、`prune_tree` / `--prune` 脏数据清理、修复 `l4.force` 被 `noop` 跳过与 L4 分片对半拆片容错；脱敏示例配置并新增 `bin/pack_cluster_pkg.py` 一键同步与打包 `.whl`/`.tar.gz` |

## 2. 关键基础设施变更

### 2026-11-09 AIME 迁到 SSD

- 数据根迁至 **`/mnt/aime_eval_ssd/1_aime/eval_output`**，代码同步迁到 `/mnt/aime_eval_ssd/code/2_cluster_algorithm`。
- 交付物不存行号与绝对路径，换挂载点只改 `AIME_ROOT`。

### 2026-11-17 统一网关 + 容器重建

- 网关切至 **`http://10.217.220.51/ai-gateway/v1`**（LiteLLM，按 key 限流，要 API key）；并发固定 **4**，429/5xx 退避重试。
- 容器更新为 `interactive-s3ynzadcvm9c`，SSH 端口 **2473**，内存配额 **40 GiB**。
- ceph `/mnt/aime_eval_ssd` 保留；本地盘 `/opt/cluster-venv`、tzdata、tmux 重建。

### 2026-11-12 输入一对一清理

上游按 trace 去重（`one-to-one-report.json`）：123,339 条输入 trace，归因保留 102,679 条，归档重复与孤立记录，重跑半成品不再进树。

## 3. 事故与修复

| 日期 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 11-09 | 预检 `find` 卡 >5 分钟 | `.delivery` 下小文件极多，ceph-fuse 逐个 stat | find 先 prune 隐藏/产物目录 |
| 11-11 | `aime_expert` 整树不交付 | S8 缺“图像数值本身错”叶子 | 补 `S8.M5.2`；4.4.4 起取消隐式兜底 |
| 11-12 | expert ingest 中断 | 线上出现未登记组件 `stock-screener` / `catalyst-calendar` | 登记 components/stages 后补跑 |
| 11-18 | fast / expert 整树不交付 | S12 无 M5 位、S9 无 M3 位，模型序号或机制越界 | 补叶至 181 叶 + 带合法清单重问一次（4.4.5–4.4.8） |
| 11-18 | fast `make_delta` ENOMEM | 页缓存顶满 cgroup，分配瞬间回收跟不上 | 单树 sync+10s 重试一次（4.4.9） |
| 11-18 | fast 429 整树失败 | 5 轮请求重试全撞限流 | 请求重试提至 8 轮（4.4.10） |
| 11-20 | expert 整树不交付 | 上游回补把 `session_id` 写成字符串 `"None"`（119 条） | ingest 退用 `trace_id`（4.4.11），脏数据用 `prune_items.py` 清理（4.4.12） |
| 11-20 | 索引刷新长期被跳过 | 身份索引 12h TTL 过长，且内存闸门把页缓存算进占用 | TTL 改 1h，内存闸门改看 `anon`（4.4.13–4.4.14） |
| 11-22 | 瞬时抖动致整轮跳过或单树失败 | 网关探活只测一次即放弃 | `classify` 与 `run_trees` 均加 4 次退避探活（4.4.15–4.4.16） |
| 11-22 | expert 连续 ingest 失败 | 新增图像理解通道 `understand-image-10` 未登记 | 登记至 S8，并在 `component_patterns.json` 补通配规则 |

**统一原则**：不隐式兜底、不替模型瞎猜、不悄悄吞失败。生产原位脚本遇缺叶留痕待审；外部业务模块 `cluster_pkg` 支持受 `taxonomy_lint` 约束的自适应建叶与演进审计。

## 4. 当前口径（4.6.0）

- 分类法：12 环节 × 7 机制 = 181 叶 + 12 叶下钻 42 个 L4 子类；矩阵 67/84 有叶，17 格登记“不适用”。
- 质量：双盲 α=0.918；质量模式叶子准确率 91.3%，快速模式 88.1%。
- 交付：每棵树 11 件；`verify` 必须 `errors=0`、`locator_mismatch=0`。
- 身份：`trace_id`；回原文一律去 `eval_input`，不再用文件行号。
