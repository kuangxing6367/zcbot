# -*- coding: utf-8 -*-
"""示例插件：群管小工具（演示 IIPC 命令 + 通用 API 通道 api.call）。

命令：
  /who     查询当前机器人登录信息（get_login_info）
  /members 列出当前群成员数（get_member_list）
  /ban 号  禁言某人 600s（set_group_ban）
  /kick 号 踢出某人（set_group_kick）

展示 IIPC 的「命令路由 + 通用 API 通道」两个处理面，验证 Python 侧能经内核
调用任意 OneBot action 并把结果回传。
"""
from sdk import PluginContext

__plugin_meta__ = {
    "name": "群管小工具",
    "version": "0.1.0",
    "author": "zc",
    "desc": "演示命令 + 通用 API 通道：查询登录信息 / 群成员 / 禁言 / 踢人",
}


def on_error(event, error):
    print("[group_admin] on_error: %s" % error)


def register(ctx: PluginContext):
    ctx.logger.info("群管小工具插件已加载 (plugin_id=%s)", ctx.plugin_id)

    @ctx.command("/who")
    def who(event, match):
        info = ctx.get_login_info()
        ctx.send_msg(message="机器人信息: %s" % info,
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/members")
    def members(event, match):
        if not event.group_id:
            ctx.send_msg(message="请在群里使用本命令",
                         user_id=event.user_id, group_id=event.group_id)
            return
        try:
            lst = ctx.get_member_list(group_id=event.group_id)
            n = len(lst) if isinstance(lst, list) else 0
            ctx.send_msg(message="本群成员数: %d" % n,
                         user_id=event.user_id, group_id=event.group_id)
        except Exception as e:
            ctx.send_msg(message="查询失败: %s" % e,
                         user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/ban")
    def ban(event, match):
        target = (match.group(1) or "").strip()
        if not target.isdigit() or not event.group_id:
            ctx.send_msg(message="用法：/ban 对方QQ（需在群里）",
                         user_id=event.user_id, group_id=event.group_id)
            return
        try:
            ctx.ban(group_id=event.group_id, user_id=int(target), duration=600)
            ctx.send_msg(message="已禁言 %s 600 秒" % target,
                         user_id=event.user_id, group_id=event.group_id)
        except Exception as e:
            ctx.send_msg(message="禁言失败: %s" % e,
                         user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/kick")
    def kick(event, match):
        target = (match.group(1) or "").strip()
        if not target.isdigit() or not event.group_id:
            ctx.send_msg(message="用法：/kick 对方QQ（需在群里）",
                         user_id=event.user_id, group_id=event.group_id)
            return
        try:
            ctx.kick(group_id=event.group_id, user_id=int(target))
            ctx.send_msg(message="已踢出 %s" % target,
                         user_id=event.user_id, group_id=event.group_id)
        except Exception as e:
            ctx.send_msg(message="踢出失败: %s" % e,
                         user_id=event.user_id, group_id=event.group_id)
