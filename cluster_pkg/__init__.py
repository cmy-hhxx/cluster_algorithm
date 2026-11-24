# -*- coding: utf-8 -*-
"""cluster_pkg — 零外部依赖、自包含、支持自适应演进的归因聚类可执行模块。

Python 调用示例：
    from cluster_pkg import run_cluster

    res = run_cluster("config.yaml")
"""
from .cfg import AdaptiveConfig, ApiConfig, ClusterConfig, ClusterParams, L4Config
from .engine import ClusterEngine, __version__, run_cluster
from .tax_store import TaxonomyStore

__all__ = [
    "__version__",
    "ApiConfig",
    "ClusterParams",
    "AdaptiveConfig",
    "L4Config",
    "ClusterConfig",
    "ClusterEngine",
    "TaxonomyStore",
    "run_cluster",
]
