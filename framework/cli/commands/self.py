# -*- coding: utf-8 -*-
"""``zb self`` —— 管理 ZCBOT 实例自身（pip 安装 / 卸载 / 升级）。"""
from ..utils import PROG, run_python


def do_self(args, working_dir):
    action = args.action
    if action in (None, "list"):
        return _list()
    if action == "install":
        spec = getattr(args, "spec", None) or "zcbot"
        return _pip(["install", spec])
    if action == "upgrade":
        return _pip(["install", "-U", "zcbot"])
    if action == "uninstall":
        return _pip(["uninstall", "-y", "zcbot"])
    print("未知 self 子命令: %s" % action)
    return 2


def _list():
    code, out = run_python(["-m", "pip", "show", "zcbot"])
    if code != 0 or "Name:" not in out:
        print("%s self list：未检测到以包形式安装的 zcbot（源码方式运行无需安装）" % PROG)
        return 0
    print(out)
    return 0


def _pip(pip_args):
    code, out = run_python(["-m", "pip"] + list(pip_args))
    print(out)
    return code
