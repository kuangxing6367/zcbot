# -*- coding: utf-8 -*-
"""``zb driver`` —— 管理驱动器（Web 后台所用的服务器驱动）。

ZCBOT 的「驱动器」即 Web 管理后台依赖的服务器实现（waitress / flask 等），
与 pip 包一一对应。list 展示可用驱动器；install / uninstall 走 pip。
"""
from ..utils import PROG, run_python

KNOWN_DRIVERS = [
    ("waitress", "waitress>=3.0.0", "生产级纯 Python WSGI 服务器（默认）"),
    ("flask", "flask>=2.3.0", "Flask 开发服务器 / 嵌入式后端"),
]


def do_driver(args, working_dir):
    action = args.action
    if action in (None, "list"):
        return _list()
    if action == "search":
        print("%s driver search —— 可用驱动器：" % PROG)
        return _list()
    if action == "install":
        return _pip("install", args.id)
    if action == "uninstall":
        return _pip("uninstall", args.id)
    print("未知 driver 子命令: %s" % action)
    return 2


def _list():
    rows = [[d[0], d[1], d[2]] for d in KNOWN_DRIVERS]
    print("%s driver list" % PROG)
    from ..utils import render_table
    print(render_table(rows, ["ID", "SPEC", "DESC"]))
    return 0


def _pip(verb, driver_id):
    spec = None
    for d in KNOWN_DRIVERS:
        if d[0] == driver_id:
            spec = d[1]
            break
    if spec is None:
        print("错误：未知驱动器: %s（可用：%s）" %
              (driver_id, ", ".join(d[0] for d in KNOWN_DRIVERS)))
        return 1
    if verb == "uninstall":
        cmd = ["-m", "pip", "uninstall", "-y", spec.split(">=")[0]]
    else:
        cmd = ["-m", "pip", "install", spec]
    code, out = run_python(cmd)
    print(out)
    return code
