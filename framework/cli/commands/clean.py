# -*- coding: utf-8 -*-
"""``zb clean`` —— 清理项目缓存。"""
import os
import shutil

from ..utils import PROG


def do_clean(args, working_dir):
    removed = 0
    if args.py:
        for dirpath, dirs, files in os.walk(working_dir):
            # 跳过 .git / 虚拟环境 / node 产物
            tail = os.path.basename(dirpath)
            if tail in (".git", ".venv", "venv", "node_modules"):
                dirs[:] = [d for d in dirs if d not in (".git", ".venv", "venv", "node_modules")]
                continue
            if tail == "__pycache__":
                shutil.rmtree(dirpath, ignore_errors=True)
                removed += 1
                dirs[:] = []
                continue
            for fn in files:
                if fn.endswith(".pyc") or fn.endswith(".pyo"):
                    try:
                        os.remove(os.path.join(dirpath, fn))
                        removed += 1
                    except OSError:
                        pass
            if tail in (".pytest_cache", "build", "dist") or tail.endswith(".egg-info"):
                shutil.rmtree(dirpath, ignore_errors=True)
                removed += 1
                dirs[:] = []
    if args.npm:
        for dirpath, dirs, _files in os.walk(working_dir):
            if os.path.basename(dirpath) == "node_modules":
                shutil.rmtree(dirpath, ignore_errors=True)
                removed += 1
                dirs[:] = []
    print("%s clean：已清理 %d 处缓存" % (PROG, removed))
    return 0
