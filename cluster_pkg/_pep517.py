# -*- coding: utf-8 -*-
"""纯标准库 PEP 517 / PEP 660 构建后端：无需联网、无需安装 setuptools/wheel 即可 pip install。"""
import base64
import hashlib
import os
import zipfile

NAME = "cluster_pkg"
DIST_NAME = "cluster_pkg"
VERSION = "4.6.0"
DIST_INFO = f"{DIST_NAME}-{VERSION}.dist-info"

METADATA = f"""Metadata-Version: 2.1
Name: cluster-pkg
Version: {VERSION}
Summary: Zero-dependency portable attribution clustering service (OpenAI Responses/Chat API + adaptive taxonomy)
Requires-Python: >=3.9
"""

WHEEL_META = """Wheel-Version: 1.0
Generator: cluster_pkg._pep517
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY_POINTS = """[console_scripts]
cluster-run = cluster_pkg.run_cluster:main
"""


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"{arcname},sha256={digest},{len(data)}"


def _iter_pkg_files(src_root: str):
    skip_dirs = {"__pycache__", ".git", ".pytest_cache", "build", "dist", "tests"}
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in skip_dirs and not d.startswith(".") and not d.endswith(".egg-info") and not d.endswith(".dist-info")
        )
        rel_dir = os.path.relpath(dirpath, src_root)
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")) or fn in ("pyproject.toml", "_pep517.py"):
                continue
            full = os.path.join(dirpath, fn)
            arc = f"{NAME}/{fn}" if rel_dir == "." else f"{NAME}/{rel_dir.replace(os.sep, '/')}/{fn}"
            yield full, arc


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    src_root = os.path.dirname(os.path.abspath(__file__))
    whl_name = f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
    whl_path = os.path.join(wheel_directory, whl_name)
    records = []
    with zipfile.ZipFile(whl_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for full, arc in _iter_pkg_files(src_root):
            with open(full, "rb") as fh:
                data = fh.read()
            zf.writestr(arc, data)
            records.append(_record_line(arc, data))
        for meta_fn, content in (("METADATA", METADATA), ("WHEEL", WHEEL_META), ("entry_points.txt", ENTRY_POINTS)):
            arc = f"{DIST_INFO}/{meta_fn}"
            b = content.encode("utf-8")
            zf.writestr(arc, b)
            records.append(_record_line(arc, b))
        rec_arc = f"{DIST_INFO}/RECORD"
        records.append(f"{rec_arc},,")
        zf.writestr(rec_arc, ("\n".join(records) + "\n").encode("utf-8"))
    return whl_name


def build_editable(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    src_root = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(src_root)
    whl_name = f"{DIST_NAME}-{VERSION}-0.editable-py3-none-any.whl"
    whl_path = os.path.join(wheel_directory, whl_name)
    records = []
    with zipfile.ZipFile(whl_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        pth_arc = f"{DIST_NAME}.pth"
        pth_bytes = (parent_dir + "\n").encode("utf-8")
        zf.writestr(pth_arc, pth_bytes)
        records.append(_record_line(pth_arc, pth_bytes))
        for meta_fn, content in (("METADATA", METADATA), ("WHEEL", WHEEL_META), ("entry_points.txt", ENTRY_POINTS)):
            arc = f"{DIST_INFO}/{meta_fn}"
            b = content.encode("utf-8")
            zf.writestr(arc, b)
            records.append(_record_line(arc, b))
        rec_arc = f"{DIST_INFO}/RECORD"
        records.append(f"{rec_arc},,")
        zf.writestr(rec_arc, ("\n".join(records) + "\n").encode("utf-8"))
    return whl_name
