"""
Echo 插件 - 原样返回用户文本消息
Echo 插件 - 原样返回用户文本消息
"""
__plugin_meta__ = {
    "name": "Echo",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "原样返回用户文本消息，无参数时返回 PONG",
    "priority": 50,
}


def register(ctx):
    """插件注册入口"""
    ctx.command("/echo", handle_echo, priority=50, description="原样返回输入文本，无参数返回 PONG")


def handle_echo(event, match):
    """原样返回用户文字消息

    event.message 包含完整指令文本（如 "/echo hello world"）。
    match.group(1) 为命令后的参数文本；为空时返回 PONG。
    """
    text = ""
    if match:
        text = match.group(1).strip()
    # 兼容：若 match 未携带参数，则直接解析 event.message
    if not text:
        msg = event.message or ""
        if msg.startswith("/echo"):
            text = msg[len("/echo"):].strip()

    reply = text if text else "PONG"

    # 群聊回群、私聊回私
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=reply,
    )
