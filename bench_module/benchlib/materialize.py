# -*- coding: utf-8 -*-
"""从 eval_input 里按 trace_id 取原始 messages（多轮上下文就靠它）。

为什么要有这一层：eval_input 有 590 个文件 / 六七十 G，每次重抽都全扫要二十分钟，
"动态更新"就没人愿意跑了。所以建一份 trace_id → (相对路径, 偏移, 长度) 的定位表，
按文件 size+mtime 增量刷新；之后重抽只 seek 读选中的那几千行。

产物里不写绝对路径，定位表内部也只用相对 eval_input 根的相对路径。
"""
import json
import os
import re

from .common import clean, jdump, jload

try:                      # 不是依赖：机器上有 orjson 就用它加速（350MB/s vs 60MB/s），没有照样跑
    import orjson as _orjson
except ImportError:       # pragma: no cover
    _orjson = None

_TID = re.compile(rb'"trace_id"\s*:\s*"([^"]{1,128})"')
SKIP_DIRS = {".archive", ".runs", ".delivery", ".git", "__pycache__", ".parts", ".merge"}
CACHE_VERSION = 2         # 定位表口径变了就 +1，旧缓存自动失效


class Materializer:
    def __init__(self, eval_root, build_dir, log=print):
        self.root = eval_root
        self.cache_path = os.path.join(build_dir, "trace_loc.tsv")
        self.state_path = os.path.join(build_dir, "trace_loc.state.json")
        self.log = log
        self.loc = {}
        self.state = jload(self.state_path, {})
        self._dirty = False
        self._load()

    # ---------- 定位表 ----------

    def _load(self):
        if not os.path.isfile(self.cache_path):
            return
        with open(self.cache_path, encoding="utf-8") as fh:
            first = fh.readline()
            parts = first.rstrip("\n").split("\t")
            if not parts or parts[0] != "#v%d" % CACHE_VERSION:
                self.log("定位表版本不符，重建")
                os.unlink(self.cache_path)
                self.state = {}
                return
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 4:
                    try:
                        self.loc[parts[0]] = (parts[1], int(parts[2]), int(parts[3]))
                    except ValueError:
                        continue
        self.log("定位表：%d 条" % len(self.loc))

    def save(self):
        if not self._dirty:
            return
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("#v%d\n" % CACHE_VERSION)
            for tid in sorted(self.loc):
                rel, off, ln = self.loc[tid]
                fh.write("%s\t%s\t%d\t%d\n" % (tid, rel, off, ln))
        os.replace(tmp, self.cache_path)
        jdump(self.state, self.state_path)
        self._dirty = False

    @staticmethod
    def _tid_of(line):
        """必须真解析：行内 messages / message_pool 里也会出现 "trace_id" 字样，
        用正则取第一个命中会大面积取错（2026-11-01 expert 实测只对上 12%）。"""
        if _orjson is not None:
            try:
                rec = _orjson.loads(line)
                tid = rec.get("trace_id") or ""
                return tid if isinstance(tid, str) else ""
            except Exception:
                pass
        try:
            rec = json.loads(line.decode("utf-8", "ignore"))
            tid = rec.get("trace_id") or ""
            return tid if isinstance(tid, str) else ""
        except ValueError:
            m = _TID.search(line)          # 坏行兜底：只影响极端个例
            return m.group(1).decode("utf-8", "ignore") if m else ""

    def _scan(self, rel, wanted):
        path = os.path.join(self.root, rel)
        try:
            st = os.stat(path)
        except OSError:
            return 0
        found = 0
        with open(path, "rb") as fh:
            off = 0
            for line in fh:
                n = len(line)
                if line.strip():
                    tid = self._tid_of(line)
                    if tid:
                        if tid not in self.loc:
                            self.loc[tid] = (rel, off, n)
                            self._dirty = True
                        if tid in wanted:
                            found += 1
                off += n
        self.state[rel] = [st.st_size, st.st_mtime_ns]
        self._dirty = True
        return found

    def _iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(".jsonl"):
                    yield os.path.relpath(os.path.join(dirpath, fn), self.root).replace("\\", "/")

    def _reconcile(self):
        """上游会重写/追加 eval_input 文件，旧偏移会指到别的行。

        凡是 size+mtime 变过的文件，先丢掉它名下的所有定位，再整文件重扫。
        """
        stale = []
        for rel, sig in list(self.state.items()):
            try:
                st = os.stat(os.path.join(self.root, rel))
            except OSError:
                stale.append((rel, None))
                continue
            if [st.st_size, st.st_mtime_ns] != sig:
                stale.append((rel, sig))
        if not stale:
            return 0
        went = {rel for rel, _ in stale}
        self.loc = {t: v for t, v in self.loc.items() if v[0] not in went}
        for rel, _ in stale:
            self.state.pop(rel, None)
            if os.path.isfile(os.path.join(self.root, rel)):
                self._scan(rel, set())
        self._dirty = True
        self.log("定位表：%d 个文件被重写，已重扫" % len(stale))
        return len(stale)

    def ensure(self, wanted):
        """wanted: {trace_id: (model, batch_id)}。返回仍然缺的 trace_id 集合。"""
        self._reconcile()
        missing = {t: v for t, v in wanted.items() if t not in self.loc}
        if not missing:
            return set()

        # 1) 按 batch_id 直接点名当天的 eval_input 文件（绝大多数命中）
        hints = set()
        for _tid, (model, batch) in missing.items():
            if not batch:
                continue
            for base in ("0_daily_eval_input", "0_daily_eval_chunks", "2_task_eval"):
                hints.add("%s/%s/%s.jsonl" % (base, model, batch))
        before = len(missing)
        for rel in sorted(hints):
            if os.path.isfile(os.path.join(self.root, rel)):
                self._scan(rel, set(missing))
        missing = {t: v for t, v in missing.items() if t not in self.loc}
        self.log("定位表：点名命中 %d/%d，仍缺 %d" % (before - len(missing), before, len(missing)))
        if not missing:
            return set()

        # 2) 兜底：全量扫（只扫没扫过的文件；扫过的文件内容没变就跳过）
        scanned = 0
        for rel in self._iter_files():
            if not missing:
                break
            path = os.path.join(self.root, rel)
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = self.state.get(rel)
            if prev and prev[0] == st.st_size and prev[1] == st.st_mtime_ns:
                continue
            self._scan(rel, set(missing))
            scanned += 1
            missing = {t: v for t, v in missing.items() if t not in self.loc}
        self.log("定位表：兜底扫 %d 个文件，仍缺 %d" % (scanned, len(missing)))
        return set(missing)

    # ---------- 取原文 ----------

    def read(self, trace_id):
        hit = self.loc.get(trace_id)
        if not hit:
            return None
        rel, off, ln = hit
        path = os.path.join(self.root, rel)
        try:
            with open(path, "rb") as fh:
                fh.seek(off)
                raw = fh.read(ln)
            rec = json.loads(raw.decode("utf-8", "ignore"))
        except (OSError, ValueError):
            return None
        # 偏移失效时会读到别人的行：核对身份，宁缺勿错
        if clean(rec.get("trace_id")) != trace_id:
            return None
        return rec
