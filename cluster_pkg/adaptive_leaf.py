# -*- coding: utf-8 -*-
"""未知组件路由与缺失新叶自适应归纳器（内置分类法 lint 门禁与演进留痕）。"""
import copy
import datetime
import json
import os
import re

from .llm_client import ResponsesClient, extract_json_object, parse_jsonl_rows
from .tax_store import MECHS, OLD_REF_RE, PATH_REF_RE, VAGUE_WORDS, TaxonomyStore, load_prompt_file

ROLE_STAGE_FALLBACK = {
    "request_generation": "S10",
    "delivery": "S9",
    "script": "S4",
    "skill": "S8",
    "planning": "S12",
    "tool": "S3",
}

DEFAULT_TAG_BY_MECH = {
    "M1": "缺失",
    "M2": "实体",
    "M3": "失败",
    "M4": "缺失",
    "M5": "口径",
    "M6": "编造",
    "M7": "呈现",
}


def _append_evolution(log_path: str, record: dict):
    if not log_path:
        return
    rec = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), **record}
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def resolve_unknown_components(
    unknown_samples: dict,
    client: ResponsesClient,
    tax: TaxonomyStore,
    evolution_log_path: str,
    logger,
):
    """对未登记 component 调用模型路由责任段并持久化写入分类法。"""
    if not unknown_samples:
        return []

    stages_lines = ["| 段 | 名称 | 定义 | 代表工具 |", "|---|---|---|---|"]
    for s in tax.stage_ids():
        meta = tax.inv["l1"][s]
        tools = "、".join((tax.stages.get(s) or {}).get("components", [])[:5])
        stages_lines.append(f"| `{s}` | {meta['name']} | {meta['definition']} | {tools} |")

    unknown_lines = [
        json.dumps({"raw": raw, "samples": samples}, ensure_ascii=False)
        for raw, samples in sorted(unknown_samples.items())
    ]
    prompt = (
        load_prompt_file("adaptive_component.md")
        .replace("{{STAGES_TABLE}}", "\n".join(stages_lines))
        .replace("{{UNKNOWN_BODY}}", "\n".join(unknown_lines))
    )

    valid_stages = set(tax.stage_ids())
    routed = {}
    for attempt in range(2):
        try:
            rows = parse_jsonl_rows(client.complete_text(prompt))
            for r in rows:
                raw = str(r.get("raw") or "").strip()
                st = str(r.get("stage") or "").strip()
                if raw in unknown_samples and st in valid_stages:
                    routed[raw] = {
                        "raw": raw,
                        "tool": str(r.get("tool") or raw).strip() or raw,
                        "label": str(r.get("label") or raw).strip()[:20] or raw,
                        "role": str(r.get("role") or "").strip(),
                        "stage": st,
                    }
            if len(routed) == len(unknown_samples):
                break
        except Exception as e:
            logger(f"  WARN 组件路由第 {attempt + 1} 次调用异常: {e}")

    created = []
    for raw, samples in sorted(unknown_samples.items()):
        info = routed.get(raw)
        if not info:
            sample_role = (samples[0].get("component_role") if samples else "") or ""
            st = ROLE_STAGE_FALLBACK.get(sample_role, "S3")
            info = {"raw": raw, "tool": raw, "label": raw[:20], "role": sample_role, "stage": st}
        tax.register_component(info["raw"], info["tool"], info["label"], info["role"], info["stage"])
        _append_evolution(evolution_log_path, {"type": "component_registered", **info})
        created.append(info)
        logger(f"  + 自适应登记组件 `{raw}` -> tool={info['tool']} stage={info['stage']}")
    return created


def _sanitize_leaf_name(raw_name: str, l1: str, stage_name: str, existing_names: set) -> str:
    nm = re.sub(r"[`\"'\s]+", "", str(raw_name or "").strip())
    for w in VAGUE_WORDS:
        nm = nm.replace(w, "").replace(w.capitalize(), "")
    nm = nm.replace("与", "").replace("或", "")
    if not nm:
        nm = f"{stage_name[:4]}特定失效"
    nm = nm[:18]
    if nm in existing_names:
        cand = f"{stage_name[:4]}{nm}"[:18]
        idx = 2
        while cand in existing_names:
            suffix = str(idx)
            cand = f"{stage_name[:4]}{nm[: max(1, 18 - 4 - len(suffix))]}{suffix}"
            idx += 1
        nm = cand
    return nm


def _clean_refs(text: str, self_id: str, valid_all_ids: set, fallback_near: str, is_exclude: bool = False) -> str:
    s = str(text or "").strip()
    s = OLD_REF_RE.sub("", s)
    if is_exclude:
        s = s.replace("其他", "其余同段既有类别").replace("其它", "其余同段既有类别")

    def _fix_path(m):
        ref = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
        if ref == self_id or ref not in valid_all_ids:
            return fallback_near if fallback_near and fallback_near != self_id else ""
        return ref

    s = PATH_REF_RE.sub(_fix_path, s)
    s = re.sub(rf"\b{re.escape(self_id)}\b", "", s).strip()
    if not s:
        if is_exclude and fallback_near:
            return f"不属于本叶动作边界的情形 → `{fallback_near}`。"
        return "见本类别定义与可观测信号。"
    return s


def induct_and_assign_missing_leaves(
    nofit_by_l1: dict,
    client: ResponsesClient,
    tax: TaxonomyStore,
    evolution_log_path: str,
    logger,
):
    """对各环节未归类条目归纳互斥新叶，经 taxonomy_lint 零错误门禁后入库并返回补充归类结果。"""
    resolved_assignments = {}
    created_leaves = []

    for l1 in sorted(nofit_by_l1, key=lambda s: int(s[1:]) if s[1:].isdigit() else 99):
        records = nofit_by_l1[l1]
        if not records:
            continue
        items = [r["item"] for r in records]
        rejected_by_seq = {r["item"]["seq"]: r.get("rejected_path", "") for r in records}
        l1_meta = tax.inv["l1"][l1]
        existing_l1_nodes = sorted(
            [n for n in tax.inv["nodes"] if n["l1"] == l1],
            key=lambda n: tax._id_sort_key(n["id"]),
        )
        near_example = existing_l1_nodes[0]["id"] if existing_l1_nodes else f"{l1}.M3.1"

        existing_lines = ["| 叶子 id | 机制 | 名称 | 定义 | 排除 |", "|---|---|---|---|---|"]
        for n in existing_l1_nodes:
            existing_lines.append(
                f"| `{n['id']}` | {n['l2']} | {n['name']} | {n['definition']} | {n['exclude']} |"
            )

        nofit_lines = [
            json.dumps(
                {
                    "seq": it["seq"],
                    "component": it.get("component", ""),
                    "label": it.get("label", ""),
                    "definition": it.get("definition", ""),
                    "reason": it.get("reason", ""),
                    "prev_rejected": rejected_by_seq.get(it["seq"], ""),
                },
                ensure_ascii=False,
            )
            for it in items
        ]

        base_prompt = (
            load_prompt_file("adaptive_leaf.md")
            .replace("{{L1}}", l1)
            .replace("{{L1_NAME}}", l1_meta["name"])
            .replace("{{L1_DEF}}", l1_meta["definition"])
            .replace("{{EXISTING_LEAVES}}", "\n".join(existing_lines))
            .replace("{{ALLOWED_TAGS}}", ", ".join(tax.tags.keys()))
            .replace("{{N}}", str(len(items)))
            .replace("{{NOFIT_ITEMS}}", "\n".join(nofit_lines))
            .replace("{{NEAR_EXAMPLE}}", near_example)
        )

        want_seqs = {it["seq"] for it in items}
        item_by_seq = {it["seq"]: it for it in items}
        existing_l1_ids = {n["id"] for n in existing_l1_nodes}

        last_err = ""
        committed_for_l1 = False
        prompt = base_prompt

        for attempt in range(2):
            try:
                obj = extract_json_object(client.complete_text(prompt))
                raw_leaves = obj.get("new_leaves") or []
                raw_assigns = obj.get("assignments") or []

                # 预拷贝分类法，所有变更先过 lint 门禁再落盘
                st_inv = copy.deepcopy(tax.inv)
                st_leaf_tags = copy.deepcopy(tax.leaf_tags)
                st_disc = copy.deepcopy(tax.disc)
                st_comp2l1 = copy.deepcopy(tax.comp2l1)
                st_matrix_na = copy.deepcopy(tax.matrix_na)

                existing_names = {n["name"].strip() for n in st_inv["nodes"]}
                all_ids = {n["id"] for n in st_inv["nodes"]}

                # 第一遍：为每个候选新叶确定唯一合法的 S<l1>.M<k>.<num>
                next_counter = {}
                key_to_id = {}
                staged_specs = []
                for idx, spec in enumerate(raw_leaves, 1):
                    if not isinstance(spec, dict):
                        continue
                    tkey = str(spec.get("key") or f"NEW_{idx}").strip()
                    l2 = str(spec.get("l2") or "").strip().upper()
                    if l2 not in MECHS:
                        l2 = "M3"
                    cur_max = next_counter.get(l2)
                    if cur_max is None:
                        nums = [
                            int(n["id"].split(".")[2])
                            for n in st_inv["nodes"]
                            if n["l1"] == l1 and n["l2"] == l2 and n["id"].split(".")[2].isdigit()
                        ]
                        cur_max = max(nums) if nums else 0
                    cur_max += 1
                    next_counter[l2] = cur_max
                    nid = f"{l1}.{l2}.{cur_max}"
                    key_to_id[tkey] = nid
                    all_ids.add(nid)
                    staged_specs.append((tkey, nid, l2, spec))

                def _replace_temp_keys(s: str) -> str:
                    out_s = str(s or "")
                    for tk, real_id in sorted(key_to_id.items(), key=lambda kv: -len(kv[0])):
                        out_s = out_s.replace(tk, real_id)
                    return out_s

                # 解析条目归属，收集每个新叶实际承接的条目与组件
                seq_to_target = {}
                seqs_by_leaf = {}
                for a in raw_assigns:
                    if not isinstance(a, dict) or "seq" not in a:
                        continue
                    sq = a["seq"]
                    if sq not in want_seqs:
                        continue
                    tgt = _replace_temp_keys(str(a.get("target") or a.get("path") or "").strip().strip("`"))
                    if tgt in existing_l1_ids or tgt in key_to_id.values():
                        seq_to_target[sq] = (tgt, bool(a.get("edge", False)))
                        seqs_by_leaf.setdefault(tgt, []).append(sq)

                if set(seq_to_target.keys()) != want_seqs:
                    missing_sq = sorted(want_seqs - set(seq_to_target.keys()))
                    raise ValueError(f"assignments 未覆盖全部条目，缺失 seq={missing_sq}")

                # 仅保留真正被分配了条目的新叶，避免空叶子污染分类法
                used_new_ids = {p for p, _ in seq_to_target.values() if p in key_to_id.values()}
                valid_all_after = {n["id"] for n in st_inv["nodes"]} | used_new_ids

                stage_created = []
                for _tkey, nid, l2, spec in staged_specs:
                    if nid not in used_new_ids:
                        continue
                    nm = _sanitize_leaf_name(spec.get("name", ""), l1, l1_meta["name"], existing_names)
                    existing_names.add(nm)

                    raw_near = _replace_temp_keys(spec.get("near", "")).strip().strip("`")
                    if raw_near not in valid_all_after or raw_near == nid:
                        same_m = [n["id"] for n in existing_l1_nodes if n["l2"] == l2]
                        raw_near = same_m[0] if same_m else (existing_l1_nodes[0]["id"] if existing_l1_nodes else "")

                    defn = _clean_refs(_replace_temp_keys(spec.get("definition", "")), nid, valid_all_after, raw_near)
                    inc = _clean_refs(_replace_temp_keys(spec.get("include", "")), nid, valid_all_after, raw_near)
                    exc = _clean_refs(
                        _replace_temp_keys(spec.get("exclude", "")), nid, valid_all_after, raw_near, is_exclude=True
                    )
                    plain = _clean_refs(_replace_temp_keys(spec.get("plain", nm)), nid, valid_all_after, raw_near)[:60]

                    leaf_seqs = seqs_by_leaf.get(nid, [])
                    comps = sorted({item_by_seq[s]["component"] for s in leaf_seqs if item_by_seq[s].get("component")})
                    if not comps:
                        comps = list((tax.stages.get(l1) or {}).get("components") or [])[:1]
                    for c in comps:
                        cur_c = str(st_comp2l1.get(c, "")).strip()
                        if not cur_c:
                            st_comp2l1[c] = l1
                        elif l1 not in cur_c.split("+"):
                            parts = sorted(set(cur_c.split("+")) | {l1},
                                           key=lambda x: int(x[1:]) if x[1:].isdigit() else 99)
                            st_comp2l1[c] = "+".join(parts)

                    node_obj = {
                        "id": nid,
                        "l1": l1,
                        "l2": l2,
                        "name": nm,
                        "plain": plain,
                        "definition": defn,
                        "include": inc,
                        "exclude": exc,
                        "components": comps,
                    }
                    st_inv["nodes"].append(node_obj)

                    tgs = [t for t in (spec.get("tags") or []) if t in tax.tags]
                    st_leaf_tags[nid] = tgs or [DEFAULT_TAG_BY_MECH.get(l2, "失败")]

                    if raw_near:
                        st_disc[nid] = {
                            "sig": str(spec.get("sig") or inc)[:120],
                            "near": raw_near,
                            "split": _clean_refs(_replace_temp_keys(spec.get("split", exc)), nid, valid_all_after, raw_near)[:120],
                        }
                    st_matrix_na.setdefault("cells", {}).pop(f"{l1}.{l2}", None)
                    stage_created.append((node_obj, st_leaf_tags[nid], st_disc.get(nid, {}), leaf_seqs))

                # 分类法强自检门禁：任何新叶都不得引入 lint error
                lint_errs, _lint_warns = tax.lint(
                    inv=st_inv, leaf_tags=st_leaf_tags, disc=st_disc, comp2l1=st_comp2l1, matrix_na=st_matrix_na
                )
                if lint_errs:
                    raise ValueError(f"新叶未通过 taxonomy_lint 校验: {lint_errs[:3]}")

                # 通过门禁：正式提交至 TaxonomyStore 并刷新 INV_<L1>.md 与 MATRIX.md
                tax.inv = st_inv
                tax.leaf_tags = st_leaf_tags
                tax.disc = st_disc
                tax.comp2l1 = st_comp2l1
                tax.matrix_na = st_matrix_na
                for node_obj, _tgs, _dc, _lseqs in stage_created:
                    for c in node_obj["components"]:
                        tax._ensure_tool_stage(c, l1)
                tax.save_all(touched_l1={l1})

                for node_obj, tgs, dc, lseqs in stage_created:
                    ev = {
                        "type": "leaf_created",
                        "id": node_obj["id"],
                        "l1": l1,
                        "l2": node_obj["l2"],
                        "name": node_obj["name"],
                        "plain": node_obj["plain"],
                        "definition": node_obj["definition"],
                        "include": node_obj["include"],
                        "exclude": node_obj["exclude"],
                        "tags": tgs,
                        "near": dc.get("near", ""),
                        "trigger_count": len(lseqs),
                        "trigger_traces": [item_by_seq[s]["trace_id"] for s in lseqs[:10]],
                    }
                    _append_evolution(evolution_log_path, ev)
                    created_leaves.append(ev)
                    logger(f"  + 自适应新建叶子 `{node_obj['id']}`「{node_obj['name']}」（承接 {len(lseqs)} 条）")

                resolved_assignments.update(seq_to_target)
                committed_for_l1 = True
                break
            except Exception as e:
                last_err = str(e)
                logger(f"  WARN {l1} 自适应建叶第 {attempt + 1} 次尝试未通过: {last_err}")
                prompt = base_prompt + f"\n\n注意：上一轮输出校验失败（原因：{last_err}），请严格修正后只输出合法 JSON。"

        if not committed_for_l1:
            raise RuntimeError(f"环节 {l1} 自适应建叶失败 ({len(items)} 条未归类): {last_err}")

    return resolved_assignments, created_leaves
