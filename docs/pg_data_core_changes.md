# `pg_data_core` 配合聚类 PG-Native 化改动需求

> 对应架构：AIME 聚类切换为 PG 16 真源（高频增量 `POST /api/v1/data-core/clusters/bind` + 按日/按需 `writers publish-cluster` 发版）。  
> 目标代码根：`/opt/pg_data_core/pg_data_core/`。所有改动保持向后兼容，不改表结构（零 migration）。

## 改动总览

| 优先级 | 项 | 涉及文件与行号 | 核心目的 |
|---|---|---|---|
| **P0** | 1. 增量绑定补写 `public.changes` + 改批量更新 | `core.py:747-789`、`http_api.py:230-238` | 让 5003 `/changes` 下游感知聚类回填；提升批量吞吐、减少锁持有 |
| **P0** | 2. 修复 `publish-cluster` 冲突更新漏字段 | `writers.py:263-286` | 支持同日幂等重发，防止 `cluster_trees` 统计与 `data_cutoff` 停留在旧值 |
| **P1** | 3. `publish-cluster` 支持 `--skip-bind`、差异更新与 `is_latest` 守卫 | `writers.py:312-364, 415-419` | 避免重复重写 515k 行引发 PG 膨胀；不误改历史重评行；兼容 `scope` 字段 |
| **P1** | 4. 放开模型名硬编码 + 增量推进 `data_cutoff` | `writers.py:416`、`core.py:747`、`http_api.py:230` | 新模型免改 data-core；5003 的 `cluster_through` 随增量实时前进 |

---

## 1. [P0] `bind_problem_clusters` 补写 `public.changes` 并改为批量更新

### 现状
- `core.py:747-789` 逐条 `for item in bindings: conn.execute("UPDATE ...")`，单批数千条时往返次数高。
- 绑定成功后**未写 `public.changes`**，导致 5003 `/api/v1/analysis/changes` 下游订阅者拿不到 `cluster_code` 从 `NULL` 变为已聚类的变更通知。

### 改动要求
1. **批量更新**：在单事务内改用 `UNNEST` 数组或临时表 / `executemany` 一次性更新 `public.eval_problems`，并加差异条件避免空写死元组：
   - 仅当 `cluster_code IS DISTINCT FROM new_code OR is_edge IS DISTINCT FROM new_edge OR tags IS DISTINCT FROM COALESCE(new_tags, tags)` 时更新。
   - 更新列保持不变：`cluster_tree_id = COALESCE(%s, cluster_tree_id)`、`cluster_version = COALESCE(%s, cluster_version)`、`cluster_code`、`l1_stage`、`l2_mechanism`、`l3_leaf`、`l4_subleaf`、`is_edge`、`tags = COALESCE(%s, tags)`、`clustered_at = now()`。
2. **补写 `public.changes`（按 trace 去重）**：
   - 收集本次实际更新的 `eval_problems` 行（`RETURNING input_id, output_id`），仅对 **`eval_outputs.is_latest = TRUE`** 且 **`NOT eval_inputs.is_deleted`** 的受影响 trace 按 `DISTINCT (input_id, output_id)` 写入 `public.changes`（同一 trace 多条 problem 只写 1 条）：
     ```sql
     INSERT INTO public.changes(
         db_epoch, entity_type, op, human_only,
         input_id, output_id, product, model, app_version, biz_day, trace_id
     )
     SELECT DISTINCT
         %s, 'trace', 'upsert', FALSE,
         i.input_id, o.output_id, i.product, i.model, i.app_version, i.biz_day, i.trace_id
     FROM public.eval_outputs o
     JOIN public.eval_inputs i ON i.input_id = o.input_id
     WHERE o.output_id = ANY(%s)
       AND o.is_latest = TRUE
       AND NOT i.is_deleted
     ```
3. **HTTP 接口返回兼容**（`http_api.py:230-238`）：保持原有 `{"updated": int}` 字段，可附加 `"changes_emitted": int`（或保持原返回由服务端内部写完）。

---

## 2. [P0] 修复 `publish-cluster` 的 `ON CONFLICT DO UPDATE` 漏字段

### 现状
`writers.py:263-286` 写 `public.cluster_trees` 时：
- `INSERT` 漏了 `taxonomy_desc`（`etl/migrate_cluster.py:577,588` 有写，`writers.py` 漏了）；
- `ON CONFLICT (product, model, release_tag) DO UPDATE SET` 只更新了 `is_current`、`total_items`、`artifact_rel_dir`、`generated_at`、`extra`，导致同日重复发版时其余关键列停留在旧值。

### 改动要求
1. `INSERT` 补回 `taxonomy_desc`（取 `pv.get("taxonomy") if isinstance(pv, dict) else None`，与 `migrate_cluster.py` 一致）。
2. `ON CONFLICT (product, model, release_tag) DO UPDATE SET` 补全以下列：
   ```sql
   ON CONFLICT (product, model, release_tag) DO UPDATE SET
       version          = EXCLUDED.version,
       taxonomy_desc    = COALESCE(EXCLUDED.taxonomy_desc, public.cluster_trees.taxonomy_desc),
       is_current       = TRUE,
       total_items      = EXCLUDED.total_items,
       total_traces     = EXCLUDED.total_traces,
       total_sessions   = EXCLUDED.total_sessions,
       leaf_count       = EXCLUDED.leaf_count,
       verify_errors    = EXCLUDED.verify_errors,
       verify_mismatch  = EXCLUDED.verify_mismatch,
       edge_flagged     = EXCLUDED.edge_flagged,
       data_cutoff      = EXCLUDED.data_cutoff,
       artifact_rel_dir = EXCLUDED.artifact_rel_dir,
       generated_at     = EXCLUDED.generated_at,
       extra            = EXCLUDED.extra
   ```

---

## 3. [P1] `publish-cluster` 增加 `--skip-bind`、差异更新与 `is_latest` 守卫

### 现状
`writers.py:312-363` 只要 `--cluster-dir` 里存在 `item_index.jsonl` 就无条件全表 `UPDATE`：
- 每次发版重写 515k 行产生大量 PG dead tuples；
- `WHERE` 条件未限制 `eval_outputs.is_latest = TRUE`，可能覆盖历史重评旧版本；
- 解析 `scope` 只看旧字段 `json_path`（`writers.py:336-337`），未优先读 `it.get("scope")`；
- 绑定变更后同样未写 `public.changes`。

### 改动要求
1. CLI 参数（`writers.py:415-419`）新增 `--skip-bind`（`action="store_true"`）：开启时只拷 10 个交付文件、切 `current` 软链、更新 `cluster_trees` 与 `cluster_nodes`，跳过 `item_index.jsonl` 绑定。
2. 未传 `--skip-bind` 且存在 `item_index.jsonl` 时：
   - `scope` 解析改为：`scope = clean_str(it.get("scope")) or ("session" if str(it.get("json_path") or "").startswith("session_problem") else "turn")`。
   - `UPDATE public.eval_problems p` 增加 **最新输出过滤 + 差异更新**，并收集变更 `output_id` 写入 `public.changes`（同第 1 节 SQL）：
     ```sql
     UPDATE public.eval_problems p
     SET cluster_tree_id = %s, cluster_version = %s, cluster_code = s.cluster_code,
         l1_stage = s.l1_stage, l2_mechanism = s.l2_mechanism, l3_leaf = s.l3_leaf,
         l4_subleaf = s.l4_subleaf, is_edge = s.is_edge, tags = s.tags, clustered_at = %s
     FROM _stage_cl s
     JOIN public.eval_outputs o ON o.is_latest = TRUE AND o.model = %s AND o.trace_id = s.trace_id
     WHERE p.output_id = o.output_id
       AND p.scope = s.scope
       AND p.turn_index = s.turn_index
       AND p.problem_index = s.problem_index
       AND (
           p.cluster_code IS DISTINCT FROM s.cluster_code
           OR p.is_edge IS DISTINCT FROM s.is_edge
           OR p.tags IS DISTINCT FROM s.tags
       )
     RETURNING p.output_id
     ```

---

## 4. [P1] 放开模型名硬编码 & 增量推进 `data_cutoff`

### 现状
- `writers.py:416` 将 `publish-cluster --model` 写死为 `choices=["aime_expert", "aime_fast"]`，新增模型时会被 CLI 拦截。
- 5003 `pg_store.py:130` 读 `MAX(data_cutoff) FROM cluster_trees WHERE is_current = TRUE` 作为 `cluster_through`。高频走 `/clusters/bind` 而日级走 `publish-cluster` 时，`cluster_through` 会滞后最多 24 小时。

### 改动要求
1. 移除 `publish-cluster` 的 `--model` `choices` 限制（仅要求非空字符串，如 `^[A-Za-z0-9_.-]+$`）。
2. `POST /api/v1/data-core/clusters/bind`（`http_api.py:230-238`）及 `DataCore.bind_problem_clusters`（`core.py:747`）增加可选参数 `data_cutoff: str | None = None`：
   - 当调用方传入 `tree_id` 且传入非空 `data_cutoff`（ISO-8601 UTC 字符串）时，在同一事务内单调推进 `cluster_trees.data_cutoff`：
     ```sql
     UPDATE public.cluster_trees
     SET data_cutoff = GREATEST(COALESCE(data_cutoff, ''), %s)
     WHERE tree_id = %s AND is_current = TRUE
     ```

---

## 验收检查

1. 调一次 `POST http://127.0.0.1:5003/api/v1/data-core/clusters/bind`（传入 1 条变更与 1 条未变更的 `problem_id` + `tree_id` + `data_cutoff`）：
   - 返回 `updated == 1`（未变更行不空写）；
   - `public.changes` 新增 1 条对应 `trace_id` 的 `op='upsert', human_only=FALSE` 记录；
   - `cluster_trees.data_cutoff` 单调推进（传入更早时间戳时不倒退）。
2. 对同一 `release_tag` 连跑两次 `python3 -m pg_data_core.writers publish-cluster --model aime_fast --cluster-dir <dir> --skip-bind`：
   - `bound == 0`，`cluster_trees` 的 `total_traces / total_sessions / leaf_count / data_cutoff / taxonomy_desc` 均与目录内 `cluster_tree.json` 一致。
3. 去掉 `--skip-bind` 再跑一次相同目录：`bound == 0`（差异守卫生效，零无意义重写），且历史 `is_latest = FALSE` 的 `eval_problems` 不被改动。
