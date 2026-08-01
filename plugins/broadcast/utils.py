"""
utils.py

适配目标框架：
  - 同步实现（time.sleep 替代 asyncio.sleep）
  - ctx.api() 替代 aiocqhttp CQHttp
  - ctx 替代 AiocqhttpMessageEvent 和 astrbot.core.logger
  - 事件回复消息格式：onebot 消息段列表
"""

import time


def parse_scope_name(
    scope_name: str = "",
    *,
    strict: bool = False,
    default_is_group: bool = True,
) -> bool | None:
    scope = scope_name.strip()
    scope_lower = scope.lower()

    if scope in ("好友", "私聊") or scope_lower in ("friend", "f"):
        return False
    if scope in ("群聊", "群") or scope_lower in ("group", "g"):
        return True
    if strict:
        return None
    return default_is_group


def parse_scope_and_index(
    arg1: str = "",
    arg2: str = "",
) -> tuple[bool, int | None, str | None]:
    first = arg1.strip()
    second = arg2.strip()

    if first.isdigit() and not second:
        index = int(first)
        if index <= 0:
            return True, None, "序号必须大于 0"
        return True, index, None

    if not first:
        return True, None, None

    is_group = parse_scope_name(first, strict=True)
    if is_group is None:
        return True, None, "参数错误，格式：开启广播 [群聊|私聊] [序号]"

    if not second:
        return is_group, None, None

    if not second.isdigit():
        return is_group, None, "序号必须是正整数"

    index = int(second)
    if index <= 0:
        return is_group, None, "序号必须大于 0"
    return is_group, index, None


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


def get_friend_by_index(ctx, index: int | None) -> tuple[str | None, str | None]:
    """根据序号获取好友信息"""
    try:
        api = ctx.api()
        friends = api.get_friend_list()
        friends.sort(key=lambda x: x["user_id"])

        if index:
            friend = friends[index - 1]
        else:
            uid = ctx.user_id
            friend = next(
                (f for f in friends if str(f["user_id"]) == str(uid)),
                None,
            )
            if not friend:
                return str(uid), str(uid)

        name = friend.get("remark") or friend.get("nickname") or str(friend["user_id"])
        return str(friend["user_id"]), name
    except Exception as e:
        ctx.logger.error(f"获取好友信息失败: {e}")
        return None, None


def get_ids(api, is_group: bool) -> list[str]:
    """获取所有目标 ID 列表"""
    if is_group:
        groups = api.get_group_list()
        return [str(g["group_id"]) for g in groups]
    else:
        friends = api.get_friend_list()
        return [str(f["user_id"]) for f in friends]


def broadcast(ctx, api, is_group: bool, message_id: str | int, ids: list[str], delay: float = 0.5, cancel_check=None):
    """
    向指定 ID 列表广播消息（同步版本）

    适配目标框架：
      - time.sleep 替代 asyncio.sleep
      - api.forward_* 替代 client.forward_*
      - cancel_check: 可选回调函数，返回 True 时取消广播（替代 asyncio.CancelledError）
    """
    success_ids = []
    try:
        for tid in ids:
            if cancel_check and cancel_check():
                ctx.logger.info("广播任务被取消")
                return success_ids

            time.sleep(delay)
            try:
                if is_group:
                    api.forward_group_single_msg(
                        group_id=int(tid),
                        message_id=message_id,
                    )
                    success_ids.append(tid)
                else:
                    api.forward_friend_single_msg(
                        user_id=int(tid),
                        message_id=message_id,
                    )
                    success_ids.append(tid)
            except Exception as e:
                ctx.logger.warning(f"{tid} 广播失败: {e}")
        return success_ids
    except Exception as e:
        ctx.logger.info("广播任务被取消")
        return success_ids