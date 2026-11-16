#!/usr/bin/env python3
"""演练启动器（以 python3 -I 运行，避免任何本地模块污染）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dim_admission.demo import run_demo  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_demo(keep_db="--keep-db" in sys.argv))
