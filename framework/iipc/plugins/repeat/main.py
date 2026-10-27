# -*- coding: utf-8 -*-
"""示例插件：复读姬（演示 IIPC 事件订阅 on("message")）。

收到任意非命令文本消息，原样复读回去——验证「事件总线 → Python 插件 → 批量动作回传」。
与 echo 插件区分：echo 走命令路由（/echo 命令），本插件走事件订阅（on message），
展示 IIPC 两种处理面的差异。

注意：为演示清晰，仅复读不以 / 开头的消息，避免与命令互相干扰。
"""
from sdk import PluginContext

__plugin_meta__ = {
    "name": "复读姬",
    "version": "0.1.0",
    "author": "zc",
    "desc": "演示 on(message) 事件订阅：收到消息原样复读",
}


def on_error(event, error):
    print("[repeat] on_error: %s" % error)


def register(ctx: PluginContext):
    ctx.logger.info("复读姬插件已加载 (plugin_id=%s)", ctx.plugin_id)

    @ctx.on("message")
    def repeat(event):
        text = event.message
        # 跳过命令消息（以 / 开头），避免与 echo 等命令插件互相干扰
        if not text or text.startswith("/"):
            return
        ctx.send_msg(
            message=text,
            user_id=event.user_id,
            group_id=event.group_id,
        )
