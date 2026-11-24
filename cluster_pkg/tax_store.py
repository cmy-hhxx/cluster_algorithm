# -*- coding: utf-8 -*-
"""自包含分类法存储与演进管理器：加载、历史树对齐、组件登记、新叶写入、lint 自检与判定卡导出。"""
import collections
import copy
import datetime
import json
import os
import re
import shutil
import tempfile

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
BUILTIN_TAX_DIR = os.path.join(PKG_DIR, "builtin_taxonomy")
PROMPTS_DIR = os.path.join(PKG_DIR, "prompts")

MECHS = ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]
VAGUE_WORDS = ("其他", "杂项", "未知", "miscellaneous", "其它")
OLD_REF_RE = re.compile(r"\b([PQA])\.(M[1-7])\.(\d+)\b")
PATH_REF_RE = re.compile(r"\b(S\d+)\.(M[1-7])\.(\d+)\b")

GATE_QUESTIONS = {
    "M1": "这一环该做的动作，一点都没做？",
    "M2": "动作做了，但对象/指标/时段/范围不是用户要的？",
    "M3": "调用报错/超时/零行/空返回/被截断？",
    "M4": "有返回，但内容撑不起任务（不相关/样板/过期/缺字段）？",
    "M5": "数据对，但换算/聚合/口径/比较错，或自相矛盾？",
    "M6": "结论没证据，或与已有证据相反？",
    "M7": "形式与完整性不达标（缺项/格式/语言/答非所问）？",
}


def jload(path, default=None):
    if not os.path.isfile(path):
        return copy.deepcopy(default)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def jdump(obj, path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_prompt_file(name: str) -> str:
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as fh:
        return fh.read()


class TaxonomyStore:
    """管理单个聚类树绑定的分类法副本，支持安全演进与完整自检。"""

    def __init__(self, tax_dir: str):
        self.tax_dir = os.path.abspath(tax_dir)
        self.reload()

    @classmethod
    def initialize(cls, target_tax_dir: str, custom_tax_dir: str = "", existing_tree_dir: str = "") -> "TaxonomyStore":
        dst = os.path.abspath(target_tax_dir)
        os.makedirs(dst, exist_ok=True)

        # 选择种子源：显式 taxonomy_dir > 目标已有 taxonomy > tree_dir/taxonomy > tree_dir 自身就是分类法 > 内置分类法
        src = ""
        if custom_tax_dir and os.path.isfile(os.path.join(custom_tax_dir, "inventory.json")):
            src = os.path.abspath(custom_tax_dir)
        elif os.path.isfile(os.path.join(dst, "inventory.json")):
            src = dst
        elif existing_tree_dir and os.path.isfile(os.path.join(existing_tree_dir, "taxonomy", "inventory.json")):
            src = os.path.abspath(os.path.join(existing_tree_dir, "taxonomy"))
        elif existing_tree_dir and os.path.isfile(os.path.join(existing_tree_dir, "inventory.json")):
            src = os.path.abspath(existing_tree_dir)
        else:
            src = BUILTIN_TAX_DIR

        if os.path.abspath(src) != dst:
            for fn in os.listdir(src):
                sp = os.path.join(src, fn)
                dp = os.path.join(dst, fn)
                if os.path.isfile(sp):
                    shutil.copy2(sp, dp)
        # 补齐可能缺失的辅助表（例如外部轻量 taxonomy 目录未带 component_patterns.json）
        for fn in os.listdir(BUILTIN_TAX_DIR):
            dp = os.path.join(dst, fn)
            if not os.path.exists(dp) and os.path.isfile(os.path.join(BUILTIN_TAX_DIR, fn)):
                shutil.copy2(os.path.join(BUILTIN_TAX_DIR, fn), dp)

        store = cls(dst)
        if existing_tree_dir:
            tree_json = os.path.join(existing_tree_dir, "cluster_tree.json")
            if os.path.isfile(tree_json):
                store.reconcile_from_tree_json(tree_json)
        return store

    def reload(self):
        self.inv = jload(os.path.join(self.tax_dir, "inventory.json"), {"l1": {}, "l2": {}, "nodes": []})
        self.tags = jload(os.path.join(self.tax_dir, "tags.json"), {})
        self.leaf_tags = jload(os.path.join(self.tax_dir, "leaf_tags.json"), {})
        self.disc = jload(os.path.join(self.tax_dir, "discriminators.json"), {})
        self.components = jload(os.path.join(self.tax_dir, "components.json"), {})
        self.comp2l1 = jload(os.path.join(self.tax_dir, "comp2l1.json"), {})
        self.stages = jload(os.path.join(self.tax_dir, "stages.json"), {})
        self.matrix_na = jload(os.path.join(self.tax_dir, "matrix_na.json"), {"cells": {}})
        self.l4_nodes = jload(os.path.join(self.tax_dir, "l4_nodes.json"), {})
        pat_doc = jload(os.path.join(self.tax_dir, "component_patterns.json"), {"rules": []})
        self.patterns = [(r["id"], re.compile(r["match"]), r) for r in (pat_doc.get("rules") or [])]

    def save_all(self, touched_l1=None):
        jdump(self.inv, os.path.join(self.tax_dir, "inventory.json"))
        jdump(self.leaf_tags, os.path.join(self.tax_dir, "leaf_tags.json"))
        jdump(self.disc, os.path.join(self.tax_dir, "discriminators.json"))
        jdump(self.components, os.path.join(self.tax_dir, "components.json"))
        jdump(self.comp2l1, os.path.join(self.tax_dir, "comp2l1.json"))
        jdump(self.stages, os.path.join(self.tax_dir, "stages.json"))
        jdump(self.matrix_na, os.path.join(self.tax_dir, "matrix_na.json"))
        jdump(self.l4_nodes, os.path.join(self.tax_dir, "l4_nodes.json"))
        self.export_inventory_md(only_l1=touched_l1)
        self.export_matrix_md()

    def stage_ids(self):
        return sorted((k for k in self.inv.get("l1", {}) if k != "S0"),
                      key=lambda s: int(s[1:]) if s[1:].isdigit() else 99)

    def node_map(self):
        return {n["id"]: n for n in self.inv["nodes"]}

    def reconcile_from_tree_json(self, tree_json_path: str):
        """从既有交付目录的 cluster_tree.json 吸收自定义 L3/L4 节点，保证跨渠道历史树 100% 兼容。"""
        doc = jload(tree_json_path, {})
        root = (doc or {}).get("tree") or {}
        known_l3 = {n["id"] for n in self.inv["nodes"]}
        existing_names = {n["name"].strip() for n in self.inv["nodes"]}
        touched_l1 = set()
        changed_l4 = False

        for l1_node in root.get("children") or []:
            l1 = l1_node.get("id", "")
            if not l1 or l1 not in self.inv.get("l1", {}):
                continue
            for l2_node in l1_node.get("children") or []:
                l2 = l2_node.get("mechanism") or (l2_node.get("id", "").split(".")[1:] or [""])[0]
                for l3_node in l2_node.get("children") or []:
                    nid = l3_node.get("id", "")
                    if nid and nid not in known_l3 and re.fullmatch(r"S\d+\.M[1-7]\.\d+", nid):
                        nm = (l3_node.get("name") or nid).strip()
                        if nm in existing_names:
                            nm = f"{l1}_{nm}"[:18]
                        comps = [c for c in (l3_node.get("components") or []) if c]
                        if not comps:
                            comps = list((self.stages.get(l1) or {}).get("components") or ["unknown"])[:1]
                        for c in comps:
                            self._ensure_tool_stage(c, l1)
                        self.inv["nodes"].append({
                            "id": nid,
                            "l1": l1,
                            "l2": l2,
                            "name": nm,
                            "plain": l3_node.get("plain") or nm,
                            "definition": l3_node.get("definition") or nm,
                            "include": l3_node.get("include") or nm,
                            "exclude": l3_node.get("exclude") or "非本模式动作越界。",
                            "components": comps,
                        })
                        tgs = [t for t in (l3_node.get("tags") or []) if t in self.tags]
                        self.leaf_tags[nid] = tgs or ["失败"]
                        self.matrix_na.setdefault("cells", {}).pop(f"{l1}.{l2}", None)
                        known_l3.add(nid)
                        existing_names.add(nm)
                        touched_l1.add(l1)
                    for l4_node in l3_node.get("children") or []:
                        cid = l4_node.get("id", "")
                        if cid and cid not in self.l4_nodes:
                            self.l4_nodes[cid] = {
                                "name": l4_node.get("name") or cid.split(".")[-1],
                                "plain": l4_node.get("plain") or "",
                                "definition": l4_node.get("definition") or l4_node.get("name") or cid,
                                "include": l4_node.get("include") or "",
                                "exclude": l4_node.get("exclude") or "",
                            }
                            changed_l4 = True

        if touched_l1 or changed_l4:
            self.save_all(touched_l1=touched_l1 if touched_l1 else None)

    def _ensure_tool_stage(self, tool: str, stage: str):
        cur = str(self.comp2l1.get(tool, "")).strip()
        if not cur:
            self.comp2l1[tool] = stage
        else:
            parts = [p for p in cur.split("+") if p]
            if stage not in parts:
                parts.append(stage)
                parts.sort(key=lambda s: int(s[1:]) if s[1:].isdigit() else 99)
                self.comp2l1[tool] = "+".join(parts)
        st_entry = self.stages.setdefault(stage, {
            "name": self.inv["l1"].get(stage, {}).get("name", stage),
            "definition": self.inv["l1"].get(stage, {}).get("definition", ""),
            "components": [],
        })
        if tool not in st_entry.setdefault("components", []):
            st_entry["components"].append(tool)

    def canon_component(self, raw: str):
        """返回 (tool, label, stage, role, is_registered)。"""
        s = (raw or "").strip()
        if not s:
            return ("", "", "", "", False)
        exact = self.components.get(s)
        if exact:
            tool = exact["tool"]
            stage = exact.get("stage") or self._single_stage(tool)
            return (tool, exact.get("label", ""), stage, exact.get("role", ""), bool(stage))
        for _rid, rx, rule in self.patterns:
            if rx.search(s):
                return (rule["tool"], rule.get("label", ""), rule.get("stage", ""), rule.get("role", ""), True)
        if s in self.comp2l1:
            st = self._single_stage(s)
            if st:
                return (s, s, st, "", True)
        return (s, "", "", "", False)

    def _single_stage(self, tool: str) -> str:
        val = str(self.comp2l1.get(tool, "")).strip()
        if val and "+" not in val:
            return val
        return ""

    def register_component(self, raw: str, tool: str, label: str, role: str, stage: str):
        if stage not in self.inv.get("l1", {}) or stage == "S0":
            raise ValueError(f"非法 stage: {stage!r}")
        tool = (tool or raw).strip()
        label = (label or tool).strip()
        self.components[raw] = {"tool": tool, "label": label, "role": role or "", "stage": stage}
        self._ensure_tool_stage(tool, stage)
        jdump(self.components, os.path.join(self.tax_dir, "components.json"))
        jdump(self.comp2l1, os.path.join(self.tax_dir, "comp2l1.json"))
        jdump(self.stages, os.path.join(self.tax_dir, "stages.json"))

    def load_stage_rules(self, l1: str):
        inv_path = os.path.join(self.tax_dir, f"INV_{l1}.md")
        if not os.path.isfile(inv_path):
            self.export_inventory_md(only_l1={l1})
        rules = load_prompt_file("01_assign.md").replace("{{L1}}", l1)
        with open(inv_path, encoding="utf-8") as fh:
            inv_md = fh.read()
        valid = {n["id"] for n in self.inv["nodes"] if n["l1"] == l1}
        return rules, inv_md, valid

    def l1_menu(self, l1: str):
        nodes = sorted((n for n in self.inv["nodes"] if n["l1"] == l1), key=lambda n: self._id_sort_key(n["id"]))
        mechs = sorted({n["l2"] for n in nodes})
        rows = [
            f"| `{n['id']}` | {n['name']} | {n['l2']} {self.inv['l2'][n['l2']]['name']} |"
            for n in nodes
        ]
        return rows, mechs

    @staticmethod
    def _id_sort_key(nid: str):
        parts = nid.split(".")
        out = []
        for p in parts:
            num = p[1:] if p[:1] in ("S", "M") else p
            out.append(int(num) if num.isdigit() else 999)
        return tuple(out)

    def next_leaf_number(self, l1: str, l2: str) -> int:
        nums = [
            int(n["id"].split(".")[2])
            for n in self.inv["nodes"]
            if n["l1"] == l1 and n["l2"] == l2 and len(n["id"].split(".")) >= 3 and n["id"].split(".")[2].isdigit()
        ]
        return (max(nums) if nums else 0) + 1

    def lint(self, inv=None, leaf_tags=None, disc=None, comp2l1=None, matrix_na=None):
        inv = self.inv if inv is None else inv
        leaf_tags = self.leaf_tags if leaf_tags is None else leaf_tags
        disc = self.disc if disc is None else disc
        comp2l1 = self.comp2l1 if comp2l1 is None else comp2l1
        matrix_na = self.matrix_na if matrix_na is None else matrix_na

        nodes = inv["nodes"]
        ids = [n["id"] for n in nodes]
        idset = set(ids)
        errors, warnings = [], []

        def err(code, msg):
            errors.append({"code": code, "msg": msg})

        def warn(code, msg):
            warnings.append({"code": code, "msg": msg})

        for dup in [i for i, c in collections.Counter(ids).items() if c > 1]:
            err("dup_id", f"重复叶子 id: {dup}")

        for n in nodes:
            nid = n["id"]
            if not re.fullmatch(r"S\d+\.M[1-7]\.\d+", nid):
                err("bad_id", f"{nid}: id 不符合 S<段>.M<机制>.<序号>")
                continue
            l1, l2 = nid.split(".")[:2]
            if n.get("l1") != l1 or n.get("l2") != l2:
                err("axis_mismatch", f"{nid}: l1/l2 字段与 id 不一致")
            if l1 not in inv["l1"] or l1 == "S0":
                err("bad_l1", f"{nid}: 段 {l1} 未定义")
            if l2 not in inv["l2"]:
                err("bad_l2", f"{nid}: 机制 {l2} 未定义")
            if not n.get("components"):
                err("no_component", f"{nid}: components 为空")
            for c in n.get("components", []):
                if c not in comp2l1:
                    err("unregistered_component", f"{nid}: component {c!r} 未登记")
                else:
                    sts = set(str(comp2l1[c]).split("+"))
                    if l1 not in sts:
                        err("component_stage", f"{nid}: component {c!r} 属于 {'+'.join(sorted(sts))}，叶子在 {l1}")
            for k in ("name", "definition", "include", "exclude"):
                if not str(n.get(k, "")).strip():
                    err("empty_field", f"{nid}: {k} 为空")
            nm_low = n.get("name", "").lower()
            if any(v in nm_low or v in n.get("name", "") for v in VAGUE_WORDS):
                err("fallback_bucket", f"{nid}: 名称含兜底词「{n['name']}」")
            if re.search(r"[与或]", n.get("name", "")):
                warn("joined_name", f"{nid}: 名称含「与/或」：{n['name']}")
            if len(n.get("name", "")) > 18:
                warn("long_name", f"{nid}: 名称超 18 字：{n['name']}")

            for k in ("definition", "include", "exclude", "plain"):
                txt = str(n.get(k, "") or "")
                for pre, m, num in OLD_REF_RE.findall(txt):
                    err("stale_ref", f"{nid}.{k}: 旧口径引用 {pre}.{m}.{num}")
                for s_, m_, num_ in PATH_REF_RE.findall(txt):
                    ref = f"{s_}.{m_}.{num_}"
                    if ref not in idset:
                        err("dangling_ref", f"{nid}.{k}: 引用了不存在的叶子 {ref}")
                if re.search(rf"\b{re.escape(nid)}\b", txt):
                    err("self_ref", f"{nid}.{k}: 引用了自身")
                if k == "exclude" and "其他" in txt:
                    err("fallback_exclude", f"{nid}.exclude: 用「其他」当排他项")

        for nid, d in disc.items():
            if nid not in idset:
                err("orphan_disc", f"discriminators 孤儿条目 {nid}")
            near = d.get("near", "")
            if near and near not in idset:
                err("dangling_near", f"{nid}: 最近邻 {near} 不存在")

        byname = collections.defaultdict(list)
        for n in nodes:
            byname[n["name"].strip()].append(n["id"])
        for name, xs in byname.items():
            if len(xs) > 1:
                err("dup_name", f"叶子同名「{name}」：{xs}")

        for nid in idset:
            if nid not in leaf_tags:
                err("missing_tags", f"{nid}: leaf_tags 缺条目")
        for nid in leaf_tags:
            if nid not in idset:
                err("orphan_tags", f"leaf_tags 孤儿条目 {nid}")

        na_cells = (matrix_na or {}).get("cells") or {}
        have = {(n["l1"], n["l2"]) for n in nodes}
        for s_ in [x for x in inv["l1"] if x != "S0"]:
            for m_ in MECHS:
                has, decl = (s_, m_) in have, f"{s_}.{m_}" in na_cells
                if not has and not decl:
                    err("matrix_hole", f"{s_}.{m_}: 无叶子且未在 matrix_na.json 登记理由")
                elif has and decl:
                    err("matrix_stale_na", f"{s_}.{m_}: 已有叶子但 matrix_na.json 仍登记不适用")

        return errors, warnings

    def export_inventory_md(self, only_l1=None):
        for l1, meta in self.inv["l1"].items():
            if l1 == "S0" or (only_l1 and l1 not in only_l1):
                continue
            leaves = sorted([n for n in self.inv["nodes"] if n["l1"] == l1],
                            key=lambda n: self._id_sort_key(n["id"]))
            if not leaves:
                continue
            out = [f"# 环节 {l1}：{meta['name']}", "", meta["definition"], "",
                   "## 0. 判定骨架（按顺序问，命中即停）", ""]
            for m in MECHS:
                ms = [n for n in leaves if n["l2"] == m]
                if ms:
                    out.append(f"{int(m[1])}. **{GATE_QUESTIONS[m]}** → "
                               + " / ".join(f"`{n['id']}` {n['name']}" for n in ms))
            out += ["", "> 同一环节内，先定机制，再选类别。跨机制的类别不互斥，靠本节顺序裁决。", "",
                    "## 1. 类别速查表", "",
                    "| 类别 | 名称 | 机制 | 条目 | 会话 | 标签 | 最相近 |",
                    "|---|---|---|---|---|---|---|"]
            for n in leaves:
                d = self.disc.get(n["id"], {})
                out.append(f"| `{n['id']}` | {n['name']} | {n['l2']} | – | – | "
                           f"{','.join(self.leaf_tags.get(n['id'], []))} | `{d.get('near', '–')}` |")
            out += ["", "## 2. 类别判定卡（用来区分互斥）", ""]
            for n in leaves:
                d = self.disc.get(n["id"], {})
                out += [
                    f"### `{n['id']}` {n['name']}", "",
                    f"- **定义**：{n['definition']}",
                    f"- **可观测信号**：{d.get('sig', '–')}",
                    f"- **纳入**：{n['include']}",
                    f"- **排除**：{n['exclude']}",
                    f"- **最相近的类别**：`{d.get('near', '–')}` — 区分点：{d.get('split', '–')}",
                    f"- **标签**：{','.join(self.leaf_tags.get(n['id'], []))} ｜ **组件**：{'、'.join(n.get('components') or []) or '全部'}",
                    "",
                ]
            seen, pairs = set(), []
            for n in leaves:
                d = self.disc.get(n["id"], {})
                near = d.get("near", "")
                pair = tuple(sorted([n["id"], near]))
                if near and pair not in seen:
                    seen.add(pair)
                    pairs.append((n["id"], near, d.get("split", "")))
            out += ["## 3. 最容易混淆的类别对", "", "| A | B | 一句话区分 |", "|---|---|---|"]
            all_name = {x["id"]: x["name"] for x in self.inv["nodes"]}
            for a_, b_, s in pairs:
                out.append(f"| `{a_}` {all_name.get(a_, '')} | `{b_}` {all_name.get(b_, '')} | {s} |")
            out.append("")
            p = os.path.join(self.tax_dir, f"INV_{l1}.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(out) + "\n")

    def export_matrix_md(self):
        stages = self.stage_ids()
        cells = collections.Counter(".".join(n["id"].split(".")[:2]) for n in self.inv["nodes"])
        na_cells = (self.matrix_na or {}).get("cells") or {}
        empty = {f"{s}.{m}" for s in stages for m in MECHS if cells[f"{s}.{m}"] == 0}
        out = [
            "# 环节 × 机制 覆盖矩阵", "",
            f"更新于 {datetime.date.today().isoformat()}；共 {len(stages)} 环节 × 7 机制 = {len(stages) * 7} 格，"
            f"有叶 {sum(1 for s in stages for m in MECHS if cells[f'{s}.{m}'])} 格，空 {len(empty)} 格。", "",
            "| 环节 | " + " | ".join(MECHS) + " |",
            "|---|" + "---|" * len(MECHS),
        ]
        for s in stages:
            row = [f"{s} {self.inv['l1'][s]['name']}"] + [str(cells[f"{s}.{m}"]) if cells[f"{s}.{m}"] else "·" for m in MECHS]
            out.append("| " + " | ".join(row) + " |")
        out += ["", "## 空格位（不设叶子）及归属", "", "| 格子 | 为什么不需要 |", "|---|---|"]
        for c in sorted(empty):
            out.append(f"| `{c}` | {na_cells.get(c, '')} |")
        with open(os.path.join(self.tax_dir, "MATRIX.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
