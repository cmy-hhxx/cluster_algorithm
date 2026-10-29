# 03 L4 归类（把叶子内条目分到子类）

> 占位符：`{{ROOT}}` · `{{RUN}}` · `{{LEAF}}` · `{{CHUNK}}` · `{{N}}`

## 1. 输入

1. 方案：`{{RUN}}/l4/{{LEAF}}.plan.json`（**唯一合法的子类来源**）
2. 条目：`{{RUN}}/l4/{{CHUNK}}.jsonl`（{{N}} 行）

## 2. 规则

- 只能使用 plan 里的 4 级 id（`{{LEAF}}.1` …）；**不许新造、不许改 plan**。
- 先按 plan 的 `include` 正匹配；多个命中时按 `exclude` 逐条排除。
- 全部排除后仍无归属：选 `definition` 最接近的一个，标 `"edge": true`，并在回复里报数。
- 继承 Tier-1 的裁决原则：看**实际失效动作**，不看 label 措辞。

## 3. 输出

`assign/{{CHUNK}}.out.jsonl`：

```json
{"seq": 12, "path": "{{LEAF}}.2", "edge": false}
```

## 4. 自检

```bash
python3 - <<'PY'
import json
src=[json.loads(l) for l in open('{{RUN}}/l4/{{CHUNK}}.jsonl') if l.strip()]
out=[json.loads(l) for l in open('{{RUN}}/assign/{{CHUNK}}.out.jsonl') if l.strip()]
plan=json.load(open('{{RUN}}/l4/{{LEAF}}.plan.json'))
ids={c['id'] for c in plan['children']}
assert len(out)=={{N}} and {r['seq'] for r in out}=={r['seq'] for r in src}
assert all(r['path'] in ids for r in out), {r['path'] for r in out}-ids
print('OK', len(out), sorted({r['path'] for r in out}))
PY
```

## 5. 交付回复（≤4 行）

`done {{CHUNK}} n={{N}}` + 各子类计数 + `edge` 数。
