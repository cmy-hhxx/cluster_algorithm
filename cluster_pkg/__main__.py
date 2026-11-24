# -*- coding: utf-8 -*-
"""支持 `python -m cluster_pkg -c config.yaml` 直接执行。"""
import sys
from .run_cluster import main

if __name__ == "__main__":
    sys.exit(main())
