#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步主仓分类法/提示词至 cluster_pkg、执行防漂移自检，并生成 .whl 与 .tar.gz 分发产物。"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "cluster_pkg")
TAX_SRC = os.path.join(ROOT, "taxonomy")
TAX_DST = os.path.join(PKG_DIR, "builtin_taxonomy")
PROMPTS_SRC = os.path.join(ROOT, "prompts")
PROMPTS_DST = os.path.join(PKG_DIR, "prompts")
SYNC_PROMPTS = ("01_assign.md", "03_l4_assign.md")
FORBIDDEN_PROMPTS = ("02_l4_induct.md",)

SKIP_TAR_DIRS = {"__pycache__", ".git", ".pytest_cache", "build", "dist"}


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_versions() -> dict:
    with open(os.path.join(ROOT, "VERSION.json"), encoding="utf-8") as fh:
        ver_json = json.load(fh)["version"]
    with open(os.path.join(PKG_DIR, "engine.py"), encoding="utf-8") as fh:
        m_eng = re.search(r'^__version__\s*=\s*"([^"]+)"', fh.read(), re.M)
    with open(os.path.join(PKG_DIR, "pyproject.toml"), encoding="utf-8") as fh:
        m_pyp = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
    with open(os.path.join(PKG_DIR, "_pep517.py"), encoding="utf-8") as fh:
        m_pep = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    return {
        "VERSION.json": ver_json,
        "cluster_pkg/engine.py": m_eng.group(1) if m_eng else "",
        "cluster_pkg/pyproject.toml": m_pyp.group(1) if m_pyp else "",
        "cluster_pkg/_pep517.py": m_pep.group(1) if m_pep else "",
    }


def sync_assets() -> None:
    """将主仓 taxonomy/ 与指定 prompts 同步进 cluster_pkg/，并清理废弃/多余文件。"""
    os.makedirs(TAX_DST, exist_ok=True)
    src_tax_files = {
        fn for fn in os.listdir(TAX_SRC)
        if os.path.isfile(os.path.join(TAX_SRC, fn)) and not fn.startswith(".")
    }
    dst_tax_files = {
        fn for fn in os.listdir(TAX_DST)
        if os.path.isfile(os.path.join(TAX_DST, fn))
    }
    for extra in sorted(dst_tax_files - src_tax_files):
        os.unlink(os.path.join(TAX_DST, extra))
    for fn in sorted(src_tax_files):
        shutil.copy2(os.path.join(TAX_SRC, fn), os.path.join(TAX_DST, fn))

    os.makedirs(PROMPTS_DST, exist_ok=True)
    for fn in SYNC_PROMPTS:
        sp = os.path.join(PROMPTS_SRC, fn)
        if not os.path.isfile(sp):
            raise FileNotFoundError(f"主仓缺少必需提示词: {sp}")
        shutil.copy2(sp, os.path.join(PROMPTS_DST, fn))
    for fn in FORBIDDEN_PROMPTS:
        fp = os.path.join(PROMPTS_DST, fn)
        if os.path.exists(fp):
            os.unlink(fp)


def check_alignment() -> str:
    """强校验：分类法一致性、提示词一致性、版本号一致性、builtin_taxonomy lint 零错误。"""
    errors = []

    src_tax = sorted(
        fn for fn in os.listdir(TAX_SRC)
        if os.path.isfile(os.path.join(TAX_SRC, fn)) and not fn.startswith(".")
    )
    dst_tax = sorted(
        fn for fn in os.listdir(TAX_DST)
        if os.path.isfile(os.path.join(TAX_DST, fn)) and not fn.startswith(".")
    )
    if src_tax != dst_tax:
        errors.append(
            f"builtin_taxonomy 文件集合与 taxonomy/ 不一致: "
            f"missing={sorted(set(src_tax) - set(dst_tax))}, extra={sorted(set(dst_tax) - set(src_tax))}"
        )
    else:
        for fn in src_tax:
            if _sha256_file(os.path.join(TAX_SRC, fn)) != _sha256_file(os.path.join(TAX_DST, fn)):
                errors.append(f"taxonomy/{fn} 与 cluster_pkg/builtin_taxonomy/{fn} 内容不一致")

    for fn in SYNC_PROMPTS:
        sp = os.path.join(PROMPTS_SRC, fn)
        dp = os.path.join(PROMPTS_DST, fn)
        if not os.path.isfile(dp):
            errors.append(f"cluster_pkg/prompts/{fn} 缺失")
        elif _sha256_file(sp) != _sha256_file(dp):
            errors.append(f"prompts/{fn} 与 cluster_pkg/prompts/{fn} 内容不一致")
    for fn in FORBIDDEN_PROMPTS:
        if os.path.exists(os.path.join(PROMPTS_DST, fn)):
            errors.append(f"cluster_pkg/prompts/{fn} 不应存在（当前未被代码调用）")

    versions = _read_versions()
    uniq_vers = set(versions.values())
    if len(uniq_vers) != 1 or not next(iter(uniq_vers)):
        errors.append(f"版本号不一致: {versions}")

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from cluster_pkg.tax_store import TaxonomyStore

    lint_errs, _ = TaxonomyStore(TAX_DST).lint()
    if lint_errs:
        errors.append(f"builtin_taxonomy lint 失败: {lint_errs[:3]}")

    if errors:
        raise RuntimeError("防漂移自检失败:\n- " + "\n- ".join(errors))
    return next(iter(uniq_vers))


def build_tarball(out_dir: str, version: str) -> str:
    tar_name = f"cluster_pkg-{version}.tar.gz"
    tar_path = os.path.join(out_dir, tar_name)

    def _filter(ti: tarfile.TarInfo):
        parts = ti.name.split("/")
        base = parts[-1]
        if any(p in SKIP_TAR_DIRS or p.endswith((".egg-info", ".dist-info")) for p in parts):
            return None
        if base == ".DS_Store" or base.endswith((".pyc", ".pyo")):
            return None
        return ti

    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(PKG_DIR, arcname="cluster_pkg", filter=_filter)
    return tar_path


def build_packages(out_dir: str, do_sync: bool = True):
    if do_sync:
        sync_assets()
    version = check_alignment()
    os.makedirs(out_dir, exist_ok=True)

    if PKG_DIR not in sys.path:
        sys.path.insert(0, PKG_DIR)
    import _pep517

    whl_name = _pep517.build_wheel(out_dir)
    whl_path = os.path.join(out_dir, whl_name)
    tar_path = build_tarball(out_dir, version)

    print(f"check OK (version={version})")
    for p in (whl_path, tar_path):
        size_kb = os.path.getsize(p) / 1024.0
        print(f"BUILT {os.path.relpath(p, ROOT)} ({size_kb:.1f} KB, sha256={_sha256_file(p)})")
    print(f"离线 pip 安装: pip install --no-index --no-build-isolation {os.path.relpath(whl_path, ROOT)}")
    print(f"免安装解压运行: tar -xzf {os.path.relpath(tar_path, ROOT)} && python3 cluster_pkg/run_cluster.py -c cluster_pkg/config.example.yaml")
    return whl_path, tar_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同步并打包 cluster_pkg 分发产物（.whl + .tar.gz）")
    ap.add_argument("--sync", action="store_true", help="仅同步主仓 taxonomy 与 prompts 至 cluster_pkg 并退出")
    ap.add_argument("--no-sync", action="store_true", help="构建前跳过同步（直接基于现有文件校验并打包）")
    ap.add_argument("--check", action="store_true", help="仅执行防漂移与 lint 自检，不写文件")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "dist"), help="分发产物输出目录（默认 dist/）")
    args = ap.parse_args(argv)

    if args.check:
        ver = check_alignment()
        print(f"check OK (version={ver})")
        return 0

    if args.sync:
        sync_assets()
        ver = check_alignment()
        print(f"sync + check OK (version={ver})")
        return 0

    build_packages(os.path.abspath(args.out_dir), do_sync=not args.no_sync)
    return 0


if __name__ == "__main__":
    sys.exit(main())
