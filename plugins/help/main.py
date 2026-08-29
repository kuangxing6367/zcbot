"""
帮助系统插件 - 查询所有已注册命令并生成图片帮助菜单
帮助系统插件

原插件依赖框架的命令注册系统获取命令，
新框架中命令存储在 MySQL 的 commands 表，plugins 表存储插件元数据。

本插件使用 Pillow 渲染图片帮助菜单（纯 Python，无 numpy 依赖），
图片保存到临时文件，通过 CQ 码 [CQ:image,file=file:///路径] 发送，
发送完成后立即删除临时文件。

命令：
  /help  /帮助  /菜单   生成图片帮助菜单发送

配置项（_conf_schema.json）：
  show_builtin_cmds  布尔  显示内置命令，默认 true
  show_all_cmds      布尔  显示所有命令（包括管理员命令），默认 false
  custom_cmds        列表  自定义的额外命令
  plugin_display_names 列表  插件显示名称映射
  plugin_blacklist   列表  插件黑名单（不显示帮助的插件）
  title_help         字符串 帮助菜单标题，默认 "ZGRIC 命令帮助"
  title_desc         字符串 帮助菜单简介，默认 "可用插件及指令列表"
  logo_enable        布尔  启用帮助菜单 logo，默认 true
"""
import os
import re
import tempfile
from collections import OrderedDict

__plugin_meta__ = {
    "name": "帮助系统",
    "version": "1.0.0",
    "author": "tinker",
    "desc": "查询所有已注册命令，生成图片帮助菜单",
    "priority": 100,
}


def register(ctx):
    """插件注册入口"""
    ctx.command(
        "/help",
        handle_help,
        priority=100,
        alias=["/帮助", "/菜单"],
        description="显示所有可用命令列表（图片形式，按插件分组）",
    )


def _query_commands(ctx):
    """
    查询 commands + plugins 表，返回原始行列表。

    - 只显示 is_active=1 的命令
    - 只显示 plugins.is_active=1 的插件
    - 按插件 priority 升序、命令 priority 升序排列
    """
    sql = (
        "SELECT c.plugin_name, c.pattern, c.alias, c.description, "
        "c.require_level, "
        "c.priority AS cmd_priority, p.version, p.priority AS plg_priority, "
        "p.description AS plugin_desc "
        "FROM commands c "
        "LEFT JOIN plugins p ON c.plugin_name = p.plugin_name "
        "WHERE c.is_active = 1 "
        "  AND (p.is_active = 1 OR p.is_active IS NULL) "
        "ORDER BY p.priority ASC, c.plugin_name ASC, c.priority ASC, c.created_at ASC"
    )
    try:
        return ctx.db_query(sql)
    except Exception as e:
        ctx.log(f"查询命令列表失败: {e}", level="error")
        return []


def _get_display_name_map(ctx):
    """解析配置中的插件显示名称映射，返回 {plugin_name: display_name} 字典"""
    mapping = {}
    raw_list = ctx.get_config("plugin_display_names", []) or []
    for item in raw_list:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item or ":" not in item:
            continue
        parts = item.split(":", 1)
        pname = parts[0].strip()
        dname = parts[1].strip()
        if pname and dname:
            mapping[pname] = dname
    return mapping


def _prettify_command(pattern):
    r"""将正则 pattern 美化为友好的命令名显示。

    例如: ^/一言(?:\s+(.+))?\s*$ → /一言
          ^/今日早报\s*$ → /今日早报
    """
    cmd = pattern
    cmd = re.sub(r'^\^', '', cmd)
    cmd = re.sub(r'\$$', '', cmd)
    cmd = re.sub(r'\([^)]*\)[\?\*\+]?', '', cmd)
    cmd = re.sub(r'\\[sS]\*|\\[sS]\+', '', cmd)
    cmd = re.sub(r'\.\*?\??', '', cmd)
    cmd = cmd.replace('?', '')
    cmd = cmd.strip()
    return cmd if cmd else pattern


def _build_plugin_commands(ctx, rows):
    """
    将数据库查询结果转换为 draw.py 期望的格式：
        {plugin_display_name: [{"command": str, "desc": str}, ...]}

    - 跳过 help 插件自身
    - 支持 show_builtin_cmds 过滤内置命令
    - 支持 show_all_cmds 过滤管理员命令（require_level=admin/super）
    - 支持 plugin_blacklist 过滤黑名单插件
    - 支持 plugin_display_names 映射插件显示名称
    - 命令 pattern 确保以 / 开头
    """
    self_plugin = getattr(ctx, "_plugin_name", "help")

    # 读取配置
    show_all = bool(ctx.get_config("show_all_cmds", False))
    show_builtin = bool(ctx.get_config("show_builtin_cmds", True))
    blacklist = ctx.get_config("plugin_blacklist", []) or []
    display_name_map = _get_display_name_map(ctx)

    plugin_commands = OrderedDict()
    for r in rows:
        plugin_name = r.get("plugin_name") or "未知插件"

        # 跳过 help 插件自身
        if plugin_name == self_plugin:
            continue

        # show_builtin_cmds: 过滤内置命令插件
        if not show_builtin and plugin_name == "builtin_commands":
            continue

        # plugin_blacklist: 过滤黑名单插件
        if plugin_name in blacklist:
            continue

        # show_all_cmds: 过滤管理员命令
        if not show_all:
            require_level = (r.get("require_level") or "").strip()
            if require_level in ("admin", "super"):
                continue

        # 确定显示名称: 优先使用配置映射，再回退到 plugin_desc，最后回退到 plugin_name
        display_name = (
            display_name_map.get(plugin_name)
            or (r.get("plugin_desc") or "").strip()
            or plugin_name
        )

        pattern = (r.get("pattern") or "").strip()
        if not pattern:
            continue
        pattern = _prettify_command(pattern)
        # 确保命令以 / 开头
        if not pattern.startswith("/"):
            pattern = "/" + pattern

        desc = (r.get("description") or "").strip()

        if display_name not in plugin_commands:
            plugin_commands[display_name] = []
        plugin_commands[display_name].append(
            {"command": pattern, "desc": desc}
        )

    return plugin_commands


def _build_fallback_text(ctx, rows):
    """
    图片生成失败时的纯文本回退。
    保留原有文本帮助列表逻辑。
    """
    self_plugin = getattr(ctx, "_plugin_name", "help")

    # 读取配置
    show_all = bool(ctx.get_config("show_all_cmds", False))
    show_builtin = bool(ctx.get_config("show_builtin_cmds", True))
    blacklist = ctx.get_config("plugin_blacklist", []) or []

    grouped = OrderedDict()
    for r in rows:
        plg = r.get("plugin_name") or "未知插件"

        # 跳过 help 插件自身
        if plg == self_plugin:
            continue

        # show_builtin_cmds: 过滤内置命令插件
        if not show_builtin and plg == "builtin_commands":
            continue

        # plugin_blacklist: 过滤黑名单插件
        if plg in blacklist:
            continue

        # show_all_cmds: 过滤管理员命令
        if not show_all:
            require_level = (r.get("require_level") or "").strip()
            if require_level in ("admin", "super"):
                continue

        if plg not in grouped:
            grouped[plg] = {
                "version": (r.get("version") or "").strip(),
                "commands": [],
            }
        grouped[plg]["commands"].append(r)

    if not grouped:
        return None

    lines = ["\U0001F4CB 可用命令列表", "━" * 15]

    for plg, info in grouped.items():
        version = info["version"] or "?"
        lines.append(f"【{plg} v{version}】")
        for cmd in info["commands"]:
            pattern = (cmd.get("pattern") or "").strip()
            desc = (cmd.get("description") or "").strip()
            if not pattern:
                continue
            pattern = _prettify_command(pattern)
            display_cmd = pattern if pattern.startswith("/") else f"/{pattern}"
            if desc:
                lines.append(f"  {display_cmd} - {desc}")
            else:
                lines.append(f"  {display_cmd}")

    lines.append("━" * 15)
    lines.append("发送 /help 查看此列表")

    return "\n".join(lines)


def _send_image(event, image_bytes, ctx):
    """
    保存图片到临时文件，发送 CQ 码，然后立即删除临时文件。

    内存管理：
      - 使用 tempfile.NamedTemporaryFile 创建临时文件
      - 发送完成后在 finally 块中 os.unlink() 删除
      - 删除失败不抛出异常
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_bytes)
        tmp_path = f.name

    try:
        path_str = tmp_path.replace("\\", "/")
        cq = f"[CQ:image,file=file:///{path_str}]"
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=cq,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def handle_help(event, match):
    """/help - 生成图片帮助菜单并发送，失败时回退到纯文本"""
    # 读取配置
    show_all = bool(ctx.get_config("show_all_cmds", False))
    title_help = ctx.get_config("title_help", "ZGRIC 命令帮助")
    title_desc = ctx.get_config("title_desc", "可用插件及指令列表")
    logo_enable = bool(ctx.get_config("logo_enable", True))

    # 查询数据库
    rows = _query_commands(ctx)
    if not rows:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="当前没有可用的命令。",
        )
        return

    # 转换为 draw.py 期望的数据格式
    plugin_commands = _build_plugin_commands(ctx, rows)

    if not plugin_commands:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="当前没有可用的命令。",
        )
        return

    # 读取额外配置
    plugin_blacklist = ctx.get_config("plugin_blacklist", []) or []
    custom_cmds = ctx.get_config("custom_cmds", []) or []
    font_sizes = ctx.get_config("font_sizes", {}) or {}

    # 构造 drawer 配置 dict
    drawer_config = {
        "title_help": title_help,
        "title_desc": title_desc,
        "logo_enable": logo_enable,
        "show_all_cmds": show_all,
        "plugin_blacklist": plugin_blacklist,
        "custom_cmds": custom_cmds,
        "font_sizes": font_sizes,
        "plugin_display_name": "ZGRIC",
        "plugin_version": __plugin_meta__["version"],
    }

    # 尝试生成图片
    image_bytes = None
    try:
        # 框架用 importlib.util 加载插件，相对导入不可用
        # 将插件目录加入 sys.path 后用绝对导入
        import sys as _sys
        import os as _os
        _plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _plugin_dir not in _sys.path:
            _sys.path.insert(0, _plugin_dir)
        from draw import ZcbotHelpDrawer

        drawer = ZcbotHelpDrawer(drawer_config)
        image_bytes = drawer.draw_help_image(plugin_commands)
        # 释放 drawer（含 resized_logo 等资源）
        del drawer
    except Exception as e:
        ctx.log(f"生成帮助图片失败，回退到纯文本: {e}", level="error")
        image_bytes = None

    # 发送图片或回退文本
    if image_bytes:
        # 优先走 image_renderer 官方发送接口 _send_image(ctx, event, img_or_bytes)
        # （官方接口内部自带异常兜底与错误提示，不抛异常），不可用时回退本地发送
        sent = False
        try:
            import sys as _sys2
            _img_mod = _sys2.modules.get("plugin_image_renderer")
            if _img_mod is not None and hasattr(_img_mod, "_send_image"):
                _img_mod._send_image(ctx, event, image_bytes)
                sent = True
        except Exception as _e:
            ctx.log(f"image_renderer 统一发送失败，回退本地发送: {_e}", level="warning")
        if not sent:
            _send_image(event, image_bytes, ctx)
        # 释放图片字节缓冲
        del image_bytes
    else:
        help_text = _build_fallback_text(ctx, rows)
        if not help_text:
            help_text = "当前没有可用的命令。"
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=help_text,
        )
