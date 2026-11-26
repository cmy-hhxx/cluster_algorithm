# `cluster_pkg` — 可移植归因聚类服务模块

将「失败 / 流失归因」条目聚成**可下钻、可回溯、可增量演进**的三轴四层聚类树。

- **零运行环境依赖**：纯 Python ≥ 3.9 标准库即可运行（内置轻量 YAML 解析器、Responses / Chat Completions 双协议客户端与 SVG 矢量热力图渲染；若环境装有 `PyYAML` 或 `Pillow` 会自动启用，不装也能完整交付）。
- **开箱自包含**：内置完整三轴分类法（12 环节 × 7 机制 × 181 叶 + 42 个 L4 子类）与提示词规格，支持直接复制 `cluster_pkg/`、解压独立归档包或离线 `pip install` 使用。
- **自适应创建新组件与新叶**：遇到未登记的 `component` 自动路由责任段（`S1`–`S12`）；遇到现有叶子均无法覆盖的新失效模式自动归纳互斥新 L3 叶子，强制通过 `taxonomy_lint` 零错误自检门禁后持久化写回聚类树目录。
- **原子安全增量与脏数据清理**：传入原来的聚类树目录（`tree_dir`），自动按 `(trace_id, problem_index)` 复用历史判定，仅对新增条目调用模型；支持 `--prune` 按清单或 `self-session` 清理脏数据并零 API 重聚合；全部产物先在临时目录通过 `verify`（`errors=0, locator_mismatch=0`）后再原子替换目标目录。

---

## 1. 快速上手（30 秒）

### 1.1 编写配置文件 `config.yaml`

最少只需要 **3 项**：OpenAI 协议 API 端点（兼容 `/v1/responses` 与 `/v1/chat/completions`）、原来的聚类树目录、待聚类的归因输入路径（相对路径默认相对于 `config.yaml` 所在目录解析）：

```yaml
api:
  base_url: "${CLUSTER_BASE_URL:-https://api.openai.com/v1}"  # 支持 .../v1、.../v1/responses 或 .../v1/chat/completions
  protocol: "auto"                                            # auto（默认优先 responses，自动降级 chat_completions）
  api_key: "${OPENAI_API_KEY}"                                # 支持直填或 ${ENV_VAR} / ${ENV_VAR:-default}
  model: "${CLUSTER_MODEL:-gpt-4.1-mini}"

tree_dir: "./cluster_tree"                                    # 既有聚类树目录（首次运行为空或不存在则自动初始化）
input: "./data/attribution"                                   # 单个 .jsonl 文件或包含归因 .jsonl 的目录
```

完整可选参数见同目录下的 [`config.example.yaml`](config.example.yaml)。

### 1.2 命令行一键执行

```bash
# 在任意目录下直接运行：
python /path/to/cluster_pkg/run_cluster.py -c config.yaml

# 或进入模块目录运行：
cd /path/to/cluster_pkg && python run_cluster.py -c config.yaml

# 或以 Python 模块方式运行：
python -m cluster_pkg -c config.yaml
```

按节点召回原始证据样本：

```bash
python /path/to/cluster_pkg/run_cluster.py --tree-dir ./cluster_tree --recall S9.M6.4 --limit 5
```

清理既有聚类树中的脏数据（零 API 调用，保留既有 L4 路径并留痕备份）：

```bash
python /path/to/cluster_pkg/run_cluster.py --prune --tree-dir ./cluster_tree \
  --drop-ids dirty_cases.txt --self-session --reason "剔除上游回滚批次"
```

---

## 2. 其他项目在 Python 代码中直接引用

第三方项目支持以下**全部接入方式**（无需联网、无需安装 `setuptools`/`wheel`）：

- **解压分发包或直接复制目录 / 加入 `PYTHONPATH`**（零安装）：解压 `cluster_pkg-<ver>.tar.gz` 或把 `cluster_pkg/` 拷入业务仓库，或 `export PYTHONPATH=/path/to/cluster_algorithm`；
- **离线 `pip install`**（内置纯标准库 PEP 517 后端）：`pip install --no-index --no-build-isolation /path/to/cluster_pkg-<ver>-py3-none-any.whl`（或直接指向 `cluster_pkg/` 目录，安装后额外获得全局命令 `cluster-run -c config.yaml`）。

```python
from cluster_pkg import prune_tree, run_cluster

# 方式 1：直接传配置文件路径
result = run_cluster("config.yaml")

# 方式 2：直接传文件路径字典（无需落地配置文件）
result = run_cluster({
    "api": {
        "base_url": "https://api.openai.com/v1",
        "protocol": "auto",
        "api_key": "${OPENAI_API_KEY}",
        "model": "gpt-4.1-mini",
    },
    "tree_dir": "./my_cluster_tree",
    "input": "./eval_output/attribution.jsonl",
})

# 方式 3：直接传入内存中的归因字典列表 records（无需落地输入文件）
result = run_cluster({
    "api": {"base_url": "https://api.openai.com/v1", "api_key": "${OPENAI_API_KEY}", "model": "gpt-4.1-mini"},
    "tree_dir": "./my_cluster_tree",
    "records": [
        {"trace_id": "t1", "session_id": "s1", "component": "finquery", "severity": "P1",
         "label": "SQL报错", "definition": "表名不存在", "reason": "unknown table"}
    ],
})

# 方式 4：对既有聚类树按 case_id / trace_id 清单清理脏数据（零 API 调用）
prune_res = prune_tree("./my_cluster_tree", drop_ids=["t_bad"], self_session=True, reason="清理测试脏数据")

print(result["total_items"], result["created_leaves"])
```

---

## 3. 输入格式兼容

模块自动识别以下两种输入 JSONL 格式：

1. **标准多轮归因格式**：
   ```json
   {"session_meta": {"session_id": "s1", "question": "...", "request_time": "2026-11-24 10:00:00"},
    "turns": [{"meta": {"trace_id": "t1"},
               "problem": [{"problem_id": "p1", "component": "finquery", "severity": "P1",
                            "label": "...", "definition": "...", "reason": "..."}]}]}
   ```
2. **极简扁平单行格式**（方便外部新业务快速接入）：
   ```json
   {"trace_id": "t1", "session_id": "s1", "component": "finquery", "severity": "P1",
    "label": "...", "definition": "...", "reason": "...", "question": "..."}
   ```

---

## 4. 自适应演进机制说明

当 `adaptive.enabled: true`（默认开启）时：

1. **未知组件自适应路由（`auto_component: true`）**：
   出现未在 `components.json` / `component_patterns.json` 中登记的新工具或技能时，模块自动抽取样本结合 `component_role` 调用模型判定其所属责任段（`S1`–`S12`），写回 `<tree_dir>/taxonomy/components.json`。
2. **新叶自适应归纳（`auto_leaf: true`）**：
   归类阶段先走「带本环节合法清单重问」（`path_repair`），优先收敛边界样本；若重问后模型明确判定 `NONE`（现有叶子均不匹配），模块按环节汇总全部未归类条目，归纳符合三轴互斥规范的新 L3 叶子：
   - 序号由代码按 `S<段>.M<机制>.<max+1>` 确定性分配，杜绝编号冲突；
   - 强制校验无“其他/杂项/未知”兜底词、排他项无悬空引用、无重名，通过内置 `taxonomy_lint`（`errors=0`）后才落盘；
   - 自动刷新 `<tree_dir>/taxonomy/INV_<段>.md` 与 `MATRIX.md`，下一轮增量直接作为正式叶子参与归类；
   - 全部新增组件与新增叶子均追加留痕于 `<tree_dir>/taxonomy_evolution.jsonl`，透明可审计。

---

## 5. 输出目录结构

运行完成后，`tree_dir`（或另行指定的 `output_dir`）包含：

| 文件 / 目录 | 说明 |
|---|---|
| `cluster_brief.md` | **一页纸业务摘要**（Top 问题、L4 高优子类、机制层分布、可信度刻度） |
| `cluster_summary.md` | 完整环节/机制/横切标签/L4 明细报告 |
| `cluster_tree.json` / `.txt` | 结构化完整聚类树与终端树形打印 |
| `item_index.jsonl` | 全量条目归属索引（同时作为后续轮次的自包含增量基线） |
| `leaves.tsv` | 叶子统计表（可直接导入 Excel / BI） |
| `syndrome.json` / `.md` | 会话级失效机制共现分析 |
| `cluster_matrix.svg` / `.png` | 环节 × 机制交叉热力图（SVG 恒定产出，PNG 在具备 Pillow+字体时同步产出） |
| `verify.json` / `MANIFEST.json` | 完整性自检报告（必满足 `errors=0`）与产物哈希清单 |
| `taxonomy/` | 与本棵树绑定的分类法副本（含自适应扩充后的叶子与判定卡） |
| `taxonomy_evolution.jsonl` | 自适应新增组件与新增叶子的演进审计账本（发生扩充时生成） |

---

## 6. 自检测试

```bash
python3 -m unittest discover -s cluster_pkg/tests -p "test_*.py" -v
```
