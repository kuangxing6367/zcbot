"""
main.py - 广播助手插件入口

适配目标框架：
  - register(ctx) 函数入口
  - ctx.command(pattern, handler, alias, description) 注册命令
  - ctx.api().send_msg() / get_group_list() / get_friend_list() / forward_* 发送消息
  - ctx.user_id, ctx.group_id, ctx.message_str
  - ctx.get_config(key, default), ctx.set_config(key, value)
  - ctx.logger 记录日志
  - ctx.event 事件对象
  - 异步操作转同步（requests 替代 httpx/aiohttp，time.sleep 替代 asyncio.sleep）
"""

import threading
import time

from plugins.broadcast.config import PluginConfig
from plugins.broadcast.utils import (
    broadcast,
    get_friend_by_index,
    get_group_by_index,
    get_ids,
    get_reply_id,
    parse_scope_and_index,
    parse_scope_name,
)

# ========================
# 全局广播线程状态
# ========================
_broadcast_thread = None
_broadcast_cancel = False


def register(ctx):
    """插件入口函数"""
    cfg = PluginConfig(ctx)
    ctx.logger.info("广播助手插件已加载")

    # ============================================================
    # 开启广播
    # ============================================================
    def _enable_broadcast(event, arg1="", arg2=""):
        """开启广播 <留空|群聊|私聊> <序号>"""
        nonlocal cfg

        is_group, index, err = parse_scope_and_index(arg1, arg2)
        if err:
            return err

        if is_group:
            target_id, name = get_group_by_index(ctx, index)
        else:
            target_id, name = get_friend_by_index(ctx, index)
        if not target_id:
            return

        cfg.enable_target(target_id, is_group=is_group)
        scope_name = "群聊" if is_group else "私聊"
        return f"【{name}】已开启{scope_name}广播"

    ctx.command(
        "开启广播",
        _enable_broadcast,
        description="开启广播 <留空|群聊|私聊> <序号>",
    )

    # ============================================================
    # 关闭广播
    # ============================================================
    def _disable_broadcast(event, arg1="", arg2=""):
        """关闭广播 <留空|群聊|私聊> <序号>"""
        nonlocal cfg

        is_group, index, err = parse_scope_and_index(arg1, arg2)
        scope_text = "群聊" if is_group else "好友"

        if err:
            return err

        if is_group:
            target_id, name = get_group_by_index(ctx, index)
        else:
            target_id, name = get_friend_by_index(ctx, index)
        if not target_id:
            return

        cfg.disable_target(target_id, is_group=is_group)
        return f"已关闭【{name}】的{scope_text}广播"

    ctx.command(
        "关闭广播",
        _disable_broadcast,
        description="关闭广播 <留空|群聊|私聊> <序号>",
    )

    # ============================================================
    # 广播列表
    # ============================================================
    def _broadcast_list(event, scope_name=""):
        """广播列表 <留空|群聊|私聊>"""
        nonlocal cfg

        is_group = bool(parse_scope_name(scope_name))
        scope_text = "群聊" if is_group else "好友"

        api = ctx.api()

        enabled = []
        disabled = []

        if is_group:
            groups = api.get_group_list()
            groups.sort(key=lambda x: x["group_id"])
            for idx, g in enumerate(groups, 1):
                target_id = str(g["group_id"])
                info = f"{idx}. {g['group_name']} ({target_id})"
                if cfg.is_disabled(target_id, is_group=True):
                    disabled.append(info)
                else:
                    enabled.append(info)
        else:
            friends = api.get_friend_list()
            friends.sort(key=lambda x: x["user_id"])
            for idx, f in enumerate(friends, 1):
                target_id = str(f["user_id"])
                name = f.get("remark") or f.get("nickname") or target_id
                info = f"{idx}. {name} ({target_id})"
                if cfg.is_disabled(target_id, is_group=False):
                    disabled.append(info)
                else:
                    enabled.append(info)

        msg = f"【{scope_text}开启广播】\n" + "\n".join(enabled)
        if len(disabled) > 0:
            msg += f"\n\n【{scope_text}关闭广播】\n" + "\n".join(disabled)

        return msg

    ctx.command(
        "广播列表",
        _broadcast_list,
        description="广播列表 <留空|群聊|私聊>",
    )

    # ============================================================
    # 广播
    # ============================================================
    def _cmd_broadcast(event, scope_name=""):
        """(引用消息)广播 <群聊|私聊|全部>"""
        nonlocal cfg
        global _broadcast_thread, _broadcast_cancel

        reply_id = get_reply_id(event)
        if not reply_id:
            return "需要引用要广播的消息"

        if _broadcast_thread and _broadcast_thread.is_alive():
            return "已有广播正在进行中"

        is_group = bool(parse_scope_name(scope_name))
        scope_text = "群聊" if is_group else "好友"

        api = ctx.api()
        ids = get_ids(api, is_group=is_group)

        if cfg.skip_source:
            source_id = str(ctx.group_id if is_group else ctx.user_id)
            if source_id in ids:
                ids.remove(source_id)

        filter_ids = cfg.filter_broadcastable(ids, is_group=is_group)

        _broadcast_cancel = False

        # 在后台线程中执行广播
        def _run_broadcast():
            global _broadcast_thread, _broadcast_cancel

            try:
                success_ids = broadcast(
                    ctx=ctx,
                    api=api,
                    is_group=is_group,
                    message_id=reply_id,
                    ids=filter_ids,
                    delay=cfg.get_broadcast_delay(),
                    cancel_check=lambda: _broadcast_cancel,
                )
            finally:
                _broadcast_thread = None

            # 广播完成后通知结果
            try:
                ctx.api().send_msg(
                    group_id=ctx.group_id,
                    message=f"已向{len(success_ids)}个{scope_text}广播此消息",
                )
            except Exception as e:
                ctx.logger.error(f"发送广播结果通知失败: {e}")

        _broadcast_thread = threading.Thread(target=_run_broadcast, daemon=True)
        _broadcast_thread.start()

        return f"正在向{len(filter_ids)}个{scope_text}广播此消息..."

    ctx.command(
        "广播",
        _cmd_broadcast,
        description="(引用消息)广播 <群聊|私聊|全部>",
    )

    # ============================================================
    # 取消广播
    # ============================================================
    def _cancel_broadcast(event):
        """取消当前正在进行的广播任务"""
        global _broadcast_thread, _broadcast_cancel

        if not _broadcast_thread or not _broadcast_thread.is_alive():
            return "当前没有进行中的广播"

        _broadcast_cancel = True
        return "已请求取消广播"

    ctx.command(
        "取消广播",
        _cancel_broadcast,
        description="取消当前正在进行的广播任务",
    )