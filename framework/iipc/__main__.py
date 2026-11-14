"""`python -m framework.iipc` 入口（等价于 `python startup.py --iipc`）。"""
import sys

from . import main

if __name__ == "__main__":
    sys.exit(main() or 0)
