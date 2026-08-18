"""
多轮会话等待插件 (session_waiter)
=================================
为插件提供「等待用户下一条消息」的能力（多轮对话 / 问卷 / 向导式交互）。

用法（其他插件）：
    import sys
    sw = sys.modules.get("plugin_session_waiter")
    if sw is None:
        return  # session_waiter 未加载（它 priority=1 最先加载，正常必在）

    # async handler 中：
    reply = await sw.wait_for_user(ctx, session_id=sw.make_session_id(event), timeout=60)
    if reply is None:
        # 超时
        ...

    # 旧式同步 handler 中：
    reply = sw.wait_for_user_sync(ctx, session_id=sw.make_session_id(event), timeout=60)

实现原理：
- 注册 on_raw_message（priority=1，最先执行），每条消息先查会话表
- 命中 → resolve 对应 Future 并把消息返回给等待方，随后返回 True 接管
  （该消息不再走命令匹配/关键词回复，防止被其他插件误处理）
- 超时自动清理会话；会话表有上限，防内存增长

限制说明：
- 会话键默认按「发送者 + 群」区分，同一用户同一群的等待互不干扰
- 等待期间用户发命令（如 /help）也会被会话接管（返回原始消息，由调用方判断）
"""
import asyncio
import threading
import time

__plugin_meta__ = {
    "name": "会话等待器",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "多轮会话基础设施：插件可等待用户下一条消息（wait_for_user）",
    "priority": 1,
}

_DEFAULT_TIMEOUT = 60
_MAX_SESSIONS = 5000

# session_id -> {"future": asyncio.Future, "expires": float, "handler": callable|None}
_SESSIONS: dict = {}
_LOCK = threading.Lock()


def make_session_id(event) -> str:
    """从事件对象构造会话键（user_id:group_id），供 wait_for_user 使用"""
    gid = getattr(event, "group_id", None) or 0
    uid = getattr(event, "user_id", 0)
    return f"{uid}:{gid}"


def _session_key(raw: dict) -> str:
    """从原始消息事件构造会话键"""
    gid = raw.get("group_id") or 0
    uid = raw.get("user_id") or 0
    return f"{uid}:{gid}"


def register(ctx):
    ctx.on_raw_message(_on_raw_message)
    ctx.task("*/5 * * * *", _cleanup_expired, description="清理过期会话等待")
    ctx.log("[session_waiter] 会话等待器已就绪（priority=1，先于其他插件处理消息）")


def _cleanup_expired_locked():
    """清理过期会话（调用前需持有锁）"""
    now = time.time()
    for sid in [s for s, w in _SESSIONS.items() if w["expires"] < now]:
        w = _SESSIONS.pop(sid, None)
        if w and not w["future"].done():
            w["future"].set_result(None)  # 超时：返回 None


def _cleanup_expired():
    """定时任务：清理过期会话（防内存增长）"""
    try:
        with _LOCK:
            _cleanup_expired_locked()
    except Exception as e:
        ctx.log(f"[session_waiter] 清理过期会话失败: {e}", level="error")


async def _on_raw_message(raw: dict, bot_name: str):
    """原始消息注入点：查会话表，命中则触发等待"""
    sid = _session_key(raw)
    with _LOCK:
        waiter = _SESSIONS.get(sid)
    if waiter is None:
        return False

    fut = waiter["future"]
    if fut.done():
        return False

    handler = waiter.get("handler")
    if handler is not None:
        try:
            if asyncio.iscoroutinefunction(handler):
                consume = await handler(raw)
            else:
                consume = await asyncio.to_thread(handler, raw)
        except Exception:
            consume = True
        if not consume:
            return False  # handler 决定不结束等待，继续等下一条

    fut.set_result(raw)
    with _LOCK:
        _SESSIONS.pop(sid, None)
    return True  # 接管：消息不再走命令匹配/关键词回复


async def wait_for_user(ctx, session_id: str, timeout: float = _DEFAULT_TIMEOUT,
                        handler=None):
    """
    等待用户下一条消息（async 版，推荐 async handler 使用）

    :param session_id: 会话键（用 make_session_id(event) 生成，或自定义）
    :param timeout: 超时秒数（None=不超时）
    :param handler: 可选回调 handler(raw_event) -> bool
                    返回 True 消费该消息并结束等待；返回 False 继续等待下一条
    :return: 下一条消息的原始 dict（含 message/user_id/group_id 等）；超时返回 None
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    with _LOCK:
        if len(_SESSIONS) >= _MAX_SESSIONS:
            _cleanup_expired_locked()
        _SESSIONS[session_id] = {
            "future": fut,
            "expires": time.time() + (timeout if timeout else 3600),
            "handler": handler,
        }
    try:
        if timeout:
            return await asyncio.wait_for(fut, timeout)
        return await fut
    except asyncio.TimeoutError:
        with _LOCK:
            _SESSIONS.pop(session_id, None)
        return None


def wait_for_user_sync(ctx, session_id: str, timeout: float = _DEFAULT_TIMEOUT,
                       handler=None):
    """
    等待用户下一条消息（同步版，供旧式同步 handler 使用；阻塞当前线程）

    用法与 wait_for_user 相同，返回下一条消息 dict 或 None（超时）
    """
    fw = getattr(ctx, "_framework", None)
    loop = getattr(fw, "loop", None)
    if loop is None or not loop.is_running():
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(
            wait_for_user(ctx, session_id, timeout, handler), loop)
        return fut.result(timeout=(timeout if timeout else 60) + 10)
    except Exception:
        return None
