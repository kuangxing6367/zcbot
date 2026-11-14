"""``zb plugin`` —— 管理插件（list / search / store / install / uninstall）。"""
import logging
import os
import webbrowser

from ..utils import (
    PROG,
    discover_plugins,
    enable_user_plugin,
    plugin_enabled_state,
    render_table,
    set_core_plugin_enabled,
)

logger = logging.getLogger('zcbot')

MARKET_URL = "https://github.com/kuangxing6367/zcbot"


def do_plugin(args, working_dir):
    action = args.action
    if action in (None, "list"):
        return _list(working_dir)
    if action == "search":
        print("集市插件检索（本地已发现）：")
        return _list(working_dir)
    if action == "store":
        print("打开插件集市: %s" % MARKET_URL)
        try:
            webbrowser.open(MARKET_URL)
        except Exception as _exc:
            logger.debug('[do_plugin] 捕获异常后按降级路径继续: %s', _exc)
        return 0
    if action == "install":
        return _enable(args.id, working_dir, True)
    if action == "uninstall":
        return _enable(args.id, working_dir, False)
    print("未知 plugin 子命令: %s" % action)
    return 2


def _list(working_dir):
    plugins = discover_plugins(working_dir)
    if not plugins:
        print("未找到任何插件（已检查内核目录 %s 与当前目录下的 plugins/ 、core_plugins/）"
              % _plugin_roots_desc())
        return 0
    rows = []
    for pid, path, meta, kind in plugins:
        enabled = plugin_enabled_state(working_dir, pid, kind)
        rows.append([
            pid,
            meta.get("version", "-"),
            "on" if enabled else "off",
            kind,
            meta.get("desc", "") or "",
        ])
    print("%s plugin list —— 共 %d 个" % (PROG, len(rows)))
    print(render_table(rows, ["ID", "VER", "STATE", "KIND", "DESC"]))
    return 0


def _plugin_roots_desc():
    """把实际扫描到的插件根目录列出来，别再让用户猜"为什么列表是空的"。"""
    try:
        from ..utils import _kernel_plugin_dirs
        user_root, core_root = _kernel_plugin_dirs()
        parts = [p for p in (user_root, core_root) if p]
        return " 、".join(parts) if parts else "（未解析到）"
    except Exception as _exc:
        logger.debug('[_plugin_roots_desc] 捕获异常后按降级路径继续: %s', _exc)
        return "（未解析到）"


def _enable(plugin_id, working_dir, enabled):
    plugins = {pid: (path, kind) for pid, path, meta, kind in discover_plugins(working_dir)}
    disabled_path = os.path.join(working_dir, "plugins_disabled", plugin_id)
    if plugin_id not in plugins and not os.path.isdir(disabled_path):
        print("错误：未找到插件: %s（先用 `%s plugin list` 查看）" % (plugin_id, PROG))
        return 1
    if plugin_id in plugins:
        _path, kind = plugins[plugin_id]
    else:
        kind = "user"  # 位于 plugins_disabled/ 的禁用插件
    if kind == "core":
        _data_yaml = os.path.join(working_dir, "data", "core_plugins.yaml")
        _yaml = _data_yaml if os.path.isfile(_data_yaml) \
            else os.path.join(working_dir, "core_plugins.yaml")
        ok, detail = set_core_plugin_enabled(_yaml, plugin_id, enabled)
    else:
        ok, detail = enable_user_plugin(working_dir, plugin_id, enabled)
    if not ok:
        print("失败：%s" % detail)
        return 1
    print("插件 %s: %s" % (plugin_id, "启用" if enabled else "禁用"))
    print("  %s" % detail)
    return 0
