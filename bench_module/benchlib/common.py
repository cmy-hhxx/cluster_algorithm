# -*- coding: utf-8 -*-
"""bench_module 公共层：JSON 读写、问句归一化、分类法映射。

只依赖标准库。弗吉尼亚机器上只有 python3.12 标准库 + orjson，没有 pandas/numpy。
所以这里一个第三方 import 都不许有。
"""
import hashlib
import json
import os
import re
import tempfile
import unicodedata

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TAXONOMY_DIR = os.path.join(REPO_ROOT, "taxonomy")

_DROP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_ISO_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def jload(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="surrogatepass") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {} if default is None else default


def jdump(obj, path, indent=1):
    """原子写：先写同目录临时文件再 replace，读的人永远看不到半个文件。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_jsonl(path):
    """按行读 JSONL，坏行跳过（上游偶尔会有被截断的尾行）。"""
    with open(path, encoding="utf-8", errors="surrogatepass") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def clean(s):
    """统一成 str，并把孤立代理字符处理掉（上游中文里偶发）。"""
    if s is None:
        return ""
    if not isinstance(s, str):
        return str(s)
    if any("\ud800" <= c <= "\udfff" for c in s):
        return s.encode("utf-8", "ignore").decode("utf-8")
    return s


def iso_time(v):
    v = clean(v).strip()
    if not v:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
        return v
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$", v)
    return "%sT%s+08:00" % (m.group(1), m.group(2)) if m else ""


def request_day(v):
    t = iso_time(v)
    m = _ISO_DAY.match(t)
    return m.group(1) if m else ""


def norm_question(q):
    """问句归一化：NFKC + 小写 + 只留中日韩/字母/数字。

    用途只有一个：判断"两个 trace 是不是同一个问题"，给去重用。
    不参与任何哈希身份（bad_case_id 另有口径），所以这里不需要时间。
    """
    s = unicodedata.normalize("NFKC", clean(q)).strip().lower()
    return _DROP.sub("", s)


def stable_hash(seed, key):
    """确定性排序键：同一 (seed, trace_id) 永远得到同一个值，跨机器、跨重跑一致。"""
    return hashlib.md5(("%s\x1f%s" % (seed, key)).encode("utf-8")).hexdigest()


def round_bucket(n):
    """会话轮次分桶。"""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 10:
        return "4-10"
    return "11+"


class Taxonomy:
    """分类法只读映射：叶 id → (名称, L1, L2)、L1/L2 中文名、component → 段。

    读的是聚类仓库里的 taxonomy/*.json。只读，不写，不改聚类流程。
    """

    def __init__(self, root=None):
        d = root or TAXONOMY_DIR
        inv = jload(os.path.join(d, "inventory.json"), {})
        self.l1 = {k: (v or {}).get("name", k) for k, v in (inv.get("l1") or {}).items()}
        self.l2 = {k: (v or {}).get("name", k) for k, v in (inv.get("l2") or {}).items()}
        self.leaf = {}
        for node in inv.get("nodes") or []:
            lid = clean(node.get("id"))
            if lid:
                self.leaf[lid] = {
                    "name": clean(node.get("name")),
                    "l1": clean(node.get("l1")) or lid.split(".")[0],
                    "l2": clean(node.get("l2")) or ".".join(lid.split(".")[:2]),
                }
        self.stage = {}  # component 写法 → 段
        for key, meta in (jload(os.path.join(d, "components.json"), {}) or {}).items():
            meta = meta or {}
            st = clean(meta.get("stage"))
            if not st:
                continue
            self.stage[clean(key)] = st
            tool = clean(meta.get("tool")) or clean(key).split(" ")[0]
            self.stage.setdefault(tool, st)

    def stage_name(self, s):
        return self.l1.get(s, s or "NA")

    def mech_name(self, m):
        return self.l2.get(m, m or "NA")

    def leaf_name(self, lid):
        return (self.leaf.get(lid) or {}).get("name", "")

    def stage_of_component(self, comp):
        """只用于 join 失败时的兜底：把 component 映射回 L1 段。"""
        c = clean(comp).strip()
        if not c:
            return ""
        if c in self.stage:
            return self.stage[c]
        tool = c.split(" ")[0]
        return self.stage.get(tool, "")
