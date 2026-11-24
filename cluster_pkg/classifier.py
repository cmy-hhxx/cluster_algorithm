# -*- coding: utf-8 -*-
"""并发分片归类器：支持多轮投票、4 段路径收敛、带清单重问（PATH_REPAIR）以及缺叶条目收集。"""
import collections
import concurrent.futures
import json
import os
import re
import threading
import time

from .llm_client import ResponsesClient, parse_jsonl_rows
from .tax_store import TaxonomyStore


class InvalidClassificationError(RuntimeError):
    def __init__(self, msg, unresolved=None):
        super().__init__(msg)
        self.unresolved = unresolved or []


def build_assign_prompt(l1: str, items, rules: str, inv_md: str) -> str:
    body = "\n".join(
        json.dumps({k: it.get(k, "") for k in ("seq", "component", "label", "definition", "reason")}, ensure_ascii=False)
        for it in items
    )
    return f"""你是归类员。本批共 {len(items)} 条，都已按 component 分到同一个环节；环节不用你判断。

{rules}

## 类别清单（path 必须来自这里）
{inv_md}

## 待判定条目（每行一个 JSON）
{body}

## 输出要求
只输出恰好 {len(items)} 行 JSONL，每行一个对象：{{"seq": <原样带回>, "path": "{l1}.M<k>.<j>", "uncertain": <true|false>}}。
不要解释、不要代码块、不要额外字段。每条都必须有输出，seq 必须与输入一一对应。"""


def _why_rejected(path: str, mechs):
    mech = path.split(".")[1] if path.count(".") >= 1 else "?"
    if mech not in mechs:
        return f"本环节没有 {mech} 这个机制"
    return f"{mech} 在本环节只有清单里那几个序号"


def build_repair_prompt(l1: str, items, rejected: dict, rules: str, tax: TaxonomyStore) -> str:
    menu, mechs = tax.l1_menu(l1)
    missing = [f"M{i}" for i in range(1, 8) if f"M{i}" not in mechs]
    counts = collections.Counter(rejected.values())
    why = "\n".join(
        f"- `{p}`（{n} 条）：不在清单里，{_why_rejected(p, mechs)}"
        for p, n in counts.most_common()
    )
    body = "\n".join(
        json.dumps({k: it.get(k, "") for k in ("seq", "component", "label", "definition", "reason")}, ensure_ascii=False)
        for it in items
    )
    return f"""你是归类员，正在复核一批上一轮判定被退回的条目（共 {len(items)} 条）。

{rules}

## 为什么退回
上一轮给出的 path 不在本环节的合法清单里：

{why}

本环节（{l1}）只定义了这些机制：{", ".join(mechs)}。{("没有 " + "、".join(missing) + "。") if missing else ""}

## 本环节的合法叶子（path 只能逐字取自这里）
| 类别 | 名称 | 机制 |
|---|---|---|
{chr(10).join(menu)}

## 待复核条目（每行一个 JSON）
{body}

## 输出要求
只输出恰好 {len(items)} 行 JSONL，每行一个对象：{{"seq": <原样带回>, "path": "<上面的 id 或 NONE>", "uncertain": <true|false>}}。
- path 必须逐字来自上表，不要自造、不要简写、不要编序号。
- 这批本来就是边界条目，选中后标 `uncertain=true`。
- 只有当你确信上表**所有**叶子都会把它归到错误的机制/类别时，才写 `"path": "NONE"` —— NONE 代表现有分类法缺少这一失效模式，系统将据此自适应建立新叶；若有匹配的既有叶请直接选既有叶。
- 不要解释、不要代码块、不要额外字段。"""


def normalize_rows(rows, valid):
    normalized = 0
    for r in rows:
        p = str(r.get("path", "")).strip().strip("`").strip()
        if p not in valid and p.count(".") == 3:
            parent = p.rsplit(".", 1)[0]
            if parent in valid:
                p = parent
                normalized += 1
        r["path"] = p
    return normalized


def is_valid_batch(rows, items, l1: str, valid) -> bool:
    want = {it["seq"] for it in items}
    got = [r.get("seq") for r in rows]
    if len(got) != len(want) or set(got) != want or len(set(got)) != len(got):
        return False
    pat = re.compile(rf"^{re.escape(l1)}\.M[1-7]\.\d+$")
    for r in rows:
        p = str(r.get("path", ""))
        if not pat.fullmatch(p) or p not in valid:
            return False
    return True


class BatchClassifier:
    def __init__(self, client: ResponsesClient, tax: TaxonomyStore, params, allow_nofit: bool, logger):
        self.client = client
        self.tax = tax
        self.params = params
        self.allow_nofit = allow_nofit
        self.log = logger
        self._lock = threading.Lock()
        self.stats = collections.Counter()

    def _repair_once(self, l1: str, chunk_id: str, items, last_rows, rules: str, valid):
        """返回 (resolved_rows, nofit_records, ok_all_accounted)。"""
        by_seq = {r.get("seq"): str(r.get("path", "")) for r in last_rows if "seq" in r}
        bad_items = [it for it in items if by_seq.get(it["seq"], "") not in valid]
        if not bad_items:
            return None, [], False
        rejected = {it["seq"]: by_seq.get(it["seq"], "") for it in bad_items}
        prompt = build_repair_prompt(l1, bad_items, rejected, rules, self.tax)
        try:
            rows = parse_jsonl_rows(self.client.complete_text(prompt))
            with self._lock:
                self.stats["normalized"] += normalize_rows(rows, valid)
        except Exception as e:
            self.log(f"  {chunk_id}: 重问调用失败 ({e})")
            return None, [], False

        want = {it["seq"] for it in bad_items}
        got = [r.get("seq") for r in rows]
        if len(rows) != len(bad_items) or set(got) != want or len(set(got)) != len(got):
            return None, [], False

        item_by_seq = {it["seq"]: it for it in bad_items}
        fixed, nofit = [], []
        for r in rows:
            seq = r["seq"]
            p = str(r.get("path", "")).strip().strip("`").strip()
            if p.upper() == "NONE":
                nofit.append({
                    "item": item_by_seq[seq],
                    "rejected_path": rejected.get(seq, ""),
                    "reason": "repair_none",
                })
            elif p in valid:
                r["path"] = p
                r["uncertain"] = True
                fixed.append(r)
            elif self.allow_nofit:
                nofit.append({
                    "item": item_by_seq[seq],
                    "rejected_path": p or rejected.get(seq, ""),
                    "reason": "repair_unmapped_path",
                })
            else:
                return None, [], False

        with self._lock:
            self.stats["repaired"] += len(fixed)
            self.stats["nofit"] += len(nofit)

        keep = [
            {"seq": it["seq"], "path": by_seq[it["seq"]], "uncertain": False}
            for it in items
            if it["seq"] not in want and by_seq.get(it["seq"]) in valid
        ]
        return keep + fixed, nofit, True

    def _classify_slice(self, l1: str, chunk_id: str, items, rules: str, inv_md: str, valid, depth: int = 0):
        """返回 (assigned_rows, nofit_records)。"""
        prompt = build_assign_prompt(l1, items, rules, inv_md)
        last_err, last_rows, had_invalid = None, [], False
        for attempt in range(self.params.retries):
            try:
                rows = parse_jsonl_rows(self.client.complete_text(prompt))
                with self._lock:
                    self.stats["normalized"] += normalize_rows(rows, valid)
                if is_valid_batch(rows, items, l1, valid):
                    return rows, []
                had_invalid, last_rows = True, rows
                bad = [str(r.get("path")) for r in rows if str(r.get("path")) not in valid][:3]
                last_err = f"非法或缺失输出 ({len(rows)}/{len(items)} 行, 越界路径 {bad})"
                self.log(f"  {chunk_id}: 第 {attempt + 1} 次尝试 {last_err}")
            except Exception as e:
                last_err = str(e)
            if attempt + 1 < self.params.retries:
                time.sleep(1 + attempt * 2)

        if had_invalid and self.params.path_repair:
            resolved, nofit, ok = self._repair_once(l1, chunk_id, items, last_rows, rules, valid)
            if ok:
                if nofit and not self.allow_nofit:
                    raise InvalidClassificationError(
                        f"{chunk_id}: 存在 {len(nofit)} 条无匹配叶子条目（adaptive.auto_leaf 未开启）",
                        unresolved=nofit,
                    )
                return resolved, nofit

        if depth < 2 and len(items) >= 20:
            mid = len(items) // 2
            self.log(f"  {chunk_id}: 对半拆片 {len(items)} -> {mid}+{len(items) - mid}")
            r1, n1 = self._classify_slice(l1, chunk_id, items[:mid], rules, inv_md, valid, depth + 1)
            r2, n2 = self._classify_slice(l1, chunk_id, items[mid:], rules, inv_md, valid, depth + 1)
            return r1 + r2, n1 + n2

        # 若开启自适应建叶且只有部分行是越界路径（模型坚持索要新叶），保留合法行并把越界行转入建叶池
        if had_invalid and self.allow_nofit and last_rows:
            by_seq = {r.get("seq"): str(r.get("path", "")) for r in last_rows if "seq" in r}
            keep, nofit = [], []
            for it in items:
                p = by_seq.get(it["seq"], "")
                if p in valid:
                    keep.append({"seq": it["seq"], "path": p, "uncertain": False})
                else:
                    nofit.append({"item": it, "rejected_path": p, "reason": "persistent_invalid_path"})
            with self._lock:
                self.stats["nofit"] += len(nofit)
            return keep, nofit

        raise InvalidClassificationError(f"{chunk_id}: 归类失败 ({last_err})")

    def classify_all(self, todo_items, chunk_size: int):
        """按 L1 分片并发归类，返回 (assignments_dict[seq -> (path, edge)], nofit_by_l1[l1 -> list])。"""
        if not todo_items:
            return {}, {}

        by_l1 = collections.defaultdict(list)
        for it in todo_items:
            by_l1[it["stage"]].append(it)

        chunks = []
        for l1 in sorted(by_l1, key=lambda s: int(s[1:]) if s[1:].isdigit() else 99):
            rows = sorted(by_l1[l1], key=lambda x: x["seq"])
            for i in range(0, len(rows), chunk_size):
                cid = f"{l1}_c{i // chunk_size + 1:03d}"
                chunks.append((cid, l1, rows[i: i + chunk_size]))

        self.log(f"待归类 {len(todo_items)} 条 -> {len(chunks)} 个分片（并发 {self.params.concurrency}）")

        assigned = {}
        nofit_by_l1 = collections.defaultdict(list)
        errors = []

        def worker(job):
            cid, l1, part = job
            rules, inv_md, valid = self.tax.load_stage_rules(l1)
            t0 = time.time()
            passes, all_nofit = [], []
            for p_idx in range(self.params.consensus):
                rows, nf = self._classify_slice(l1, cid, part, rules, inv_md, valid)
                passes.append(rows)
                if p_idx == 0:
                    all_nofit = nf
            nofit_seqs = {x["item"]["seq"] for x in all_nofit}
            if len(passes) == 1:
                out = [
                    (r["seq"], r["path"], bool(r.get("uncertain", r.get("edge", False))))
                    for r in passes[0] if r["seq"] not in nofit_seqs
                ]
            else:
                votes = collections.defaultdict(list)
                for rows in passes:
                    for r in rows:
                        if r["seq"] not in nofit_seqs:
                            votes[r["seq"]].append(r["path"])
                out = []
                for seq, paths in votes.items():
                    cnt = collections.Counter(paths)
                    best = max(cnt, key=lambda p: (cnt[p], p == paths[0]))
                    out.append((seq, best, len(cnt) > 1))
            self.log(f"  ✓ {cid}: 归类 {len(out)} 条"
                     + (f"，待建新叶 {len(all_nofit)} 条" if all_nofit else "")
                     + f" ({time.time() - t0:.1f}s)")
            return l1, out, all_nofit

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.params.concurrency) as pool:
            futs = {pool.submit(worker, c): c for c in chunks}
            for f in concurrent.futures.as_completed(futs):
                cid, _l1, _part = futs[f]
                try:
                    l1, out, nf = f.result()
                    for seq, path, edge in out:
                        assigned[seq] = (path, edge)
                    if nf:
                        nofit_by_l1[l1].extend(nf)
                    self.stats["chunks_ok"] += 1
                except Exception as e:
                    errors.append((cid, str(e)))
                    self.stats["chunks_failed"] += 1
                    self.log(f"  ✗ {cid}: 失败 {e}")

        if errors:
            detail = "; ".join(f"{c}: {e}" for c, e in errors[:5])
            raise RuntimeError(f"共 {len(errors)} 个分片归类失败: {detail}")

        return assigned, dict(nofit_by_l1)
