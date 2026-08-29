"""
消息守卫插件 (message_guard)
============================
在消息处理最前端（on_raw_message，priority=2）做四道可配置的守卫，
任一拦截即「静默丢弃」（机器人不响应）：

1. 唤醒词：仅 @机器人 / 唤醒前缀开头的消息才响应（群聊防打扰）
2. 群白名单 / 用户黑名单
3. 限流：按「用户+群」固定窗口，超限丢弃
4. 敏感词：命中敏感词的消息直接丢弃

配置（Web UI 插件配置页可改，60 秒生效）：
    wake_enable         是否启用唤醒词（默认 false）
    wake_prefixes       唤醒前缀列表（逗号分隔，如 "/bot,bot,"）
    wake_private        私聊是否也需要唤醒（默认 false=私聊全放行）
    whitelist_enable    是否启用群白名单（默认 false）
    whitelist_groups    白名单群号列表（逗号分隔）
    blacklist_users     用户黑名单（逗号分隔 QQ 号）
    rate_enable         是否启用限流（默认 false）
    rate_count          窗口内允许的消息数（默认 5）
    rate_seconds        窗口秒数（默认 10）
    sensitive_enable    是否启用敏感词过滤（默认 false）
    sensitive_words     敏感词列表（逗号分隔，包含即命中）
    exempt_admin        管理员/群主/超管是否豁免（默认 true）

实现说明：
- 拦截语义 = on_raw_message 返回 True（框架跳过该消息后续处理）
- 守卫优先级（priority=2）晚于 session_waiter(1)，会话等待中的消息不被守卫拦截
- 配置 60s 内存缓存，避免每条消息查库
"""
import os
import sys
import time
from collections import deque

__plugin_meta__ = {
    "name": "消息守卫",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "唤醒词 / 群白名单 / 用户黑名单 / 限流 / 敏感词过滤（配置驱动）",
    "priority": 2,
}

_CFG_TTL = 60          # 配置缓存 TTL（秒）
_cfg_cache = {"t": 0, "data": None}
# 限流窗口：sid -> deque[timestamps]
_rate_windows = {}
_RATE_MAX_SESSIONS = 5000
_rate_checks = 0


def register(ctx):
    ctx.on_raw_message(_on_raw_message)
    # 配置读取自带 60s TTL 惰性刷新（_get_config），无需每分钟定时任务；
    # 仅保留限流窗口清理（防内存增长）
    ctx.task("*/10 * * * *", _cleanup_rate_windows, description="清理限流窗口")
    ctx.log("[message_guard] 消息守卫已就绪（priority=2，配置 60s 生效）")


# ---------------- 配置 ----------------

def _load_config(ctx) -> dict:
    return {
        "wake_enable": _cfg_bool(ctx, "wake_enable", False),
        "wake_prefixes": _cfg_list(ctx, "wake_prefixes"),
        "wake_private": _cfg_bool(ctx, "wake_private", False),
        "whitelist_enable": _cfg_bool(ctx, "whitelist_enable", False),
        "whitelist_groups": _cfg_int_list(ctx, "whitelist_groups"),
        "blacklist_users": _cfg_int_list(ctx, "blacklist_users"),
        "rate_enable": _cfg_bool(ctx, "rate_enable", False),
        "rate_count": _cfg_int(ctx, "rate_count", 5),
        "rate_seconds": _cfg_int(ctx, "rate_seconds", 10),
        "sensitive_enable": _cfg_bool(ctx, "sensitive_enable", False),
        "sensitive_words": _cfg_list(ctx, "sensitive_words"),
        "exempt_admin": _cfg_bool(ctx, "exempt_admin", True),
    }


def _cfg_bool(ctx, key, default):
    v = ctx.get_config(key, default)
    return str(v).lower() in ("1", "true", "yes", "on") if v is not None else default


def _cfg_list(ctx, key):
    v = ctx.get_config(key, None)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return []


def _cfg_int(ctx, key, default):
    try:
        return int(ctx.get_config(key, default))
    except Exception:
        return default


def _cfg_int_list(ctx, key):
    out = []
    for x in _cfg_list(ctx, key):
        try:
            out.append(int(float(x)))
        except ValueError:
            continue
    return out


def _get_config(ctx):
    now = time.time()
    c = _cfg_cache
    if c["data"] is None or (now - c["t"]) > _CFG_TTL:
        try:
            c["data"] = _load_config(ctx)
            c["t"] = now
        except Exception:
            if c["data"] is None:
                c["data"] = {}
    return c["data"]


def _refresh_config():
    try:
        _cfg_cache["data"] = _load_config(ctx)
        _cfg_cache["t"] = time.time()
    except Exception:
        pass


# ---------------- 限流 ----------------

def _rate_check(ctx, sid: str) -> bool:
    """固定窗口限流：超限返回 True（应丢弃）"""
    cfg = _get_config(ctx)
    count = cfg.get("rate_count", 5)
    seconds = cfg.get("rate_seconds", 10)
    if count <= 0 or seconds <= 0:
        return False
    global _rate_checks
    _rate_checks += 1
    if _rate_checks % 256 == 0 and len(_rate_windows) > _RATE_MAX_SESSIONS:
        _cleanup_rate_windows()
    now = time.time()
    w = _rate_windows.setdefault(sid, deque())
    while w and now - w[0] > seconds:
        w.popleft()
    if len(w) >= count:
        return True
    w.append(now)
    return False


def _cleanup_rate_windows():
    """清理超限的限流窗口（防内存增长）"""
    try:
        now = time.time()
        for sid in [s for s, w in _rate_windows.items() if not w or now - w[-1] > 300]:
            _rate_windows.pop(sid, None)
    except Exception:
        pass


# ---------------- 守卫主逻辑 ----------------

async def _on_raw_message(raw: dict, bot_name: str):
    """原始消息注入点：四道守卫，任一拦截即丢弃（返回 True 接管）"""
    try:
        cfg = _get_config(ctx)
        post_type = raw.get("post_type")
        if post_type != "message":
            return False  # 非消息事件不拦截
        message_type = raw.get("message_type", "")
        user_id = raw.get("user_id", 0)
        group_id = raw.get("group_id") or 0
        text = raw.get("message", "")
        if isinstance(text, list):
            text = "".join(
                s.get("data", {}).get("text", "") for s in text
                if isinstance(s, dict) and s.get("type") == "text"
            )
        text = str(text or "")

        # 0. 管理员豁免
        if cfg.get("exempt_admin", True) and user_id:
            try:
                from framework.event import Event
                ev = Event(raw, bot_name)
                ev._framework = ctx._framework
                if ev.is_admin:
                    return False
            except Exception:
                pass

        # 1. 用户黑名单
        if user_id and user_id in cfg.get("blacklist_users", []):
            return True

        # 2. 群白名单
        if message_type == "group" and group_id and cfg.get("whitelist_enable", False):
            if group_id not in cfg.get("whitelist_groups", []):
                return True

        # 3. 唤醒词：仅当配置了唤醒前缀时才启用（空前缀 + 未@机器人不拦截，
        #    避免误开 wake_private 且前缀为空导致私聊消息全部被丢弃）
        wake_prefixes = cfg.get("wake_prefixes", [])
        if wake_prefixes and message_type == "group" and cfg.get("wake_enable", False):
            if not _is_wake(raw, text, wake_prefixes):
                return True
        elif wake_prefixes and message_type == "private" and cfg.get("wake_private", False):
            if not _is_wake(raw, text, wake_prefixes):
                return True

        # 4. 限流（仅对"要响应"的消息计数）
        if cfg.get("rate_enable", False) and user_id:
            sid = f"{user_id}:{group_id}"
            if _rate_check(ctx, sid):
                return True

        # 5. 敏感词
        if cfg.get("sensitive_enable", False) and text:
            words = cfg.get("sensitive_words", [])
            if words and any(w and w in text for w in words):
                return True

        return False
    except Exception as e:
        ctx.log(f"[message_guard] 守卫异常（放行）: {e}", level="error")
        return False


def _is_wake(raw: dict, text: str, prefixes) -> bool:
    """判断是否命中唤醒条件：@机器人 或 唤醒前缀"""
    # @机器人
    self_id = str(raw.get("self_id", ""))
    msg = raw.get("message", "")
    if isinstance(msg, list):
        for s in msg:
            if (isinstance(s, dict) and s.get("type") == "at"
                    and str(s.get("data", {}).get("qq", "")) == self_id):
                return True
    # 唤醒前缀
    for p in prefixes:
        if p and text.startswith(p):
            return True
    return False
