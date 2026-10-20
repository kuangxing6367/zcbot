# -*- coding: utf-8 -*-
"""``zb run`` —— 运行机器人（可选热重载）。"""
import os
import subprocess
import sys
import time


def _spawn(python_path, file_path, working_dir):
    return subprocess.Popen(
        [python_path, file_path],
        cwd=working_dir,
        stdout=None, stderr=None,
    )


def _watch_files(working_dir, extra_dirs):
    """返回 {path: mtime} 快照（仅 *.py）。"""
    roots = [working_dir]
    if extra_dirs:
        roots.extend(d.rstrip("/\\") for d in extra_dirs.split(",") if d.strip())
    snap = {}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            if ".git" in dirpath.split(os.sep) or "node_modules" in dirpath.split(os.sep):
                continue
            for fn in files:
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    try:
                        snap[p] = os.path.getmtime(p)
                    except OSError:
                        pass
    return snap


def do_run(args, working_dir):
    """运行入口；--reload 时监听 *.py 变更并重启子进程。"""
    file_path = args.file
    if not os.path.isfile(os.path.join(working_dir, file_path)):
        print("错误：入口文件不存在: %s（工作目录 %s）" % (file_path, working_dir))
        return 2

    python_path = args.python_path or sys.executable
    if not args.reload:
        # 直接前台运行（继承 stdio，Ctrl-C 即退出）
        code = subprocess.call([python_path, file_path], cwd=working_dir)
        return code

    print("热重载模式（--reload）：Ctrl-C 退出")
    snap = _watch_files(working_dir, getattr(args, "reload_dirs", None))
    proc = _spawn(python_path, file_path, working_dir)
    try:
        while True:
            time.sleep(args.reload_delay or 0.5)
            if proc.poll() is not None:
                # 子进程自行退出，跟随退出
                return proc.returncode
            new_snap = _watch_files(working_dir, getattr(args, "reload_dirs", None))
            changed = [p for p in new_snap if snap.get(p) != new_snap[p]
                       and p not in snap] + \
                      [p for p in snap if p not in new_snap]
            if changed:
                print("\n[reload] 检测到变更，重启…")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                snap = _watch_files(working_dir, getattr(args, "reload_dirs", None))
                proc = _spawn(python_path, file_path, working_dir)
    except KeyboardInterrupt:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print()
        return 0
