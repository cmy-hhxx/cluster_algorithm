# 05 漂移归纳（新数据出现了旧分类法装不下的东西）

> 占位符：`{{ROOT}}` · `{{RUN}}` · `{{N}}` 未归类条目数
> 触发条件：`UNMAPPED` 占比 > 5%，或出现 ≥30 条同质新现象。

## 1. 输入

`{{RUN}}/drift/unmapped.jsonl`（归不进 91 叶的条目，含 `seq/component/label/definition/reason`）

## 2. 判断分三步

1. **去重**：把 `label` 不同的条目按 `definition` 归成现象簇（不看措辞）。
2. **归因**：每个簇问一句——"这是旧叶子的**定义没覆盖**，还是执行方**没按定义判**？"
   - 定义没覆盖 → 候选新叶子（走 §3）
   - 执行方没按定义判 → 不改分类法，改归类提示词或重标（走 §4）
3. **门槛**：候选需满足 ① ≥30 条；② 跨 ≥5 个会话；③ 能挂到**现有某个 L2** 下（不新造机制）；④ 与现有叶子能写出互斥 exclude。

## 3. 输出

`{{RUN}}/drift/proposal.json`：

```json
{
  "verdict": "extend | no_change",
  "reason": "为什么扩 / 为什么不扩",
  "candidates": [
    {"parent_l2": "Q.M4", "name": "≤14 字", "plain": "一句人话",
     "definition": "...", "include": "...", "exclude": "→ 现有哪个叶子",
     "evidence_seqs": [12, 33, 41], "sessions": 7, "share_of_unmapped": 0.31}
  ]
}
```

## 4. 不允许做的事

- 新造 L1 或 L2（机制层与环节层是稳定的，变的是模式层）。
- 把零散条目包装成"新现象"（<30 条一律 `no_change`，写清原因）。
- 为了让 `UNMAPPED` 归零而放宽现有叶子的 include。

## 5. 交付回复（≤6 行）

```
verdict=extend/no_change
clusters: 3 (sizes 140/52/31)
proposed: Q.M4.x 新增「...」← 挂 Q.M4，排除 Q.M4.1
needs: 人工确认后写入 taxonomy/inventory.json 并升版本
```
