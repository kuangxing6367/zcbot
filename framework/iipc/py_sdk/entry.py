# -*- coding: utf-8 -*-
"""插件进程入口：由 Go 主进程拉起，加载指定插件目录的 main.py 并接入 IIPC。"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 编码兜底：Windows 上 Python 默认按 GBK 编码 stdout，经内核透传后被上游按 UTF-8
# 读成乱码（�）。在最早入口强制 UTF-8（内核侧也会注入 PYTHONIOENCODING，双保险）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sdk import run_plugin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin-dir", required=True)
    ap.add_argument("--iipc-addr", required=True)
    ap.add_argument("--plugin-id", required=True)
    ap.add_argument("--iipc-auth", default="")
    a = ap.parse_args()
    run_plugin(a.plugin_dir, a.plugin_id, a.iipc_addr, a.iipc_auth)


if __name__ == "__main__":
    main()
