#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIME PG 16 + Multi-Frame Zstd 原生聚类流水线。

以 PostgreSQL 16 `eval_problems` 为唯一状态真源，不再扫旧 .jsonl、不再维护本地 run_merged 基线：
  - status       查看各模型在 PG 的绑定进度、当前发版树与 5003 健康状态
  - incremental  拉取 cluster_code IS NULL 增量 → Zstd pread 补 definition/reason
                 → L3 分类 → L4 下钻 → 分批 POST /api/v1/data-core/clusters/bind
                 → 命中新叶或到达每日窗口时自动触发整树发版
  - publish      流式聚合 PG 全量最新有效 eval_problems → 生成 10 件交付物并自检
                 → 调用 pg_data_core.writers publish-cluster 发版与切 current 软链
  - backfill-l4  针对指定 L3 叶子从 PG 补跑 L4 下钻并回填
  - recall       按节点 id 直接从 PG + Zstd pread 召回原始样本
"""
from __future__ import annotations

import argparse
import collections
import datetime
import fcntl
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import classify as CL  # noqa: E402
import cluster as C  # noqa: E402

DEFAULT_PG_CORE_PATH = os.environ.get("PG_DATA_CORE_PATH", "/opt/pg_data_core")
DEFAULT_BIND_URL = os.environ.get(
    "DATA_CORE_BIND_URL",
    "http://127.0.0.1:5003/api/v1/data-core/clusters/bind",
)
DEFAULT_HEALTH_URL = os.environ.get(
    "DATA_CORE_HEALTH_URL",
    "http://127.0.0.1:5003/api/v1/data-core/health",
)
DEFAULT_L4_LEAVES = os.environ.get(
    "CLUSTER_L4_LEAVES",
    "S9.M6.4,S3.M3.2,S9.M7.4,S9.M6.1,S9.M5.1,S9.M5.4,S9.M5.2,S9.M5.3,S9.M6.2,S9.M6.3,S9.M2.1,S9.M2.2",
)
DEFAULT_MODELS = os.environ.get("CLUSTER_MODELS", "aime_fast,aime_expert")
DELIVERABLE_MANIFEST_FILES = [
    "cluster_tree.json",
    "cluster_tree.txt",
    "cluster_summary.md",
    "cluster_brief.md",
    "cluster_matrix.png",
    "syndrome.md",
    "syndrome.json",
    "leaves.tsv",
]


# ---------------------------------------------------------------- helpers
def load_version_doc() -> dict[str, Any]:
    return C.jload(os.path.join(ROOT, "VERSION.json"), {}) or {}


def pipeline_version() -> str:
    return str(load_version_doc().get("version") or "unknown")


def now_local() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def fmt_iso_utc(val: Any) -> str:
    """规范化为 UTC ISO-8601（秒精度），保证 PG 侧 data_cutoff 字符串 GREATEST 单调可比。"""
    if val is None:
        return ""
    if isinstance(val, datetime.datetime):
        dt = val.astimezone(datetime.timezone.utc) if val.tzinfo else val.replace(tzinfo=datetime.timezone.utc)
        return dt.replace(microsecond=0).isoformat()
    if isinstance(val, datetime.date):
        return f"{val.isoformat()}T00:00:00+00:00"
    s = str(val).strip()
    if not s:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt.replace(microsecond=0).isoformat()
    except ValueError:
        return s


def fmt_date_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.isoformat()[:10]
    return str(val)[:10]


def sha16(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def parse_csv_arg(val: str) -> list[str]:
    return [x.strip() for x in (val or "").split(",") if x.strip()]


def extract_problem_from_attr(doc: Any, scope: str, turn_index: int, problem_index: int) -> dict[str, Any]:
    """按 (scope, turn_index, problem_index) 从解压后的归因记录精准取回原 problem 字典。"""
    if not isinstance(doc, dict):
        return {}
    if scope == "session":
        probs = doc.get("session_problem")
        if isinstance(probs, list) and 0 <= problem_index < len(probs) and isinstance(probs[problem_index], dict):
            return probs[problem_index]
        return {}
    turns = doc.get("turns")
    if isinstance(turns, list) and 0 <= turn_index < len(turns) and isinstance(turns[turn_index], dict):
        probs = C.as_list(turns[turn_index].get("problem"))
        if 0 <= problem_index < len(probs) and isinstance(probs[problem_index], dict):
            return probs[problem_index]
    return {}


def hydrate_problems_from_zst(rows: list[dict[str, Any]], pread_fn) -> dict[int, dict[str, Any]]:
    """按 (attr_rel, attr_offset, attr_len) 分组做 O(1) Zstd pread，每条 trace 最多解压一次。"""
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        rel = r.get("attr_rel")
        off = r.get("attr_offset")
        length = r.get("attr_len")
        if rel and off is not None and length:
            groups[(str(rel), int(off), int(length))].append(r)

    out: dict[int, dict[str, Any]] = {}
    for (rel, off, length), grp in groups.items():
        try:
            doc = pread_fn(rel, off, length)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN Zstd pread 失败 {rel}@{off}+{length}: {exc}", file=sys.stderr)
            continue
        for r in grp:
            prob = extract_problem_from_attr(
                doc,
                str(r.get("scope") or "turn"),
                int(r.get("turn_index") or 0),
                int(r.get("problem_index") or 0),
            )
            if prob:
                out[int(r["problem_id"])] = prob
    return out


def http_json(url: str, payload: dict[str, Any] | None = None, *, timeout: int = 120, attempts: int = 3) -> dict[str, Any]:
    """直连本机 5003 HTTP 接口（绕过任何环境代理，含瞬时故障重试）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Accept": "application/json"}
    token = os.environ.get("DATA_CORE_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    last_err: Exception | None = None
    for i in range(max(1, attempts)):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            last_err = RuntimeError(f"HTTP {exc.code} {url}: {body}")
            if exc.code < 500 and exc.code != 429:
                raise last_err from exc
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        if i + 1 < attempts:
            time.sleep(min(2 ** i, 8))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_err}") from last_err


def append_ledger(row: dict[str, Any]) -> None:
    ledger_path = os.path.join(ROOT, "runs", "_runs.jsonl")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def repair_log_count(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def repair_log_delta(path: str, base_lines: int) -> tuple[int, int]:
    if not path or not os.path.exists(path):
        return 0, 0
    ok = nofit = 0
    with open(path, encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            if idx < base_lines or not line.strip():
                continue
            try:
                outcome = json.loads(line).get("outcome")
            except Exception:
                continue
            if outcome == "repaired":
                ok += 1
            elif outcome == "none":
                nofit += 1
    return ok, nofit


def acquire_lock():
    """与旧流水线共用 runs/.lock，避免守护进程与手动命令并发踩踏。"""
    lock_path = os.path.join(ROOT, "runs", ".lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


# ---------------------------------------------------------------- PG backend
class PGBackend:
    """对 /opt/pg_data_core 的只读访问与 CLI 封装（懒加载，便于本地无 PG 单测）。"""

    def __init__(
        self,
        *,
        pg_core_path: str = DEFAULT_PG_CORE_PATH,
        data_root: str | None = None,
        bind_url: str = DEFAULT_BIND_URL,
        health_url: str = DEFAULT_HEALTH_URL,
    ):
        self.pg_core_path = pg_core_path
        self.data_root_override = data_root or os.environ.get("AIME_DATA_ROOT") or None
        self.bind_url = bind_url
        self.health_url = health_url
        self._cfg = None
        self._connect_direct = None
        self._pread_json = None

    def _ensure_loaded(self) -> None:
        if self._cfg is not None:
            return
        if self.pg_core_path and self.pg_core_path not in sys.path:
            sys.path.insert(0, self.pg_core_path)
        from pg_data_core.config import load_config  # type: ignore[import-not-found]
        from pg_data_core.db import connect_direct  # type: ignore[import-not-found]
        from pg_data_core.zst_pack import pread_json  # type: ignore[import-not-found]

        self._cfg = load_config(role="ro", data_root=self.data_root_override)
        self._connect_direct = connect_direct
        self._pread_json = pread_json

    @property
    def data_root(self) -> Path:
        self._ensure_loaded()
        return self._cfg.data_root

    def connect(self, app_name: str = "aime_cluster"):
        self._ensure_loaded()
        return self._connect_direct(self._cfg, app_name=app_name)

    def pread_attr(self, rel_path: str, offset: int, length: int) -> Any:
        self._ensure_loaded()
        return self._pread_json(self._cfg.data_root / rel_path, offset, length)

    def discover_models(self) -> list[str]:
        with self.connect("aime_cluster_models") as conn:
            rows = conn.execute(
                "SELECT DISTINCT model FROM public.eval_problems ORDER BY model"
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT DISTINCT model FROM public.eval_inputs WHERE NOT is_deleted ORDER BY model"
                ).fetchall()
        return [str(r["model"]) for r in rows if r.get("model")]

    def current_tree_info(self, model: str) -> dict[str, Any] | None:
        with self.connect("aime_cluster_tree_info") as conn:
            row = conn.execute(
                """
                SELECT tree_id, model, version, release_tag, total_items, total_traces,
                       total_sessions, leaf_count, data_cutoff, generated_at, artifact_rel_dir
                FROM public.cluster_trees
                WHERE product = 'aime' AND model = %s AND is_current = TRUE
                LIMIT 1
                """,
                (model,),
            ).fetchone()
            if not row:
                return None
            nodes = conn.execute(
                "SELECT code FROM public.cluster_nodes WHERE tree_id = %s AND level >= 3",
                (row["tree_id"],),
            ).fetchall()
            info = dict(row)
            info["node_codes"] = {str(n["code"]) for n in nodes}
            return info

    def latest_clustered_at(self, model: str) -> datetime.datetime | None:
        with self.connect("aime_cluster_latest_ts") as conn:
            row = conn.execute(
                """
                SELECT MAX(p.clustered_at) AS max_ts
                FROM public.eval_problems p
                JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
                JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
                WHERE p.model = %s AND p.cluster_code IS NOT NULL
                """,
                (model,),
            ).fetchone()
            return row["max_ts"] if row else None

    def model_counts(self, model: str) -> dict[str, int]:
        with self.connect("aime_cluster_counts") as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(p.cluster_code) AS bound,
                    COUNT(*) FILTER (WHERE p.cluster_code IS NULL) AS unclustered,
                    COUNT(*) FILTER (WHERE p.l4_subleaf IS NOT NULL) AS l4_bound
                FROM public.eval_problems p
                JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
                JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
                WHERE p.model = %s
                """,
                (model,),
            ).fetchone()
        return {k: int(row[k] or 0) for k in ("total", "bound", "unclustered", "l4_bound")}

    def fetch_unclustered(self, model: str, limit: int = 5000) -> list[dict[str, Any]]:
        sql = """
            SELECT
                p.problem_id, p.output_id, p.input_id, p.model, p.biz_day,
                p.trace_id, p.scope, p.turn_index, p.problem_index,
                p.severity, p.component, p.component_family, p.label,
                o.attr_offset, o.attr_len, pf.rel_path AS attr_rel,
                i.session_id, i.user_id, i.batch_id, i.case_id,
                i.request_time, i.question_text
            FROM public.eval_problems p
            JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
            JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
            LEFT JOIN public.pack_files pf ON pf.file_id = o.attr_file_id
            WHERE p.model = %s
              AND p.cluster_code IS NULL
            ORDER BY p.problem_id ASC
        """
        params: list[Any] = [model]
        if limit and limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self.connect("aime_cluster_fetch_delta") as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    def fetch_l4_candidates(
        self,
        model: str,
        leaves: list[str],
        *,
        force: bool = False,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT
                p.problem_id, p.output_id, p.input_id, p.model, p.biz_day,
                p.trace_id, p.scope, p.turn_index, p.problem_index,
                p.severity, p.component, p.component_family, p.label,
                p.cluster_code, p.l3_leaf, p.l4_subleaf, p.is_edge,
                o.attr_offset, o.attr_len, pf.rel_path AS attr_rel,
                i.session_id, i.user_id, i.batch_id, i.case_id,
                i.request_time, i.question_text
            FROM public.eval_problems p
            JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
            JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
            LEFT JOIN public.pack_files pf ON pf.file_id = o.attr_file_id
            WHERE p.model = %s
              AND p.l3_leaf = ANY(%s)
              AND (%s OR p.l4_subleaf IS NULL)
            ORDER BY p.problem_id ASC
        """
        params: list[Any] = [model, leaves, bool(force)]
        if limit and limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self.connect("aime_cluster_fetch_l4") as conn:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    def sessions_all_count(self, model: str) -> int:
        with self.connect("aime_cluster_sessions_all") as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT COALESCE(NULLIF(btrim(i.session_id), ''), i.trace_id)) AS cnt
                FROM public.eval_outputs o
                JOIN public.eval_inputs i ON i.input_id = o.input_id AND NOT i.is_deleted
                WHERE o.model = %s AND o.is_latest = TRUE
                """,
                (model,),
            ).fetchone()
        return int(row["cnt"] or 0) if row else 0

    def iter_bound_problems(self, model: str, itersize: int = 10000):
        """流式遍历某模型全部最新有效已绑定问题（服务端游标，避免全量驻留内存）。"""
        sql = """
            SELECT
                p.problem_id, p.biz_day, p.trace_id, p.scope, p.turn_index, p.problem_index,
                p.severity, p.component, p.label,
                p.cluster_code, p.is_edge, p.tags,
                o.attr_offset, o.attr_len, pf.rel_path AS attr_rel,
                i.session_id, i.user_id, i.batch_id, i.case_id,
                i.request_time, i.question_text
            FROM public.eval_problems p
            JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
            JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
            LEFT JOIN public.pack_files pf ON pf.file_id = o.attr_file_id
            WHERE p.model = %s
              AND p.cluster_code IS NOT NULL
            ORDER BY p.problem_id ASC
        """
        with self.connect("aime_cluster_stream_agg") as conn:
            with conn.cursor(name=f"agg_{model}_{os.getpid()}") as cur:
                cur.itersize = itersize
                cur.execute(sql, (model,))
                for row in cur:
                    yield dict(row)

    def fetch_recall_rows(self, model: str, node: str, limit: int = 5) -> tuple[int, int, list[dict[str, Any]]]:
        with self.connect("aime_cluster_recall") as conn:
            if node == "root":
                cond = "p.cluster_code IS NOT NULL"
                params: tuple[Any, ...] = (model,)
            else:
                cond = "(p.cluster_code = %s OR p.cluster_code LIKE %s)"
                params = (model, node, f"{node}.%")
            stat = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS matched,
                    COUNT(DISTINCT COALESCE(NULLIF(btrim(i.session_id), ''), p.trace_id)) AS sessions
                FROM public.eval_problems p
                JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
                JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
                WHERE p.model = %s AND {cond}
                """,
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT
                    p.problem_id, p.biz_day, p.trace_id, p.scope, p.turn_index, p.problem_index,
                    p.severity, p.component, p.label, p.cluster_code, p.tags,
                    o.attr_offset, o.attr_len, pf.rel_path AS attr_rel,
                    i.session_id, i.batch_id, i.case_id, i.request_time, i.question_text
                FROM public.eval_problems p
                JOIN public.eval_outputs o ON o.output_id = p.output_id AND o.is_latest = TRUE
                JOIN public.eval_inputs i ON i.input_id = p.input_id AND NOT i.is_deleted
                LEFT JOIN public.pack_files pf ON pf.file_id = o.attr_file_id
                WHERE p.model = %s AND {cond}
                ORDER BY p.problem_id ASC
                LIMIT %s
                """,
                params + (int(limit),),
            ).fetchall()
        return int(stat["matched"] or 0), int(stat["sessions"] or 0), [dict(r) for r in rows]

    def bind_clusters(
        self,
        bindings: list[dict[str, Any]],
        *,
        tree_id: int | None = None,
        cluster_version: str | None = None,
        data_cutoff: str | None = None,
        batch_size: int = 500,
    ) -> dict[str, int]:
        """分批 POST /api/v1/data-core/clusters/bind 回填 PG。"""
        total_updated = 0
        total_changes = 0
        bs = max(1, int(batch_size))
        for i in range(0, len(bindings), bs):
            chunk = bindings[i : i + bs]
            payload: dict[str, Any] = {"bindings": chunk}
            if tree_id is not None:
                payload["tree_id"] = int(tree_id)
            if cluster_version:
                payload["cluster_version"] = cluster_version
            if data_cutoff:
                payload["data_cutoff"] = data_cutoff
            resp = http_json(self.bind_url, payload)
            data = resp.get("data") if isinstance(resp, dict) else {}
            if not isinstance(data, dict):
                data = {}
            total_updated += int(data.get("updated") or 0)
            total_changes += int(data.get("changes_emitted") or 0)
        return {"updated": total_updated, "changes_emitted": total_changes}

    def publish_via_writer(
        self,
        model: str,
        cluster_dir: str,
        *,
        release_tag: str | None = None,
        skip_bind: bool = True,
    ) -> dict[str, Any]:
        env = dict(os.environ)
        pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{self.pg_core_path}:{pp}" if pp else self.pg_core_path
        cmd = [
            sys.executable,
            "-m",
            "pg_data_core.writers",
        ]
        if self.data_root_override:
            cmd += ["--data-root", self.data_root_override]
        cmd += [
            "publish-cluster",
            "--model",
            model,
            "--cluster-dir",
            cluster_dir,
        ]
        if release_tag:
            cmd += ["--release-tag", release_tag]
        if skip_bind:
            cmd.append("--skip-bind")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"publish-cluster 失败 (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )
        txt = proc.stdout.strip()
        try:
            return json.loads(txt)
        except Exception:
            return {"raw_stdout": txt}

    def health_check(self) -> dict[str, Any]:
        return http_json(self.health_url, None, timeout=6, attempts=2)


# ---------------------------------------------------------------- prepare delta workdir
def build_delta_items(
    rows: list[dict[str, Any]],
    hydrated: dict[int, dict[str, Any]],
    *,
    channel: str = "aime",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 PG 增量行 + Zstd 解压字典转换成标准 items 列表，并按 Q7 隔离未登记组件。"""
    cmap = C.component_map()
    c2l = C.load_comp2l1()
    known = {v["tool"] for v in cmap.values()}
    valid_items: list[dict[str, Any]] = []
    unregistered: list[dict[str, Any]] = []

    for r in rows:
        pid = int(r["problem_id"])
        prob = hydrated.get(pid) or {}
        raw_comp = C.clean(r.get("component") or prob.get("component"))
        tool, comp_label, stage = C.canon_component(raw_comp)
        cent = cmap.get(raw_comp) or {}
        l1 = stage or c2l.get(tool)
        if l1 and "+" in str(l1):
            l1 = None
        if (tool not in known and not stage) or not l1:
            unregistered.append(
                {
                    "problem_id": pid,
                    "trace_id": C.clean(r.get("trace_id")),
                    "component_raw": raw_comp,
                    "component_tool": tool,
                    "label": C.clean(r.get("label") or prob.get("label")),
                    "severity": C.clean(r.get("severity") or prob.get("severity")),
                }
            )
            continue

        tid = C.clean(r.get("trace_id"))
        sid = C.usable_sid(r.get("session_id")) or tid
        rtime_iso = fmt_iso_utc(r.get("request_time"))
        biz_day = fmt_date_str(r.get("biz_day")) or rtime_iso[:10]
        question = C.clean(r.get("question_text"))
        batch_id = C.clean(r.get("batch_id")) or biz_day
        cid = C.clean(r.get("case_id")) or tid
        valid_items.append(
            {
                "seq": pid,
                "problem_id": pid,
                "date": biz_day,
                "batch_id": batch_id,
                "model": C.clean(r.get("model")),
                "case_id": cid,
                "trace_id": tid,
                "session_id": sid,
                "user_id": C.clean(r.get("user_id")),
                "question": question,
                "request_time": rtime_iso,
                "request_time_iso": rtime_iso,
                "bad_case_id": C.bad_case_id(channel, question, rtime_iso),
                "channel": channel,
                "scope": C.clean(r.get("scope") or "turn"),
                "turn_index": int(r.get("turn_index") or 0),
                "problem_index": int(r.get("problem_index") or 0),
                "severity": C.clean(r.get("severity") or prob.get("severity")),
                "component": tool,
                "component_label": comp_label,
                "component_raw": raw_comp,
                "component_role": cent.get("role", ""),
                "component_family": C.clean(r.get("component_family") or prob.get("component_family")),
                "stage": l1,
                "label": C.clean(r.get("label") or prob.get("label")),
                "definition": C.clean(prob.get("definition")),
                "phenomena": C.clean(prob.get("problem_phenomena")),
                "reason": C.clean(prob.get("reason")),
                "evidence": C.clean(prob.get("evidence")),
            }
        )
    return valid_items, unregistered


def write_delta_workdir(work_dir: str, items: list[dict[str, Any]], chunk_size: int = 60) -> int:
    """写出 items.jsonl 并分片成 assign/<L1>_cNNN.jsonl + manifest.json，复用 classify.py / l4_apply.py。"""
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    items_path = os.path.join(work_dir, "items.jsonl")
    with open(items_path, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    C.cmd_chunk(argparse.Namespace(work=work_dir, size=int(chunk_size)))
    manifest = C.jload(os.path.join(work_dir, "assign", "manifest.json"), []) or []
    return len(manifest)


def write_unregistered_log(model: str, unregistered: list[dict[str, Any]]) -> str:
    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    counts = collections.Counter(u["component_raw"] for u in unregistered)
    samples_by_comp: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for u in unregistered:
        if len(samples_by_comp[u["component_raw"]]) < 3:
            samples_by_comp[u["component_raw"]].append(
                {"problem_id": u["problem_id"], "trace_id": u["trace_id"], "label": u["label"]}
            )
    path = os.path.join(log_dir, f"unregistered_{model}.json")
    C.jdump(
        {
            "at": now_local().isoformat(timespec="seconds"),
            "model": model,
            "total_rows": len(unregistered),
            "unregistered": [
                {"component": comp, "count": cnt, "samples": samples_by_comp[comp]}
                for comp, cnt in counts.most_common()
            ],
        },
        path,
    )
    return path


def archive_unresolved_files(work_dir: str, model: str) -> list[str]:
    saved: list[str] = []
    ts = now_local().strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    for src in sorted(glob.glob(os.path.join(work_dir, "unresolved_*.json"))):
        dst = os.path.join(log_dir, f"{model}_{ts}_{os.path.basename(src)}")
        shutil.copy2(src, dst)
        saved.append(dst)
    return saved


def collect_l3_assignments(work_dir: str, valid_pids: set[int], valid_l3: set[str]) -> dict[int, tuple[str, bool]]:
    out: dict[int, tuple[str, bool]] = {}
    for f in sorted(glob.glob(os.path.join(work_dir, "assign", "*.out.jsonl"))):
        if os.path.basename(f) == "zz_l4.out.jsonl":
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                pid = int(r["seq"])
                path = str(r.get("path") or "")
                if pid in valid_pids and path in valid_l3:
                    out[pid] = (path, bool(r.get("edge", False)))
    return out


def collect_l4_overlay(work_dir: str, valid_pids: set[int], valid_l4: set[str]) -> dict[int, tuple[str, bool]]:
    p = os.path.join(work_dir, "assign", "zz_l4.out.jsonl")
    if not os.path.exists(p):
        return {}
    out: dict[int, tuple[str, bool]] = {}
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            pid = int(r["seq"])
            path = str(r.get("path") or "")
            if pid in valid_pids and path in valid_l4:
                out[pid] = (path, bool(r.get("edge", False)))
    return out


def run_classify_subprocess(work_dir: str, concurrency: int, repair_log: str) -> int:
    env = dict(os.environ)
    env["CLUSTER_REPAIR_LOG"] = repair_log
    cmd = [sys.executable, os.path.join(HERE, "classify.py"), "--work", work_dir, "--concurrency", str(concurrency)]
    return subprocess.run(cmd, env=env).returncode


def run_l4_subprocess(work_dir: str, leaves_csv: str, concurrency: int, *, force: bool = False) -> int:
    cmd = [
        sys.executable,
        os.path.join(HERE, "l4_apply.py"),
        "--run",
        work_dir,
        "--leaves",
        leaves_csv,
        "--concurrency",
        str(concurrency),
        "--size",
        "60",
        "--min",
        "1",
    ]
    if force:
        cmd.append("--force")
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------- PG-native whole-tree aggregation & publish
class _SampleBucket:
    """流式维护单个叶子的代表性抽样（与 cluster.py:sample 同口径：优先不同组件，按 problem_id 升序）。"""

    __slots__ = ("limit", "diverse", "seen_comps", "fallback")

    def __init__(self, limit: int):
        self.limit = limit
        self.diverse: list[dict[str, Any]] = []
        self.seen_comps: set[str] = set()
        self.fallback: list[dict[str, Any]] = []

    def add(self, rec: dict[str, Any]) -> None:
        if len(self.fallback) < self.limit:
            self.fallback.append(rec)
        comp = rec["component"]
        if len(self.diverse) < self.limit and comp not in self.seen_comps:
            self.seen_comps.add(comp)
            self.diverse.append(rec)

    def picked(self) -> list[dict[str, Any]]:
        out = list(self.diverse)
        have = {r["problem_id"] for r in out}
        for r in self.fallback:
            if len(out) >= self.limit:
                break
            if r["problem_id"] not in have:
                out.append(r)
                have.add(r["problem_id"])
        return out


def _sample_record_to_ref(rec: dict[str, Any], definition: str) -> dict[str, Any]:
    return {
        "seq": rec["problem_id"],
        "case_id": rec["case_id"],
        "bad_case_id": rec["bad_case_id"],
        "user_id": rec["user_id"],
        "session_id": rec["session_id"],
        "trace_id": rec["trace_id"],
        "batch_id": rec["batch_id"],
        "model": rec["model"],
        "src_rel": rec["attr_rel"],
        "scope": rec["scope"],
        "turn_index": rec["turn_index"],
        "problem_index": rec["problem_index"],
        "question": (rec["question"] or "")[:120],
        "problem_id": str(rec["problem_id"]),
        "severity": rec["severity"],
        "component": rec["component"],
        "label": rec["label"],
        "definition": (definition or "")[:160],
    }


def _write_syndrome(
    out_dir: str,
    inv: dict[str, Any],
    tree_doc: dict[str, Any],
    sess_mechs: dict[str, set[str]],
    sess_leaves: dict[str, set[str]],
    total_items: int,
) -> None:
    l2name = {k: v["name"] for k, v in inv["l2"].items()}
    combos: collections.Counter[tuple[str, ...]] = collections.Counter()
    pairs: collections.Counter[tuple[str, str]] = collections.Counter()
    for sid, ms in sess_mechs.items():
        combos[tuple(sorted(ms))] += 1
        leaves = sorted(sess_leaves.get(sid) or ())
        for i in range(len(leaves)):
            for j in range(i + 1, len(leaves)):
                pairs[(leaves[i], leaves[j])] += 1

    n_sess = len(sess_mechs)
    lines = [
        "# 会话级失效组合（syndrome）\n",
        f"- 会话总数：{n_sess:,}；平均每会话 {total_items / max(1, n_sess):.1f} 条问题",
        "- 组合 = 同一会话内出现的失效机制集合（L2）\n",
        "## 最常见机制组合\n",
        "| 组合 | 会话数 | 占比 |",
        "|---|---|---|",
    ]
    for ms, n in combos.most_common(15):
        lines.append(f"| {' + '.join(l2name.get(m, m) for m in ms)} | {n} | {n / max(1, n_sess):.0%} |")
    lines += ["\n## 最常同时出现的模式对\n", "| 模式 A | 模式 B | 共同会话 |", "|---|---|---|"]
    name_map: dict[str, str] = {}
    for l1 in tree_doc["tree"]["children"]:
        for l2 in l1["children"]:
            for l3 in l2["children"]:
                name_map[l3["id"]] = l3["name"]
    for (p1, p2), n in pairs.most_common(15):
        lines.append(f"| {p1} {name_map.get(p1, '')} | {p2} {name_map.get(p2, '')} | {n} |")
    with open(os.path.join(out_dir, "syndrome.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    C.jdump(
        {
            "combos": [{"mechanisms": list(ms), "sessions": n} for ms, n in combos.most_common(50)],
            "pairs": [{"a": p1, "b": p2, "sessions": n} for (p1, p2), n in pairs.most_common(50)],
        },
        os.path.join(out_dir, "syndrome.json"),
    )


def _write_manifest(out_dir: str, tree_doc: dict[str, Any], *, include_item_index: bool = False) -> None:
    ver = load_version_doc()
    files = list(DELIVERABLE_MANIFEST_FILES)
    if include_item_index:
        files.append("item_index.jsonl")
    meta = tree_doc["meta"]
    root_node = tree_doc["tree"]
    man = {
        "bundle": meta["title"],
        "pipeline_version": ver,
        "generated_at": now_local().isoformat(timespec="seconds"),
        "dataset": {k: meta[k] for k in ("total_items", "total_traces", "total_sessions", "total_cases")},
        "tree": {
            "depth": meta["depth"],
            "level1": len(root_node["children"]),
            "level2": sum(len(c["children"]) for c in root_node["children"]),
            "level3": sum(len(l2["children"]) for l1 in root_node["children"] for l2 in l1["children"]),
            "level4": sum(
                len(l3.get("children", []))
                for l1 in root_node["children"]
                for l2 in l1["children"]
                for l3 in l2["children"]
            ),
        },
        "quality": {"edge_flagged": meta["edge_flagged"], "edge_rate": meta["edge_rate"]},
        "files": [
            {"name": fn, "bytes": os.path.getsize(os.path.join(out_dir, fn)), "sha256_16": sha16(os.path.join(out_dir, fn))}
            for fn in files
            if os.path.exists(os.path.join(out_dir, fn))
        ],
        "notes": [
            "主口径为会话数，条目数作附列。",
            "明细归属真源为 PostgreSQL 16 public.eval_problems（可由 bin/pg_cluster.py recall 直接召回）。",
            "每个叶子内嵌 samples，含 trace_id / session_id / problem_id，可回 PG + Zstd 还原现场。",
        ],
    }
    C.jdump(man, os.path.join(out_dir, "MANIFEST.json"))


def build_tree_from_pg(
    backend: PGBackend,
    model: str,
    out_dir: str,
    *,
    write_item_index: bool = False,
    chart_font: str | None = None,
) -> dict[str, Any]:
    """流式遍历 PG 已绑定条目，生成完整 10 件（可选 +item_index.jsonl）并执行严格校验。"""
    inv, tagdef, l4nodes = C.taxonomy()
    nodes_meta = {n["id"]: n for n in inv["nodes"]}
    leaf_tags = C.jload(os.path.join(ROOT, "taxonomy", "leaf_tags.json"), {}) or {}
    valid_codes = set(nodes_meta) | set(l4nodes)

    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    # 聚合累加器（全流式，避免把 50 万行完整 dict 装入内存）
    path_count: collections.Counter[str] = collections.Counter()
    path_sessions: dict[str, set[str]] = collections.defaultdict(set)
    path_traces: dict[str, set[str]] = collections.defaultdict(set)
    path_sev: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    l3_samples: dict[str, _SampleBucket] = collections.defaultdict(lambda: _SampleBucket(5))
    l4_samples: dict[str, _SampleBucket] = collections.defaultdict(lambda: _SampleBucket(3))

    l1_sessions: dict[str, set[str]] = collections.defaultdict(set)
    l1_comps: dict[str, set[str]] = collections.defaultdict(set)
    l2_sessions: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    mech_count: collections.Counter[str] = collections.Counter()
    mech_sessions: dict[str, set[str]] = collections.defaultdict(set)
    mech_by_l1: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    tag_count: collections.Counter[str] = collections.Counter()
    tag_sessions: dict[str, set[str]] = collections.defaultdict(set)
    tag_by_l1: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)

    sess_mechs: dict[str, set[str]] = collections.defaultdict(set)
    sess_leaves: dict[str, set[str]] = collections.defaultdict(set)

    all_traces: set[str] = set()
    all_sessions: set[str] = set()
    all_cases: set[str] = set()
    total = 0
    edge_flagged = 0
    max_cutoff_req = ""
    max_cutoff_day = ""
    invalid_codes: collections.Counter[str] = collections.Counter()
    missing_identity_pids: list[int] = []

    idx_fh = open(os.path.join(out_dir, "item_index.jsonl"), "w", encoding="utf-8") if write_item_index else None
    try:
        for r in backend.iter_bound_problems(model):
            pid = int(r["problem_id"])
            code = C.clean(r.get("cluster_code")).strip()
            parts = code.split(".")
            if len(parts) not in (3, 4) or code not in valid_codes:
                invalid_codes[code or "<empty>"] += 1
                continue

            tid = C.clean(r.get("trace_id")).strip()
            sid = C.usable_sid(r.get("session_id")) or tid
            if not tid or not sid:
                if len(missing_identity_pids) < 20:
                    missing_identity_pids.append(pid)

            l1, l2 = parts[0], parts[1]
            l3 = ".".join(parts[:3])
            tool, _, _ = C.canon_component(C.clean(r.get("component")))
            sev = C.clean(r.get("severity")) or "?"
            rtime_iso = fmt_iso_utc(r.get("request_time"))
            biz_day = fmt_date_str(r.get("biz_day")) or rtime_iso[:10]
            if rtime_iso and rtime_iso > max_cutoff_req:
                max_cutoff_req = rtime_iso
            if biz_day and biz_day > max_cutoff_day:
                max_cutoff_day = biz_day

            cid = C.clean(r.get("case_id")) or tid
            batch_id = C.clean(r.get("batch_id")) or biz_day
            question = C.clean(r.get("question_text"))
            is_edge = bool(r.get("is_edge"))

            total += 1
            if is_edge:
                edge_flagged += 1
            all_traces.add(tid)
            all_sessions.add(sid)
            all_cases.add(cid)

            path_count[code] += 1
            path_sessions[code].add(sid)
            path_traces[code].add(tid)
            path_sev[code][sev] += 1

            l1_sessions[l1].add(sid)
            l1_comps[l1].add(tool)
            l2_sessions[(l1, l2)].add(sid)
            mech_count[l2] += 1
            mech_sessions[l2].add(sid)
            mech_by_l1[l2][l1] += 1

            tags = leaf_tags.get(l3, [])
            for t in tags:
                tag_count[t] += 1
                tag_sessions[t].add(sid)
                tag_by_l1[t][l1] += 1

            sess_mechs[sid].add(l2)
            sess_leaves[sid].add(l3)

            srec = {
                "problem_id": pid,
                "case_id": cid,
                "bad_case_id": C.bad_case_id("aime", question, rtime_iso),
                "user_id": C.clean(r.get("user_id")),
                "session_id": sid,
                "trace_id": tid,
                "batch_id": batch_id,
                "model": model,
                "scope": C.clean(r.get("scope") or "turn"),
                "turn_index": int(r.get("turn_index") or 0),
                "problem_index": int(r.get("problem_index") or 0),
                "question": question[:120],
                "severity": sev,
                "component": tool,
                "label": C.clean(r.get("label")),
                "attr_rel": C.clean(r.get("attr_rel")),
                "attr_offset": r.get("attr_offset"),
                "attr_len": r.get("attr_len"),
            }
            l3_samples[l3].add(srec)
            if len(parts) == 4:
                l4_samples[code].add(srec)

            if idx_fh is not None:
                idx_fh.write(
                    json.dumps(
                        {
                            "seq": pid,
                            "date": biz_day,
                            "request_time": rtime_iso,
                            "case_id": cid,
                            "bad_case_id": srec["bad_case_id"],
                            "user_id": srec["user_id"],
                            "session_id": sid,
                            "trace_id": tid,
                            "batch_id": batch_id,
                            "model": model,
                            "scope": srec["scope"],
                            "turn_index": srec["turn_index"],
                            "problem_index": srec["problem_index"],
                            "problem_id": str(pid),
                            "component": tool,
                            "label": srec["label"],
                            "severity": sev,
                            "path": code,
                            "edge": is_edge,
                            "tags": tags,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    finally:
        if idx_fh is not None:
            idx_fh.close()

    if total == 0:
        raise RuntimeError(f"{model}: PG 中没有已绑定的有效 eval_problems，无法发布聚类树")

    # 仅对所有抽样（约 1,000 条）做一次 O(1) Zstd pread 补全 definition
    sample_rows_by_pid: dict[int, dict[str, Any]] = {}
    for bkt in list(l3_samples.values()) + list(l4_samples.values()):
        for rec in bkt.picked():
            sample_rows_by_pid[rec["problem_id"]] = rec
    hydrated_samples = hydrate_problems_from_zst(list(sample_rows_by_pid.values()), backend.pread_attr)

    def def_of(pid: int) -> str:
        return C.clean((hydrated_samples.get(pid) or {}).get("definition"))

    def meta_for(nid: str) -> dict[str, Any]:
        if nid in nodes_meta:
            n = nodes_meta[nid]
            return {
                "name": n["name"],
                "plain": n.get("plain", ""),
                "definition": n.get("definition", ""),
                "include": n.get("include", ""),
                "exclude": n.get("exclude", ""),
                "components": n.get("components", []),
            }
        if nid in l4nodes:
            n = l4nodes[nid]
            return {
                "name": n["name"],
                "plain": n.get("plain", ""),
                "definition": n.get("definition", ""),
                "include": n.get("include", ""),
                "exclude": n.get("exclude", ""),
                "components": [],
            }
        return {"name": nid.split(".")[-1], "plain": "", "definition": "", "include": "", "exclude": "", "components": []}

    l4_by_l3: dict[str, list[str]] = collections.defaultdict(list)
    l3_by_l1_l2: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    l1_seen: set[str] = set()
    for p in path_count:
        ps = p.split(".")
        l1_seen.add(ps[0])
        l3_id = ".".join(ps[:3])
        l3_by_l1_l2[(ps[0], ps[1])].add(l3_id)
        if len(ps) == 4:
            l4_by_l3[l3_id].append(p)

    def build_l3(l3_id: str) -> dict[str, Any]:
        kids_ids = sorted(l4_by_l3.get(l3_id, ()), key=lambda x: (-path_count[x], x))
        base_cnt = path_count.get(l3_id, 0)
        tot_cnt = base_cnt + sum(path_count[k] for k in kids_ids)
        union_sess = set(path_sessions.get(l3_id, ()))
        union_traces = set(path_traces.get(l3_id, ()))
        sev_sum: collections.Counter[str] = collections.Counter(path_sev.get(l3_id, {}))
        for k in kids_ids:
            union_sess |= path_sessions[k]
            union_traces |= path_traces[k]
            sev_sum.update(path_sev[k])

        node = {
            "id": l3_id,
            "level": 3,
            **meta_for(l3_id),
            "count": tot_cnt,
            "sessions": len(union_sess),
            "traces": len(union_traces),
            "severity": dict(sev_sum),
            "share": round(tot_cnt / total, 4),
            "tags": leaf_tags.get(l3_id, []),
            "low_sample": tot_cnt < C.LOW_SAMPLE,
            "recall": f"python3 bin/pg_cluster.py recall --model {model} --node {l3_id}",
            "samples": [_sample_record_to_ref(rec, def_of(rec["problem_id"])) for rec in l3_samples[l3_id].picked()],
        }
        if kids_ids:
            children = []
            for k in kids_ids:
                kc = path_count[k]
                children.append(
                    {
                        "id": k,
                        "level": 4,
                        **meta_for(k),
                        "count": kc,
                        "sessions": len(path_sessions[k]),
                        "traces": len(path_traces[k]),
                        "severity": dict(path_sev[k]),
                        "share": round(kc / total, 4),
                        "share_within_l3": round(kc / max(1, tot_cnt), 3),
                        "low_sample": kc < C.LOW_SAMPLE,
                        "samples": [
                            _sample_record_to_ref(rec, def_of(rec["problem_id"])) for rec in l4_samples[k].picked()
                        ],
                    }
                )
            node["children"] = children
        return node

    l1_children = []
    for l1 in sorted(l1_seen):
        l2_children = []
        l1_total_cnt = 0
        for l2 in ("M1", "M2", "M3", "M4", "M5", "M6", "M7"):
            l3_ids = sorted(l3_by_l1_l2.get((l1, l2), ()))
            if not l3_ids:
                continue
            kids = [build_l3(i) for i in l3_ids]
            c = sum(k["count"] for k in kids)
            l1_total_cnt += c
            l2_children.append(
                {
                    "id": f"{l1}.{l2}",
                    "level": 2,
                    "name": inv["l2"][l2]["name"],
                    "mechanism": l2,
                    "definition": inv["l2"][l2]["definition"],
                    "count": c,
                    "share": round(c / total, 4),
                    "share_within_l1": 0.0,  # 待算完 l1_total_cnt 回填
                    "sessions": len(l2_sessions[(l1, l2)]),
                    "children": kids,
                }
            )
        for l2_node in l2_children:
            l2_node["share_within_l1"] = round(l2_node["count"] / max(1, l1_total_cnt), 4)
        l2_children.sort(key=lambda x: -x["count"])
        l1_children.append(
            {
                "id": l1,
                "level": 1,
                "name": inv["l1"][l1]["name"],
                "definition": inv["l1"][l1]["definition"],
                "count": l1_total_cnt,
                "share": round(l1_total_cnt / total, 4),
                "sessions": len(l1_sessions[l1]),
                "components": sorted(l1_comps[l1]),
                "children": l2_children,
            }
        )
    l1_children.sort(key=lambda x: -x["count"])

    tags_out = [
        {
            "tag": t,
            "name": tagdef.get(t, {}).get("name", t),
            "definition": tagdef.get(t, {}).get("definition", ""),
            "count": tag_count[t],
            "sessions": len(tag_sessions[t]),
            "share": round(tag_count[t] / total, 4),
            "by_l1": dict(tag_by_l1[t]),
        }
        for t in sorted(tag_count, key=lambda x: -tag_count[x])
    ]

    all_stages = sorted((k for k in inv["l1"] if k != "S0"), key=lambda s: int(s[1:]) if s[1:].isdigit() else 99)
    mech_out = []
    for m in ("M1", "M2", "M3", "M4", "M5", "M6", "M7"):
        if not mech_count[m]:
            continue
        mech_out.append(
            {
                "mechanism": m,
                "name": inv["l2"][m]["name"],
                "definition": inv["l2"][m].get("definition", ""),
                "count": mech_count[m],
                "share": round(mech_count[m] / total, 4),
                "sessions": len(mech_sessions[m]),
                "by_l1": {k: mech_by_l1[m][k] for k in all_stages if mech_by_l1[m][k]},
            }
        )

    sessions_all = backend.sessions_all_count(model)
    data_cutoff = max_cutoff_req or (f"{max_cutoff_day}T00:00:00+00:00" if max_cutoff_day else "")
    has_l4 = any(len(p.split(".")) == 4 for p in path_count)
    tree_doc = {
        "meta": {
            "title": "归因问题聚类树",
            "generated_at": now_local().isoformat(timespec="seconds"),
            "pipeline_version": pipeline_version(),
            "model": model,
            "source": "pg16",
            "unit": "原子归因问题条目（每条只归属一个链路 component）",
            "total_items": total,
            "total_traces": len(all_traces),
            "total_sessions": len(all_sessions),
            "total_cases": len(all_cases),
            "sessions_all": sessions_all,
            "depth": 4 if has_l4 else 3,
            "data_cutoff": data_cutoff,
            "edge_flagged": edge_flagged,
            "edge_rate": round(edge_flagged / max(1, total), 4),
            "axes": {
                "level_1": "环节：失败发生在链路哪一段（component 决定）",
                "level_2": "失效机制：动作错在哪一层（7 类统一口径，按 M1→M7 取最早失效点）",
                "level_3": "失效模式：可派工的动作级模式（互斥、穷尽）",
                "level_4": "细分变体：仅在满足三条准入时下钻",
            },
            "counting": "主口径 = 会话数（同一会话同一叶子计 1）；附列 = 条目数",
            "low_sample_threshold": C.LOW_SAMPLE,
            "low_sample_rule": "条目数低于阈值的叶子保留在树中用于定位，但不参与排名比较（统计不稳定）",
            "tags": "横切标签，一条可多标签，不改变树的互斥性",
            "traceability": {
                "principle": "每个叶子带 samples，每条带 trace_id + session_id + problem_id（真源为 PG 16 + 多帧 Zstd）",
                "recall_tool": f"python3 bin/pg_cluster.py recall --model {model} --node <id>",
            },
        },
        "tree": {
            "id": "root",
            "name": "归因聚类",
            "level": 0,
            "count": total,
            "share": 1.0,
            "sessions": len(all_sessions),
            "children": l1_children,
        },
        "mechanism_rollup": mech_out,
        "tag_rollup": tags_out,
    }
    C.jdump(tree_doc, os.path.join(out_dir, "cluster_tree.json"))

    with open(os.path.join(out_dir, "leaves.tsv"), "w", encoding="utf-8") as fh:
        fh.write("path\tl1\tl2\tname\titems\tsessions\tshare\ttags\n")
        for l1 in l1_children:
            for l2 in l1["children"]:
                for l3 in l2["children"]:
                    fh.write(
                        f"{l3['id']}\t{l1['name']}\t{l2['name']}\t{l3['name']}\t{l3['count']}\t"
                        f"{l3['sessions']}\t{l3['share']:.4f}\t{','.join(l3.get('tags', []))}\n"
                    )

    # 严格自检（替代基于 item_index.jsonl 的旧 verify，确保 err=0 / mismatch=0 才允许发版）
    errs: list[str] = []
    seen_tree: collections.Counter[str] = collections.Counter()
    mism = 0
    if invalid_codes:
        errs.append(f"invalid cluster_code in PG: {dict(invalid_codes.most_common(10))}")
    if missing_identity_pids:
        errs.append(f"rows missing trace_id/session_id: problem_ids={missing_identity_pids[:5]}")
    if sum(x["count"] for x in l1_children) != total:
        errs.append("root sum mismatch")
    for l1 in l1_children:
        if sum(x["count"] for x in l1["children"]) != l1["count"]:
            errs.append(f"{l1['id']} sum mismatch")
        for l2 in l1["children"]:
            if sum(x["count"] for x in l2["children"]) != l2["count"]:
                errs.append(f"{l2['id']} sum mismatch")
            for l3 in l2["children"]:
                kids = l3.get("children") or []
                if kids:
                    ksum = sum(x["count"] for x in kids)
                    if ksum > l3["count"]:
                        errs.append(f"{l3['id']} children exceed parent ({ksum} > {l3['count']})")
                    for c in kids:
                        seen_tree[c["id"]] += c["count"]
                    if l3["count"] - ksum:
                        seen_tree[l3["id"]] += l3["count"] - ksum
                else:
                    seen_tree[l3["id"]] += l3["count"]
                if not l3["samples"]:
                    errs.append(f"{l3['id']} no samples")
                for s in l3["samples"]:
                    if not s.get("trace_id") or not s.get("session_id") or s.get("trace_id") not in all_traces:
                        mism += 1
                        errs.append(f"sample 缺可追溯信息：problem_id={s.get('problem_id')} trace={s.get('trace_id')!r}")
    if seen_tree != path_count:
        errs.append("tree leaf counts != PG path counts")

    verify_doc = {
        "leaves": len(seen_tree),
        "total": total,
        "locator_mismatch": mism,
        "errors": len(errs),
        "items": errs[:50],
    }
    C.jdump(verify_doc, os.path.join(out_dir, "verify.json"))
    if errs or mism:
        raise RuntimeError(f"{model} 校验失败：errors={len(errs)}, locator_mismatch={mism}, first={errs[:3]}")

    # 复用 cluster.py 产出 report + chart，以及写出 syndrome + MANIFEST
    C.cmd_report(argparse.Namespace(out=out_dir))
    font = chart_font or os.environ.get("CLUSTER_CHART_FONT", "")
    C.cmd_chart(argparse.Namespace(out=out_dir, font=font))
    _write_syndrome(out_dir, inv, tree_doc, sess_mechs, sess_leaves, total)
    _write_manifest(out_dir, tree_doc, include_item_index=write_item_index)

    return {
        "total_items": total,
        "total_traces": len(all_traces),
        "total_sessions": len(all_sessions),
        "leaves": len(seen_tree),
        "l4_leaves": sum(len(l3.get("children", [])) for l1 in l1_children for l2 in l1["children"] for l3 in l2["children"]),
        "edge_flagged": edge_flagged,
        "data_cutoff": data_cutoff,
        "verify": f"err={verify_doc['errors']},mismatch={verify_doc['locator_mismatch']}",
    }


def publish_model(
    backend: PGBackend,
    model: str,
    *,
    release_tag: str | None = None,
    rebind_all: bool = False,
    dry_run: bool = False,
    reason: str = "manual",
) -> dict[str, Any]:
    t0 = time.monotonic()
    ver = pipeline_version()
    tag = release_tag or f"v{ver}_{now_local().strftime('%Y%m%d')}"
    stage_dir = os.path.join(ROOT, "runs", "_pg_publish", model, "out")
    print(f"  [publish] {model}: 流式聚合 PG 有效条目 -> {stage_dir}（tag={tag}，reason={reason}）")
    agg = build_tree_from_pg(backend, model, stage_dir, write_item_index=rebind_all)
    pub_res: dict[str, Any] = {"dry_run": True, "release_tag": tag}
    if not dry_run:
        pub_res = backend.publish_via_writer(
            model,
            stage_dir,
            release_tag=tag,
            skip_bind=not rebind_all,
        )
        print(
            f"  [publish] {model}: 已发版 tag={pub_res.get('release_tag', tag)} "
            f"tree_id={pub_res.get('tree_id')} nodes={pub_res.get('nodes')} "
            f"bound={pub_res.get('bound', 0)} changes={pub_res.get('changes_emitted', 0)}"
        )
        append_ledger(
            {
                "at": now_local().isoformat(timespec="seconds"),
                "channel": "aime",
                "tree": model,
                "mode": "pg_publish",
                "reason": reason,
                "release_tag": pub_res.get("release_tag", tag),
                "tree_id": pub_res.get("tree_id"),
                "items": agg["total_items"],
                "delta": int(pub_res.get("bound", 0)),
                "elapsed_s": int(time.monotonic() - t0),
                "verify": agg["verify"],
                "version": ver,
            }
        )
    else:
        print(f"  [publish][dry-run] {model}: items={agg['total_items']} leaves={agg['leaves']} {agg['verify']}（未调 publish-cluster）")
    return {"agg": agg, "publish": pub_res}


def should_daily_publish(
    backend: PGBackend,
    model: str,
    daily_publish_hour: int,
    *,
    now_dt: datetime.datetime | None = None,
) -> tuple[bool, str]:
    """判断当日定时窗口（默认 CST 04:00 起）是否需要自动发版。"""
    if daily_publish_hour < 0:
        return False, ""
    now_dt = now_dt or now_local()
    if now_dt.hour < daily_publish_hour:
        return False, ""
    today_tag = f"v{pipeline_version()}_{now_dt.strftime('%Y%m%d')}"
    cur = backend.current_tree_info(model)
    if cur is None:
        return True, "initial-tree"
    if str(cur.get("release_tag") or "") != today_tag:
        latest_ts = backend.latest_clustered_at(model)
        gen_ts = cur.get("generated_at")
        if latest_ts is None:
            return False, ""
        if gen_ts is None or latest_ts > gen_ts:
            return True, "daily-window"
        # 版本号升级后即使无新数据也触发一次发版
        if str(cur.get("version") or "") != pipeline_version():
            return True, "version-upgrade"
    return False, ""


# ---------------------------------------------------------------- incremental & backfill-l4
def run_incremental_model(
    backend: PGBackend,
    model: str,
    *,
    limit: int = 5000,
    concurrency: int = 4,
    chunk_size: int = 60,
    l4_leaves: list[str] | None = None,
    bind_batch_size: int = 500,
    dry_run: bool = False,
    no_bind: bool = False,
    auto_publish_new_leaf: bool = True,
    daily_publish_hour: int = -1,
    classify_runner=run_classify_subprocess,
    l4_runner=run_l4_subprocess,
    gateway_checker=CL.healthy_gateways,
) -> dict[str, Any]:
    """对单个模型执行一轮 PG-Native 增量分类 + L4 + 分批回填 + 触发发版检查。"""
    t0 = time.monotonic()
    ver = pipeline_version()
    repair_log = os.environ.get("CLUSTER_REPAIR_LOG", os.path.join(ROOT, "logs", "missing_leaf_aime.jsonl"))
    rep_base = repair_log_count(repair_log)

    print(f"\n=== {model} (PG-Native incremental) ===")
    rows = backend.fetch_unclustered(model, limit=limit)
    if not rows:
        print("  无待聚类条目（cluster_code IS NULL = 0），跳过")
        pub_triggered = False
        if not dry_run and not no_bind:
            need_pub, reason = should_daily_publish(backend, model, daily_publish_hour)
            if need_pub:
                publish_model(backend, model, reason=reason)
                pub_triggered = True
        append_ledger(
            {
                "at": now_local().isoformat(timespec="seconds"),
                "channel": "aime",
                "tree": model,
                "mode": "pg_incremental",
                "items": backend.model_counts(model)["bound"],
                "delta": 0,
                "elapsed_s": int(time.monotonic() - t0),
                "verify": "noop",
                "version": ver,
            }
        )
        return {"model": model, "status": "noop", "fetched": 0, "bound": 0, "published": pub_triggered, "rc": 0}

    print(f"  拉取待聚类 {len(rows)} 条（上限 {limit or '无'}），按 trace O(1) Zstd pread 补全文本...")
    hydrated = hydrate_problems_from_zst(rows, backend.pread_attr)
    valid_items, unregistered = build_delta_items(rows, hydrated, channel="aime")

    partial_issue = False
    unreg_path = ""
    if unregistered:
        partial_issue = True
        unreg_path = write_unregistered_log(model, unregistered)
        uniq = sorted({u["component_raw"] for u in unregistered})
        print(
            f"  WARN 隔离未登记组件 {len(unregistered)} 条（{len(uniq)} 种：{', '.join(uniq[:5])}）-> {unreg_path}",
            file=sys.stderr,
        )

    if not valid_items:
        print("  本批全部为未登记组件，无合法条目可归类（保留 cluster_code IS NULL 待登记）", file=sys.stderr)
        return {
            "model": model,
            "status": "unregistered_only",
            "fetched": len(rows),
            "unregistered": len(unregistered),
            "bound": 0,
            "rc": 1,
        }

    delta_dir = os.path.join(ROOT, "runs", "_pg_delta", model)
    n_chunks = write_delta_workdir(delta_dir, valid_items, chunk_size=chunk_size)
    print(f"  合法待归类 {len(valid_items)} 条 -> {n_chunks} 片（chunk_size={chunk_size}）")

    if dry_run:
        print(f"  [dry-run] 已完成 Zstd 提取与分片 -> {delta_dir}，未调用归类网关、未写 PG")
        return {
            "model": model,
            "status": "dry_run",
            "fetched": len(rows),
            "valid": len(valid_items),
            "unregistered": len(unregistered),
            "chunks": n_chunks,
            "bound": 0,
            "rc": 1 if partial_issue else 0,
        }

    # 有真实待分类工作才探活网关
    gateways = gateway_checker()
    if not gateways:
        print("  BLOCKED 归类网关全部不可达，中止本轮（稍后自动重试）", file=sys.stderr)
        return {"model": model, "status": "blocked", "fetched": len(rows), "bound": 0, "rc": 3}

    cls_rc = classify_runner(delta_dir, concurrency, repair_log)
    if cls_rc != 0:
        partial_issue = True
        saved = archive_unresolved_files(delta_dir, model)
        print(
            f"  WARN classify 返回 rc={cls_rc}（失败分片保留 NULL 供补叶，已留痕 {len(saved)} 份，其余合法分片继续回填）",
            file=sys.stderr,
        )

    inv, _, l4nodes = C.taxonomy()
    valid_l3 = {n["id"] for n in inv["nodes"]}
    valid_l4 = set(l4nodes)
    leaf_tags = C.jload(os.path.join(ROOT, "taxonomy", "leaf_tags.json"), {}) or {}
    valid_pids = {int(it["problem_id"]) for it in valid_items}
    item_by_pid = {int(it["problem_id"]): it for it in valid_items}

    l3_map = collect_l3_assignments(delta_dir, valid_pids, valid_l3)
    if len(l3_map) < len(valid_items):
        partial_issue = True
        print(f"  WARN 仅 {len(l3_map)}/{len(valid_items)} 条获得合法 L3 判定，缺失条目保留 NULL", file=sys.stderr)

    if not l3_map:
        return {
            "model": model,
            "status": "classify_failed",
            "fetched": len(rows),
            "valid": len(valid_items),
            "unregistered": len(unregistered),
            "bound": 0,
            "rc": 1,
        }

    # L4 下钻：仅当 L3 结果命中目标 L4 父叶时才调 l4_apply.py
    l4_target_set = set(l4_leaves or [])
    l4_hits = [pid for pid, (path, _) in l3_map.items() if path in l4_target_set]
    l4_overlay: dict[int, tuple[str, bool]] = {}
    if l4_hits and l4_leaves:
        print(f"  L4 下钻：命中 {len(l4_hits)} 条 -> 调 l4_apply.py（并发 {concurrency}）")
        l4_rc = l4_runner(delta_dir, ",".join(l4_leaves), concurrency, force=False)
        if l4_rc != 0:
            print(f"  WARN l4_apply 返回 rc={l4_rc}，未成功下钻条目暂以 3 段路径回填", file=sys.stderr)
        l4_overlay = collect_l4_overlay(delta_dir, valid_pids, valid_l4)
        print(f"  L4 覆盖：{len(l4_overlay)}/{len(l4_hits)} 条升至 4 段")

    final_assign: dict[int, tuple[str, bool]] = dict(l3_map)
    for pid, (l4_path, l4_edge) in l4_overlay.items():
        if pid in final_assign:
            final_assign[pid] = (l4_path, bool(final_assign[pid][1] or l4_edge))

    # 组装回填 payload 与单调推进的 data_cutoff
    cur_tree = backend.current_tree_info(model)
    tree_id = int(cur_tree["tree_id"]) if cur_tree else None
    existing_node_codes: set[str] = set(cur_tree["node_codes"]) if cur_tree else set()

    bindings: list[dict[str, Any]] = []
    max_cutoff = ""
    new_leaf_codes: set[str] = set()
    for pid in sorted(final_assign):
        code, is_edge = final_assign[pid]
        l3_code = ".".join(code.split(".")[:3])
        bindings.append(
            {
                "problem_id": pid,
                "cluster_code": code,
                "is_edge": bool(is_edge),
                "tags": leaf_tags.get(l3_code, []),
            }
        )
        rt = item_by_pid[pid].get("request_time_iso") or ""
        if rt and rt > max_cutoff:
            max_cutoff = rt
        if code not in existing_node_codes:
            new_leaf_codes.add(code)
        if l3_code not in existing_node_codes:
            new_leaf_codes.add(l3_code)

    if no_bind:
        print(f"  [--no-bind] 已完成分类 {len(bindings)} 条（L4={len(l4_overlay)}），未回填 PG")
        return {
            "model": model,
            "status": "classified_no_bind",
            "fetched": len(rows),
            "classified": len(bindings),
            "l4_upgraded": len(l4_overlay),
            "bound": 0,
            "rc": 1 if partial_issue else 0,
        }

    bind_stats = backend.bind_clusters(
        bindings,
        tree_id=tree_id,
        cluster_version=ver,
        data_cutoff=max_cutoff or None,
        batch_size=bind_batch_size,
    )
    print(
        f"  回填 PG：提交 {len(bindings)} 条 -> updated={bind_stats['updated']}, "
        f"changes_emitted={bind_stats['changes_emitted']}, cutoff={max_cutoff or '-'}"
    )

    # 命中 5003 cluster_nodes 尚无的休眠叶，或到达每日定时发版窗口 -> 触发整树发版
    pub_triggered = False
    if auto_publish_new_leaf and new_leaf_codes:
        print(f"  检测到首次命中节点 {sorted(new_leaf_codes)[:6]}（共 {len(new_leaf_codes)} 个），自动触发整树发版以同步 5003 cluster_nodes")
        publish_model(backend, model, reason=f"new-leaf:{len(new_leaf_codes)}")
        pub_triggered = True
    else:
        need_pub, reason = should_daily_publish(backend, model, daily_publish_hour)
        if need_pub:
            publish_model(backend, model, reason=reason)
            pub_triggered = True

    rep_ok, rep_nofit = repair_log_delta(repair_log, rep_base)
    row_ledger: dict[str, Any] = {
        "at": now_local().isoformat(timespec="seconds"),
        "channel": "aime",
        "tree": model,
        "mode": "pg_incremental",
        "items": backend.model_counts(model)["bound"],
        "delta": bind_stats["updated"],
        "changes": bind_stats["changes_emitted"],
        "l4_upgraded": len(l4_overlay),
        "elapsed_s": int(time.monotonic() - t0),
        "verify": "partial" if partial_issue else "ok",
        "version": ver,
    }
    if rep_ok or rep_nofit:
        row_ledger["repaired"], row_ledger["nofit"] = rep_ok, rep_nofit
    if unregistered:
        row_ledger["unregistered"] = len(unregistered)
    append_ledger(row_ledger)

    if not partial_issue:
        shutil.rmtree(delta_dir, ignore_errors=True)

    return {
        "model": model,
        "status": "partial_failed" if partial_issue else "ok",
        "fetched": len(rows),
        "valid": len(valid_items),
        "unregistered": len(unregistered),
        "classified": len(bindings),
        "l4_upgraded": len(l4_overlay),
        "bound": bind_stats["updated"],
        "changes_emitted": bind_stats["changes_emitted"],
        "published": pub_triggered,
        "rc": 1 if partial_issue else 0,
    }


def run_backfill_l4_model(
    backend: PGBackend,
    model: str,
    leaves: list[str],
    *,
    force: bool = False,
    limit: int = 5000,
    concurrency: int = 4,
    bind_batch_size: int = 500,
    dry_run: bool = False,
    l4_runner=run_l4_subprocess,
    gateway_checker=CL.healthy_gateways,
) -> dict[str, Any]:
    """对 PG 中落在指定 L3 叶子但尚未下钻 L4（或 --force）的历史条目补跑 L4 并回填。"""
    print(f"\n=== {model} (PG backfill-l4: {','.join(leaves)}) ===")
    rows = backend.fetch_l4_candidates(model, leaves, force=force, limit=limit)
    if not rows:
        print("  无待补钻 L4 条目，跳过")
        return {"model": model, "candidates": 0, "bound": 0, "rc": 0}

    hydrated = hydrate_problems_from_zst(rows, backend.pread_attr)
    work_dir = os.path.join(ROOT, "runs", "_pg_l4", model)
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(os.path.join(work_dir, "assign"), exist_ok=True)

    valid_pids: set[int] = set()
    edge_old: dict[int, bool] = {}
    with (
        open(os.path.join(work_dir, "items.jsonl"), "w", encoding="utf-8") as ifh,
        open(os.path.join(work_dir, "assign", "base.out.jsonl"), "w", encoding="utf-8") as afh,
    ):
        for r in rows:
            pid = int(r["problem_id"])
            prob = hydrated.get(pid) or {}
            tool, _, _ = C.canon_component(C.clean(r.get("component") or prob.get("component")))
            valid_pids.add(pid)
            edge_old[pid] = bool(r.get("is_edge"))
            ifh.write(
                json.dumps(
                    {
                        "seq": pid,
                        "component": tool,
                        "label": C.clean(r.get("label") or prob.get("label")),
                        "definition": C.clean(prob.get("definition")),
                        "reason": C.clean(prob.get("reason")),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            afh.write(
                json.dumps(
                    {"seq": pid, "path": C.clean(r.get("cluster_code")), "edge": bool(r.get("is_edge"))},
                    ensure_ascii=False,
                )
                + "\n"
            )

    print(f"  待补钻 L4 条目：{len(valid_pids)} 条")
    if dry_run:
        print(f"  [dry-run] 已落盘 {work_dir}，未调网关、未回填")
        return {"model": model, "candidates": len(valid_pids), "bound": 0, "rc": 0}

    if not gateway_checker():
        print("  BLOCKED 归类网关全部不可达", file=sys.stderr)
        return {"model": model, "candidates": len(valid_pids), "bound": 0, "rc": 3}

    l4_runner(work_dir, ",".join(leaves), concurrency, force=force)
    _, _, l4nodes = C.taxonomy()
    leaf_tags = C.jload(os.path.join(ROOT, "taxonomy", "leaf_tags.json"), {}) or {}
    overlay = collect_l4_overlay(work_dir, valid_pids, set(l4nodes))
    if not overlay:
        print("  WARN 未产出任何合法 L4 判定", file=sys.stderr)
        return {"model": model, "candidates": len(valid_pids), "bound": 0, "rc": 1}

    cur_tree = backend.current_tree_info(model)
    tree_id = int(cur_tree["tree_id"]) if cur_tree else None
    bindings = [
        {
            "problem_id": pid,
            "cluster_code": code,
            "is_edge": bool(edge_old.get(pid, False) or l4_edge),
            "tags": leaf_tags.get(".".join(code.split(".")[:3]), []),
        }
        for pid, (code, l4_edge) in sorted(overlay.items())
    ]
    stats = backend.bind_clusters(
        bindings,
        tree_id=tree_id,
        cluster_version=pipeline_version(),
        batch_size=bind_batch_size,
    )
    print(f"  L4 回填：提交 {len(bindings)} 条 -> updated={stats['updated']}, changes={stats['changes_emitted']}")
    shutil.rmtree(work_dir, ignore_errors=True)
    return {"model": model, "candidates": len(valid_pids), "bound": stats["updated"], "rc": 0}


# ---------------------------------------------------------------- CLI commands
def resolve_models(backend: PGBackend, models_arg: str, only_arg: str) -> list[str]:
    if only_arg:
        return parse_csv_arg(only_arg)
    if models_arg and models_arg.lower() != "auto":
        return parse_csv_arg(models_arg)
    found = backend.discover_models()
    return found or parse_csv_arg(DEFAULT_MODELS)


def cmd_status(args: argparse.Namespace) -> int:
    backend = PGBackend(
        pg_core_path=args.pg_core_path,
        data_root=args.data_root,
        bind_url=args.bind_url,
        health_url=args.health_url,
    )
    try:
        health = backend.health_check()
        print(f"5003 health: ok={health.get('ok')} pg={health.get('pg')} latest_seq={health.get('counts', {}).get('latest_seq')}")
    except Exception as exc:  # noqa: BLE001
        print(f"5003 health: FAIL ({exc})", file=sys.stderr)

    models = resolve_models(backend, args.models, args.only)
    for m in models:
        cnt = backend.model_counts(m)
        cur = backend.current_tree_info(m)
        tag = cur["release_tag"] if cur else "(none)"
        cutoff = cur.get("data_cutoff") if cur else "-"
        tid = cur["tree_id"] if cur else "-"
        print(
            f"  {m:14s} total={cnt['total']:>7d} bound={cnt['bound']:>7d} "
            f"unclustered={cnt['unclustered']:>5d} l4={cnt['l4_bound']:>7d} "
            f"tree_id={tid} current={tag} cutoff={cutoff}"
        )
    return 0


def cmd_incremental(args: argparse.Namespace) -> int:
    lock = acquire_lock()
    if lock is None:
        print("另一批聚类正在运行，本次跳过（锁 runs/.lock）")
        return 0
    try:
        backend = PGBackend(
            pg_core_path=args.pg_core_path,
            data_root=args.data_root,
            bind_url=args.bind_url,
            health_url=args.health_url,
        )
        models = resolve_models(backend, args.models, args.only)
        l4_leaves = parse_csv_arg(args.l4_leaves)
        worst_rc = 0
        for m in models:
            try:
                res = run_incremental_model(
                    backend,
                    m,
                    limit=args.limit,
                    concurrency=args.concurrency,
                    chunk_size=args.chunk_size,
                    l4_leaves=l4_leaves,
                    bind_batch_size=args.bind_batch_size,
                    dry_run=args.dry_run,
                    no_bind=args.no_bind,
                    auto_publish_new_leaf=not args.no_auto_publish_new_leaf,
                    daily_publish_hour=args.daily_publish_hour,
                )
                rc = int(res.get("rc", 0))
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {m} 异常失败：{exc}", file=sys.stderr)
                rc = 1
            if rc == 3 and worst_rc == 0:
                worst_rc = 3
            elif rc != 0:
                worst_rc = 1
        return worst_rc
    finally:
        lock.close()


def cmd_publish(args: argparse.Namespace) -> int:
    lock = acquire_lock()
    if lock is None:
        print("另一批聚类正在运行，本次跳过（锁 runs/.lock）")
        return 0
    try:
        backend = PGBackend(
            pg_core_path=args.pg_core_path,
            data_root=args.data_root,
            bind_url=args.bind_url,
            health_url=args.health_url,
        )
        models = resolve_models(backend, args.models, args.only)
        rc = 0
        for m in models:
            try:
                publish_model(
                    backend,
                    m,
                    release_tag=args.release_tag or None,
                    rebind_all=args.rebind_all,
                    dry_run=args.dry_run,
                    reason=args.reason,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {m} 发版失败：{exc}", file=sys.stderr)
                rc = 1
        return rc
    finally:
        lock.close()


def cmd_backfill_l4(args: argparse.Namespace) -> int:
    lock = acquire_lock()
    if lock is None:
        print("另一批聚类正在运行，本次跳过（锁 runs/.lock）")
        return 0
    try:
        backend = PGBackend(
            pg_core_path=args.pg_core_path,
            data_root=args.data_root,
            bind_url=args.bind_url,
            health_url=args.health_url,
        )
        models = resolve_models(backend, args.models, args.only)
        leaves = parse_csv_arg(args.leaves)
        if not leaves:
            sys.exit("缺少 --leaves")
        worst_rc = 0
        for m in models:
            res = run_backfill_l4_model(
                backend,
                m,
                leaves,
                force=args.force,
                limit=args.limit,
                concurrency=args.concurrency,
                bind_batch_size=args.bind_batch_size,
                dry_run=args.dry_run,
            )
            if res["rc"] != 0:
                worst_rc = res["rc"]
        return worst_rc
    finally:
        lock.close()


def cmd_recall(args: argparse.Namespace) -> int:
    backend = PGBackend(
        pg_core_path=args.pg_core_path,
        data_root=args.data_root,
        bind_url=args.bind_url,
        health_url=args.health_url,
    )
    matched, sessions, rows = backend.fetch_recall_rows(args.model, args.node, limit=args.limit)
    hydrated = hydrate_problems_from_zst(rows, backend.pread_attr) if args.full else {}
    print(f"model={args.model} node={args.node} matched={matched} sessions={sessions}")
    for r in rows:
        pid = int(r["problem_id"])
        print("-" * 96)
        print(
            f"[problem_id={pid}] {fmt_date_str(r.get('biz_day'))}  "
            f"batch={C.clean(r.get('batch_id'))}  scope={r.get('scope')}[t={r.get('turn_index')},p={r.get('problem_index')}]"
        )
        print(
            f"      case={C.clean(r.get('case_id') or r.get('trace_id'))} "
            f"trace={C.clean(r.get('trace_id'))} session={C.usable_sid(r.get('session_id')) or C.clean(r.get('trace_id'))} "
            f"severity={C.clean(r.get('severity'))}"
        )
        print(f"      question: {C.clean(r.get('question_text'))[:200]}")
        tags_str = ",".join(r.get("tags") or [])
        print(f"      component={C.clean(r.get('component'))}  path={C.clean(r.get('cluster_code'))}  tags={tags_str}")
        print(f"      label: {C.clean(r.get('label'))}")
        if args.full:
            prob = hydrated.get(pid) or {}
            print(f"      definition: {C.clean(prob.get('definition'))[:1000]}")
            print(f"      reason: {C.clean(prob.get('reason'))[:1000]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="AIME PG 16 + Multi-Frame Zstd 原生聚类流水线")
    ap.add_argument("--pg-core-path", default=DEFAULT_PG_CORE_PATH)
    ap.add_argument("--data-root", default=os.environ.get("AIME_DATA_ROOT", ""))
    ap.add_argument("--bind-url", default=DEFAULT_BIND_URL)
    ap.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_st = sub.add_parser("status", help="查看各模型绑定状态与当前发版树")
    p_st.add_argument("--models", default="auto")
    p_st.add_argument("--only", default="")
    p_st.set_defaults(fn=cmd_status)

    p_inc = sub.add_parser("incremental", help="增量拉取 NULL 行 → L3/L4 归类 → /clusters/bind 回填")
    p_inc.add_argument("--models", default=DEFAULT_MODELS)
    p_inc.add_argument("--only", default="")
    p_inc.add_argument("--limit", type=int, default=int(os.environ.get("CLUSTER_PG_BATCH_LIMIT", "5000")))
    p_inc.add_argument("--concurrency", type=int, default=int(os.environ.get("CLUSTER_CONCURRENCY", "4")))
    p_inc.add_argument("--chunk-size", type=int, default=60)
    p_inc.add_argument("--l4-leaves", default=DEFAULT_L4_LEAVES)
    p_inc.add_argument("--bind-batch-size", type=int, default=500)
    p_inc.add_argument("--dry-run", action="store_true")
    p_inc.add_argument("--no-bind", action="store_true")
    p_inc.add_argument("--no-auto-publish-new-leaf", action="store_true")
    p_inc.add_argument("--daily-publish-hour", type=int, default=-1, help="CST 每日自动发版窗口起始小时（如 4；-1 关闭）")
    p_inc.set_defaults(fn=cmd_incremental)

    p_pub = sub.add_parser("publish", help="从 PG 聚合全量树并调 publish-cluster 发版")
    p_pub.add_argument("--models", default=DEFAULT_MODELS)
    p_pub.add_argument("--only", default="")
    p_pub.add_argument("--release-tag", default="")
    p_pub.add_argument("--rebind-all", action="store_true", help="同时导出 item_index.jsonl 并执行全量差异重绑")
    p_pub.add_argument("--dry-run", action="store_true", help="仅聚合校验到本地暂存目录，不调 publish-cluster")
    p_pub.add_argument("--reason", default="manual")
    p_pub.set_defaults(fn=cmd_publish)

    p_l4 = sub.add_parser("backfill-l4", help="针对指定 L3 叶子从 PG 补钻 L4 并回填")
    p_l4.add_argument("--models", default=DEFAULT_MODELS)
    p_l4.add_argument("--only", default="")
    p_l4.add_argument("--leaves", required=True)
    p_l4.add_argument("--force", action="store_true")
    p_l4.add_argument("--limit", type=int, default=5000)
    p_l4.add_argument("--concurrency", type=int, default=int(os.environ.get("CLUSTER_CONCURRENCY", "4")))
    p_l4.add_argument("--bind-batch-size", type=int, default=500)
    p_l4.add_argument("--dry-run", action="store_true")
    p_l4.set_defaults(fn=cmd_backfill_l4)

    p_rec = sub.add_parser("recall", help="按节点 id 从 PG + Zstd 召回原始样本")
    p_rec.add_argument("--model", required=True)
    p_rec.add_argument("--node", required=True)
    p_rec.add_argument("--limit", type=int, default=5)
    p_rec.add_argument("--full", action="store_true")
    p_rec.set_defaults(fn=cmd_recall)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
