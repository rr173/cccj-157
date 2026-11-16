#!/usr/bin/env python3
"""隔离模式启动器：-I 不把脚本目录加入 sys.path，这里显式注入项目根。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dim_admission.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
