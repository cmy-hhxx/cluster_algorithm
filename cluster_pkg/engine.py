# -*- coding: utf-8 -*-
"""cluster_pkg 主编排引擎：串联锁保护、基线树加载、自适应组件/新叶演进、L4 下钻与原子交付。"""
import contextlib
import os
import shutil
import tempfile
import time
from typing import Any, Dict, Union

from .adaptive_leaf import induct_and_assign_missing_leaves, resolve_unknown_components
from .cfg import ClusterConfig
from .classifier import BatchClassifier
from .ingestor import load_baseline_from_tree, scan_raw_items
from .l4_drill import apply_l4_drilldown
from .llm_client import ResponsesClient
from .reporter import aggregate_and_deliver
from .tax_store import TaxonomyStore

__version__ = "4.5.0"


@contextlib.contextmanager
def _file_lock(lock_path: str):
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fh = open(lock_path, "w", encoding="utf-8")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except BlockingIOError as e:
            raise RuntimeError(f"另一聚类进程正在操作同一目标目录（锁文件: {lock_path}）") from e
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


class ClusterEngine:
    def __init__(self, cfg: ClusterConfig, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True))

    def run(self) -> Dict[str, Any]:
        t0 = time.time()
        out_dir = os.path.abspath(self.cfg.output_dir)
        out_parent = os.path.dirname(out_dir) or "."
        os.makedirs(out_parent, exist_ok=True)

        lock_file = os.path.join(self.cfg.work_dir or out_parent, f".cluster_{os.path.basename(out_dir)}.lock")
        with _file_lock(lock_file):
            return self._run_locked(out_dir, out_parent, t0)

    def _run_locked(self, out_dir: str, out_parent: str, t0: float) -> Dict[str, Any]:
        staging_dir = tempfile.mkdtemp(prefix=f".cluster_stage_{os.path.basename(out_dir)}_", dir=out_parent)
        try:
            # 1) 初始化并校验工作分类法（吸收既有聚类树中的历史自定义节点）
            stage_tax_dir = os.path.join(staging_dir, "taxonomy")
            tax = TaxonomyStore.initialize(
                target_tax_dir=stage_tax_dir,
                custom_tax_dir=self.cfg.taxonomy_dir,
                existing_tree_dir=self.cfg.tree_dir,
            )
            lint_errs, _ = tax.lint()
            if lint_errs:
                raise RuntimeError(f"初始分类法未通过 lint 自检: {lint_errs[:3]}")

            # 2) 加载既有聚类树基线（增量与 rebuild 复用）
            baseline = {}
            if self.cfg.mode in ("auto", "incremental", "rebuild"):
                baseline = load_baseline_from_tree(self.cfg.tree_dir)
            effective_mode = (
                "incremental" if (self.cfg.mode == "auto" and baseline)
                else ("full" if self.cfg.mode == "auto" else self.cfg.mode)
            )
            self.log(
                f"cluster_pkg v{__version__} 启动 | mode={effective_mode} | "
                f"历史树条目={len(baseline)} | 分类法叶子={len(tax.inv['nodes'])}"
            )

            # 3) 流式扫描输入归因数据（文件/目录或内存 records）
            raw_items, unknown_samples, ingest_stats = scan_raw_items(
                self.cfg.input_path, self.cfg.cases_path, self.cfg.channel, tax, records=self.cfg.records
            )
            self.log(
                f"摄入完成：文件={ingest_stats['files']} 条目={len(raw_items)} "
                f"重复跳过={ingest_stats.get('dup_items', 0)} 未知组件={len(unknown_samples)}"
            )

            if self.cfg.dry_run:
                return {
                    "status": "dry_run",
                    "mode": effective_mode,
                    "output_dir": out_dir,
                    "ingested_items": len(raw_items),
                    "baseline_items": len(baseline),
                    "unknown_components": sorted(unknown_samples.keys()),
                    "elapsed_sec": round(time.time() - t0, 2),
                }

            client = ResponsesClient(self.cfg.api, logger=self.log)
            client.check_ready()

            # 拷贝已有演进日志（若有）以便追加
            evo_log = os.path.join(staging_dir, "taxonomy_evolution.jsonl")
            for prev_dir in (out_dir, self.cfg.tree_dir):
                if prev_dir and os.path.isfile(os.path.join(prev_dir, "taxonomy_evolution.jsonl")):
                    shutil.copy2(os.path.join(prev_dir, "taxonomy_evolution.jsonl"), evo_log)
                    break

            # 4) 未知组件处理（自适应路由登记 vs 严格阻断）
            created_components = []
            if unknown_samples:
                if self.cfg.adaptive.enabled and self.cfg.adaptive.auto_component:
                    created_components = resolve_unknown_components(
                        unknown_samples, client, tax, evo_log, self.log
                    )
                    for it in raw_items:
                        if not it.get("stage"):
                            tool, comp_lbl, st, role_t, _ok = tax.canon_component(it["component_raw"])
                            it["component"] = tool
                            it["component_label"] = comp_lbl
                            it["stage"] = st
                            if not it.get("component_role") and role_t:
                                it["component_role"] = role_t
                else:
                    names = ", ".join(sorted(unknown_samples.keys()))
                    raise RuntimeError(
                        f"发现未登记 component: {names}（可开启 adaptive.auto_component 自动登记，或写入 components.json）"
                    )

            # 5) 组装全量条目表与待归类增量队列
            final_items = []
            assignments = {}
            todo_items = []
            reused_count = 0

            if effective_mode == "incremental":
                raw_by_key = {it["key"]: it for it in raw_items}
                for key, b_it in baseline.items():
                    merged_it = dict(b_it)
                    # 若本轮输入再次出现同一 trace，用最新输入补全可能缺失的展示元数据，但严格保留历史判定路径
                    fresh = raw_by_key.get(key)
                    if fresh:
                        for fld in ("component", "component_raw", "label", "question", "request_time", "date", "severity"):
                            if not merged_it.get(fld) and fresh.get(fld):
                                merged_it[fld] = fresh[fld]
                    seq = len(final_items) + 1
                    merged_it["seq"] = seq
                    final_items.append(merged_it)
                    assignments[seq] = (b_it["path"], b_it["edge"])
                    reused_count += 1

                for it in raw_items:
                    if it["key"] in baseline:
                        continue
                    seq = len(final_items) + 1
                    it = dict(it)
                    it["seq"] = seq
                    final_items.append(it)
                    todo_items.append(it)

                if (
                    not todo_items
                    and os.path.abspath(self.cfg.tree_dir) == out_dir
                    and os.path.isfile(os.path.join(out_dir, "verify.json"))
                ):
                    self.log("增量检测：无新增条目，直接复用既有聚类树 (noop)")
                    return {
                        "status": "noop",
                        "mode": effective_mode,
                        "output_dir": out_dir,
                        "total_items": len(final_items),
                        "reused_items": reused_count,
                        "new_items": 0,
                        "total_sessions": len({i["session_id"] for i in final_items}),
                        "leaves_used": len({p for p, _ in assignments.values()}),
                        "taxonomy_leaves": len(tax.inv["nodes"]),
                        "created_components": [],
                        "created_leaves": [],
                        "l4_updated": 0,
                        "api_calls": client.calls,
                        "elapsed_sec": round(time.time() - t0, 2),
                    }
            else:
                for it in raw_items:
                    seq = len(final_items) + 1
                    it = dict(it)
                    it["seq"] = seq
                    final_items.append(it)
                    old = baseline.get(it["key"]) if effective_mode == "rebuild" else None
                    if old and (not old.get("label") or old["label"] == it["label"]):
                        assignments[seq] = (old["path"], old["edge"])
                        reused_count += 1
                    else:
                        todo_items.append(it)

            if not final_items:
                raise RuntimeError("输入中没有可聚类的有效 problem 条目")

            # 6) 批量并发归类
            chunk_size = self.cfg.cluster.chunk_size or (60 if effective_mode == "incremental" else 200)
            allow_nofit = self.cfg.adaptive.enabled and self.cfg.adaptive.auto_leaf
            classifier = BatchClassifier(client, tax, self.cfg.cluster, allow_nofit=allow_nofit, logger=self.log)
            newly_assigned, nofit_by_l1 = classifier.classify_all(todo_items, chunk_size=chunk_size)
            assignments.update(newly_assigned)

            # 7) 缺叶自适应归纳与指派
            created_leaves = []
            if nofit_by_l1:
                total_nofit = sum(len(v) for v in nofit_by_l1.values())
                if not allow_nofit or total_nofit < self.cfg.adaptive.min_leaf_items:
                    raise RuntimeError(
                        f"存在 {total_nofit} 条无法落入现有分类法的条目（阈值 min_leaf_items={self.cfg.adaptive.min_leaf_items}）"
                    )
                self.log(f"触发自适应建叶：共 {total_nofit} 条跨 {len(nofit_by_l1)} 个环节")
                ad_assigned, created_leaves = induct_and_assign_missing_leaves(
                    nofit_by_l1, client, tax, evo_log, self.log
                )
                assignments.update(ad_assigned)

            # 8) L4 第四层子类下钻
            items_by_seq = {it["seq"]: it for it in final_items}
            newly_seqs = {it["seq"] for it in todo_items}
            l4_updated = apply_l4_drilldown(
                items_by_seq=items_by_seq,
                assignments=assignments,
                newly_classified_seqs=newly_seqs,
                client=client,
                tax=tax,
                cfg=self.cfg.l4,
                concurrency=self.cfg.cluster.concurrency,
                logger=self.log,
            )

            # 9) 聚合、verify 强校验与报告生成（全部在 staging_dir 内完成，校验通过才替换正式目录）
            ver = aggregate_and_deliver(
                items_list=final_items,
                assignments=assignments,
                tax=tax,
                out_dir=staging_dir,
                version=__version__,
                sessions_all=self.cfg.cluster.sessions_all,
                font_path=self.cfg.cluster.font_path,
            )

            # 10) 原子提交产物到正式 output_dir
            os.makedirs(out_dir, exist_ok=True)
            dst_tax = os.path.join(out_dir, "taxonomy")
            os.makedirs(dst_tax, exist_ok=True)
            for fn in os.listdir(stage_tax_dir):
                sp = os.path.join(stage_tax_dir, fn)
                if os.path.isfile(sp):
                    tmp_dst = os.path.join(dst_tax, fn + ".tmp")
                    shutil.copy2(sp, tmp_dst)
                    os.replace(tmp_dst, os.path.join(dst_tax, fn))

            for fn in os.listdir(staging_dir):
                sp = os.path.join(staging_dir, fn)
                if os.path.isfile(sp):
                    tmp_dst = os.path.join(out_dir, fn + ".tmp")
                    shutil.copy2(sp, tmp_dst)
                    os.replace(tmp_dst, os.path.join(out_dir, fn))

            elapsed = round(time.time() - t0, 2)
            self.log(
                f"完成！总条目={len(final_items)}（复用 {reused_count} / 新判 {len(todo_items)}）| "
                f"新建组件={len(created_components)} | 新建叶子={len(created_leaves)} | "
                f"verify err={ver['errors']} mismatch={ver['locator_mismatch']} | 耗时 {elapsed}s -> {out_dir}"
            )
            return {
                "status": "ok",
                "mode": effective_mode,
                "output_dir": out_dir,
                "total_items": len(final_items),
                "reused_items": reused_count,
                "new_items": len(todo_items),
                "total_sessions": len({i["session_id"] for i in final_items}),
                "leaves_used": ver["leaves"],
                "taxonomy_leaves": len(tax.inv["nodes"]),
                "created_components": created_components,
                "created_leaves": created_leaves,
                "l4_updated": l4_updated,
                "api_calls": client.calls,
                "verify": ver,
                "elapsed_sec": elapsed,
            }
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


def run_cluster(config: Union[str, Dict[str, Any], ClusterConfig], logger=None) -> Dict[str, Any]:
    """外部 Python 项目一键调用入口：支持传入 config.yaml 路径、字典或 ClusterConfig。"""
    cfg = ClusterConfig.from_source(config)
    return ClusterEngine(cfg, logger=logger).run()
