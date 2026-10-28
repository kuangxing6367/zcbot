# -*- coding: utf-8 -*-
"""
内置帮助菜单插件（默认开启）

教程《快速开始》《对接 IM 平台》都让用户「对机器人发 /help（或 /帮助、/菜单）
看全部命令」——但框架此前没有任何 IM 插件响应 /help，按文档操作永远收不到回复
（与 echo 缺失是同一类设计缺陷）。本插件补齐这一缺口，默认开启、覆盖全部接入端。

实现：直接从 commands 表读取当前所有已注册命令，按插件分组回显，使「查看命令」
开箱即用；新增/卸载插件后列表自动更新。
"""
__plugin_meta__ = {
    "name": "help",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "内置帮助菜单：/help 列出全部已注册命令与说明（覆盖所有接入端）",
    "priority": 1,
    "official": True,
    "process": "core",
}

ctx = None


def register(plugin_ctx):
    global ctx
    ctx = plugin_ctx
    ctx.command(
        "/help",
        handle_help,
        priority=1,
        alias=["/帮助", "/菜单"],
        description="查看全部已注册命令与说明",
    )
    ctx.log("help 帮助菜单已就绪（/help 列出全部命令；对所有接入端生效）")


def handle_help(event, match):
    rows = ctx.db_query(
        "SELECT plugin_name, pattern, description, priority FROM commands "
        "WHERE pattern IS NOT NULL AND pattern != '' "
        "ORDER BY plugin_name, priority"
    )
    if not rows:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="当前没有任何已注册命令。",
        )
        return

    groups = {}
    for r in rows:
        pn = r.get("plugin_name") or "未知插件"
        groups.setdefault(pn, []).append(r)

    lines = ["\U0001F4D6 命令清单（按插件分组）：", ""]
    for pn in sorted(groups):
        lines.append(f"【{pn}】")
        for r in groups[pn]:
            pat = (r.get("pattern") or "").strip()
            desc = (r.get("description") or "").strip()
            if desc:
                lines.append(f"  {pat} — {desc}")
            else:
                lines.append(f"  {pat}")
        lines.append("")
    lines.append("发 /echo 你好 可自测链路；Web 管理后台有更完整的命令管理器。")

    ctx.send_msg(
        user_id=event.user_id, group_id=event.group_id,
        message="\n".join(lines),
    )
