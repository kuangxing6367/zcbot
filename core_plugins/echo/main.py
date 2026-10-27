# -*- coding: utf-8 -*-
"""
回声 / 连通性自检插件（内置，默认开启）

这是框架官方的「连通性自检」工具：任意接入端（OneBot / QQ 官方 / Telegram /
Discord / WS 总线 / HTTP 注入……）接进来后，对机器人发 ``/echo 你好``，框架会
原样回 ``你好`` —— 以此证明「消息进得来 → 命令路由得通 → 回复发得出」整条链路 OK。

为什么必须有它（此前是设计缺陷）：
    教程《快速开始》《对接 IM 平台》都把「发 /echo 你好 → 收到 你好」作为接入后的
    标准验证步骤，但框架从未提供该命令——按文档操作永远收不到回复，验证形同虚设。
    本插件补齐这一缺口，且默认开启，使教程验证开箱即用。

设计要点：
    - 不声明 __plugin_meta__['adapters']，不受多平台接入的适配器绑定限制，
      对所有接入端生效（自检本就该覆盖全部适配器）。
    - 仅提供无副作用的回显命令，不干扰任何业务插件。
    - handler 直接用模块级 ctx（由加载器注入，与 image_renderer 等核心插件一致）。
"""
__plugin_meta__ = {
    "name": "echo",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "连通性自检：/echo 原样回显，验证任意接入端链路通断",
    "priority": 1,
    "official": True,
    "process": "core",
}


def register(ctx):
    ctx.command(
        "/echo",
        handle_echo,
        priority=1,
        description="连通性自检：/echo 文本 → 原样回显「文本」",
    )
    ctx.command(
        "/ping",
        handle_ping,
        priority=1,
        description="健康检查：回复 pong",
    )
    ctx.log("echo 自检插件已就绪（/echo 你好 验证链路；对所有接入端生效）")


def handle_echo(event, match):
    """/echo 你好 → 回「你好」"""
    text = (match.group(1) or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="用法：/echo 要回显的内容",
        )
        return
    ctx.send_msg(
        user_id=event.user_id, group_id=event.group_id,
        message=text,
    )


def handle_ping(event, match):
    ctx.send_msg(
        user_id=event.user_id, group_id=event.group_id,
        message="pong",
    )
