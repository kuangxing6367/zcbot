# -*- coding: utf-8 -*-
"""``zb adapter`` —— 管理适配器（list / search / install / uninstall）。

ZCBOT 的适配器即 core_plugins 下带网络接入特征的条目，
故适配器管理与插件管理共用同一份 ``core_plugins.yaml`` 启用开关。
"""
import webbrowser

from ..utils import (PROG, discover_adapters, plugin_enabled_state,
                     set_core_plugin_enabled, render_table)

MARKET_URL = "https://github.com/kuangxing6367/zcbot"


def do_adapter(args, working_dir):
    action = args.action
    if action in (None, "list"):
        return _list(working_dir)
    if action == "search":
        print("集市适配器检索（本地已发现）：")
        return _list(working_dir)
    if action == "store":
        print("打开适配器集市: %s" % MARKET_URL)
        try:
            webbrowser.open(MARKET_URL)
        except Exception:
            pass
        return 0
    if action == "install":
        return _enable(args.id, working_dir, True)
    if action == "uninstall":
        return _enable(args.id, working_dir, False)
    print("未知 adapter 子命令: %s" % action)
    return 2


def _list(working_dir):
    adapters = discover_adapters(working_dir)
    if not adapters:
        print("未找到任何适配器（core_plugins/ 下无 main.py）")
        return 0
    rows = []
    for pid, path, meta, kind in adapters:
        enabled = plugin_enabled_state(working_dir, pid, kind)
        rows.append([
            pid,
            meta.get("version", "-"),
            "on" if enabled else "off",
            meta.get("desc", "") or "",
        ])
    print("%s adapter list —— 共 %d 个" % (PROG, len(rows)))
    print(render_table(rows, ["ID", "VER", "STATE", "DESC"]))
    return 0


def _enable(adapter_id, working_dir, enabled):
    adapters = {pid: path for pid, path, meta, kind in discover_adapters(working_dir)}
    if adapter_id not in adapters:
        print("错误：未找到适配器: %s（先用 `%s adapter list` 查看）" % (adapter_id, PROG))
        return 1
    ok, detail = set_core_plugin_enabled(
        os.path.join(working_dir, "core_plugins.yaml"), adapter_id, enabled)
    if not ok:
        print("失败：%s" % detail)
        return 1
    print("适配器 %s: %s" % (adapter_id, "启用" if enabled else "禁用"))
    print("  %s" % detail)
    return 0
