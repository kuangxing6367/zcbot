"""
core/utils.py

适配目标框架：
  - 使用 ctx 代替 AiocqhttpMessageEvent 和 astrbot.core.logger
  - 事件回复消息格式：onebot 消息段列表
"""


def get_reply_id(event) -> str | int | None:
    """获取被引用消息的 id"""
    try:
        message = event.get("message", [])
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                return seg.get("data", {}).get("id")
    except Exception:
        pass
    return None


def get_group_by_index(ctx, index: int | None) -> tuple[str | None, str | None]:
    """根据序号获取群聊信息"""
    try:
        api = ctx.api()
        groups = api.get_group_list()
        groups.sort(key=lambda x: x["group_id"])

        if index:
            group = groups[index - 1]
        else:
            gid = ctx.group_id
            group = next(g for g in groups if str(g["group_id"]) == str(gid))

        return str(group["group_id"]), group["group_name"]
    except Exception as e:
        ctx.logger.error(f"获取群信息失败: {e}")
        return None, None