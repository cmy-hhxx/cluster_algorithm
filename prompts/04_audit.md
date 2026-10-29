# 04 双盲重标（信度检验）

> 占位符：`{{ROOT}}` · `{{RUN}}` · `{{L1}}` · `{{CHUNK}}` · `{{N}}`
> 目的不是"再标一遍"，是**测量两个独立判断者能否复现同一结果**。看到原判就等于作废。

## 1. 盲法要求（硬性）

- 输入文件 `run/audit/{{CHUNK}}.jsonl` **不含** `path` 字段；若发现含 path，立即停止并报告。
- 不得打开 `run/assign/`、`cluster_tree.json`、`item_index.jsonl`、`leaves.tsv` 中任何文件。
- 不得参考其它分片的结论。

## 2. 输入

1. 分类法：`{{ROOT}}/taxonomy/INV_{{L1}}.md`（完整读）
2. 条目：`{{RUN}}/audit/{{CHUNK}}.jsonl`（{{N}} 行，字段 `seq/component/label/definition`）

## 3. 判定

与 01 同口径（L1 由 component 定，M1→M7 最早失效点，L3 看实际动作）。
**唯一区别**：这里没有 `reason`，证据更少。允许更保守，但必须给唯一结论，不许留空。

## 4. 输出

`audit/{{CHUNK}}.out.jsonl`：`{"seq": 12, "path": "{{L1}}.M4.1"}`

## 5. 一致性与裁决

- 算 Krippendorff α：`python3 bin/krippendorff.py audit/a.out.jsonl audit/b.out.jsonl`
- 判定标准：**α ≥ 0.75 合格**；0.6–0.75 需说明分歧集中在哪些叶子；**α < 0.6 的叶子退回重定义**（改 definition/include/exclude，不重标整棵）。
- 分歧裁决：只裁决"定义是否写清"，不裁决"谁对"。定义改完后再抽 50 条复测。

## 6. 交付回复（≤6 行）

```
done {{CHUNK}} n={{N}}
lowest-confidence: seq=..., seq=...
observed issues: <分类法里哪条 include/exclude 实际不够互斥>
```
