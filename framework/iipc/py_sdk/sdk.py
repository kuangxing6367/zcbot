# -*- coding: utf-8 -*-
"""zcbot IIPC 插件 SDK：1:1 对齐原框架插件 API 面。

设计原则（用户定稿）：真 zcbot 插件拖进来应零改动运行。因此本文件逐项复刻
framework/ctx/* 的公开方法、framework/plugin.py 的模块级装饰器（经
framework/ 兼容包提供）、framework/messaging/event.py 的 Event 公开面。

IIPC 独特写法（性能核心）：
- send_msg / asend_msg 等发送不逐条同步往返，而是入本地队列，后台 5ms 窗口
  聚合后经 msg.send_batch 批量回传——规避旧 IPC "每调用一次就一次同步往返"
  的 8 倍削弱。
- 事件分发在独立线程 + 小线程池执行 handler，单个慢 handler 不阻塞其他事件。
- DB / 配置 / 权限这类必须拿返回值的仍走阻塞 call（可后续加本地缓存）。
- 定时任务登记到内核 cron 调度器，到期由内核推 task.tick，插件本地执行。
- 多轮会话（wait_for/create_session）由内核 session 管理器挂起/配对。
"""
import asyncio
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from iipc_client import IIPCClient
from event import Event, _RoleCache

# ─────────────────────────── 注册表 ───────────────────────────
# 单插件进程内收集装饰器/ctx 注册（register(ctx) 与模块级装饰器都汇入这里）
_registry = {
    "on": {},              # event_name -> [handler]
    "command": [],         # [cmd dict]（按 priority 排序后匹配）
    "hook": {},            # point -> [handler]
    "task": [],            # [task dict]（cron，登记到内核）
    "api": [],             # [api dict]
    "dashboard_card": [],  # [card dict]
    "group_extension": [],
    "user_extension": [],
    "raw_message": [],     # 原始消息处理器（返回 True 接管）
    "webui": None,
}

# 内核下发的命令权限策略与全局管理员（plugin.register 响应带回）
_policy = {"cmd_perm": {}, "admins": set()}

_current_ctx = None
_registry_lock = threading.Lock()


# ─────────────────────────── 日志 ───────────────────────────

class IIPCLogger:
    """插件侧日志器 —— 接口对齐标准 logging 子集 + zcbot ctx.logger 语义。

    日志在本地攒批，经 IIPC fire('log.batch') 发往内核，由内核按统一格式
    输出到 stdout，最终经 launcher 的 iipc_log 落盘到 config.yaml 的 log.file
    （与框架同一个日志文件）。插件里一律用 ctx.logger / get_logger()。
    """

    MAX_BATCH = 50
    FLUSH_INTERVAL = 0.2

    def __init__(self, client, plugin_id):
        self._client = client
        self._name = "plugin " + str(plugin_id)
        self._buf = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._flush_loop, daemon=True,
                         name="iipc-log-flush").start()

    def _emit(self, level, msg, args):
        try:
            if args:
                msg = msg % args
        except Exception:
            pass
        entry = {"level": level, "msg": str(msg), "logger": self._name}
        batch = None
        with self._lock:
            self._buf.append(entry)
            if len(self._buf) >= self.MAX_BATCH:
                batch, self._buf = self._buf, []
        if batch:
            self._send(batch)

    def _send(self, batch):
        try:
            # fire（req 帧、不等响应）：日志是高频小消息，不占同步往返
            self._client.fire("log.batch", {"items": batch})
        except Exception:
            pass  # 连接未就绪/断开时静默丢弃，不影响插件运行

    def _flush_loop(self):
        while not self._stop.wait(self.FLUSH_INTERVAL):
            with self._lock:
                batch, self._buf = self._buf, []
            if batch:
                self._send(batch)

    def flush(self):
        with self._lock:
            batch, self._buf = self._buf, []
        if batch:
            self._send(batch)

    def debug(self, msg, *args, **kwargs):
        self._emit("DEBUG", msg, args)

    def info(self, msg, *args, **kwargs):
        self._emit("INFO", msg, args)

    def warning(self, msg, *args, **kwargs):
        self._emit("WARNING", msg, args)

    warn = warning

    def error(self, msg, *args, **kwargs):
        self._emit("ERROR", msg, args)

    def exception(self, msg, *args, **kwargs):
        self._emit("ERROR", "%s | %s" % (msg, traceback.format_exc()), args)


class _FallbackLogger:
    """logger 未就绪（启动早期）时的兜底：直接 stderr。"""

    def __init__(self, name):
        self._name = name

    def _p(self, level, msg, args):
        try:
            if args:
                msg = msg % args
        except Exception:
            pass
        print("[%s] %s %s" % (self._name, level, msg), file=sys.stderr, flush=True)

    def debug(self, msg, *args, **kwargs):
        self._p("DEBUG", msg, args)

    def info(self, msg, *args, **kwargs):
        self._p("INFO", msg, args)

    def warning(self, msg, *args, **kwargs):
        self._p("WARNING", msg, args)

    warn = warning

    def error(self, msg, *args, **kwargs):
        self._p("ERROR", msg, args)

    def exception(self, msg, *args, **kwargs):
        self._p("ERROR", "%s | %s" % (msg, traceback.format_exc()), args)


def get_logger(name=None):
    """模块级日志器（对齐标准 logging.getLogger 用法）。"""
    if _current_ctx is not None:
        return _current_ctx.logger
    return _FallbackLogger(name or "plugin")


# ─────────────────────────── 命令匹配 ───────────────────────────
# 对齐 framework/messaging/router_match.py：正则/简单匹配 + / 前缀宽容 + 别名。

class SimpleMatch:
    """纯字符串匹配结果，模拟 re.Match 常用接口（与原框架同名同类）。"""

    __slots__ = ('_full', '_args')

    def __init__(self, full, args=''):
        self._full = full
        self._args = args

    def group(self, n=0):
        if n == 0:
            return self._full
        if n == 1:
            return self._args
        return None

    def groups(self):
        return (self._args,)


_REGEX_CHARS = set('^$.*+?()[]{}|\\')


def _is_regex(pattern):
    return any(c in _REGEX_CHARS for c in pattern)


def _match_simple(pattern, message):
    """纯字符串前缀匹配（/ 宽容），返回 SimpleMatch 或 None。"""
    def _try(msg):
        if msg == pattern:
            return SimpleMatch(message, '')
        if len(msg) > len(pattern) and msg.startswith(pattern):
            sep = msg[len(pattern)]
            if sep == ' ' or sep == '\t':
                return SimpleMatch(message, msg[len(pattern) + 1:].strip())
        return None

    result = _try(message)
    if result:
        return result
    if message.startswith("/") and not pattern.startswith("/"):
        result = _try(message[1:])
        if result:
            return result
    if not message.startswith("/") and pattern.startswith("/"):
        result = _try("/" + message)
        if result:
            return result
    return None


def _regex_search(rx, message):
    match = rx.search(message)
    if match:
        return match
    if message.startswith("/"):
        match = rx.search(message[1:])
        if match:
            return match
    if not message.startswith("/") and rx.pattern.startswith("^/"):
        match = rx.search("/" + message)
    return match


def _compile(pattern):
    """命令 pattern → 匹配器：含正则字符走 re，否则简单前缀。"""
    if _is_regex(pattern):
        try:
            return ("rx", re.compile(pattern))
        except re.error:
            return ("simple", pattern)
    return ("simple", pattern)


# ─────────────────────────── OneBot 动作代理 ───────────────────────────

class ActionProxy:
    """协议中立动作调用面（对齐 ctx.actions / ctx.onebot 的常用 typed 方法）。

    typed 方法（send_msg/set_group_ban/...）走阻塞 api.call（要返回值）；
    纯发送快捷面（ctx.send_msg 等）不走这里，走 SDK 批量队列。
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def call(self, action, **params):
        return self._ctx.api(action, **params)

    async def acall(self, action, **params):
        return await self._ctx.aapi(action, **params)

    # ── OneBot 11 typed 快捷（与原 onebot_api 封装同名同参）──
    def send_msg(self, user_id=None, group_id=None, message=None,
                 auto_escape=False, **kw):
        p = {"message": message, "auto_escape": auto_escape}
        if group_id is not None:
            p["message_type"] = "group"
            p["group_id"] = group_id
        else:
            p["message_type"] = "private"
            if user_id is not None:
                p["user_id"] = user_id
        p.update(kw)
        return self._ctx.api("send_msg", **p)

    async def asend_msg(self, user_id=None, group_id=None, message=None,
                        auto_escape=False, **kw):
        return await asyncio.to_thread(self.send_msg, user_id=user_id,
                                       group_id=group_id, message=message,
                                       auto_escape=auto_escape, **kw)

    def set_group_ban(self, group_id, user_id, duration=600, **kw):
        return self._ctx.api("set_group_ban", group_id=group_id,
                             user_id=user_id, duration=duration, **kw)

    def set_group_kick(self, group_id, user_id, reject_add_request=False, **kw):
        return self._ctx.api("set_group_kick", group_id=group_id, user_id=user_id,
                             reject_add_request=reject_add_request, **kw)

    def set_group_whole_ban(self, group_id, enable=True, **kw):
        return self._ctx.api("set_group_whole_ban", group_id=group_id, enable=enable, **kw)

    def set_group_card(self, group_id, user_id, card="", **kw):
        return self._ctx.api("set_group_card", group_id=group_id,
                             user_id=user_id, card=card, **kw)

    def get_group_member_list(self, group_id, **kw):
        return self._ctx.api("get_group_member_list", group_id=group_id, **kw)

    def get_group_member_info(self, group_id, user_id, **kw):
        return self._ctx.api("get_group_member_info", group_id=group_id,
                             user_id=user_id, **kw)

    def get_stranger_info(self, user_id, **kw):
        return self._ctx.api("get_stranger_info", user_id=user_id, **kw)

    def get_group_info(self, group_id, **kw):
        return self._ctx.api("get_group_info", group_id=group_id, **kw)

    def get_group_list(self, **kw):
        return self._ctx.api("get_group_list", **kw)

    def get_friend_list(self, **kw):
        return self._ctx.api("get_friend_list", **kw)

    def get_login_info(self, **kw):
        return self._ctx.api("get_login_info", **kw)

    def get_status(self, **kw):
        return self._ctx.api("get_status", **kw)

    def get_version_info(self, **kw):
        return self._ctx.api("get_version_info", **kw)

    def delete_msg(self, message_id, **kw):
        return self._ctx.api("delete_msg", message_id=message_id, **kw)

    def get_msg(self, message_id, **kw):
        return self._ctx.api("get_msg", message_id=message_id, **kw)

    def set_friend_add_request(self, flag, approve=True, **kw):
        return self._ctx.api("set_friend_add_request", flag=flag, approve=approve, **kw)

    def set_group_add_request(self, flag, sub_type, approve=True, **kw):
        return self._ctx.api("set_group_add_request", flag=flag, sub_type=sub_type,
                             approve=approve, **kw)

    def get_image(self, file, **kw):
        return self._ctx.api("get_image", file=file, **kw)

    def can_send_image(self, **kw):
        return self._ctx.api("can_send_image", **kw)

    def get_record(self, file, out_format, **kw):
        return self._ctx.api("get_record", file=file, out_format=out_format, **kw)


# ─────────────────────────── 多轮会话 ───────────────────────────

class PluginSession:
    """多轮会话对象（对齐 core_plugins/session 的 session_context）。

    用法：
        async with ctx.create_session(event, timeout=120) as sess:
            name = await sess.ask("你叫什么名字？")
    也支持同步：
        with ctx.create_session(event) as sess:
            name = sess.ask("名字？")
    """

    def __init__(self, ctx, event, timeout=60):
        self._ctx = ctx
        self._event = event
        self._timeout = timeout
        self.data = {}
        self._closed = False
        self._user_id = str(getattr(event, 'user_id', '') or '')
        self._group_id = str(getattr(event, 'group_id', '') or '')

    def ask(self, prompt=None, timeout=None):
        return self._ctx._session_wait(self._event, prompt,
                                       timeout if timeout is not None else self._timeout)

    async def aask(self, prompt=None, timeout=None):
        return await asyncio.to_thread(self.ask, prompt, timeout)

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()
        return False


# ─────────────────────────── PluginContext ───────────────────────────

class PluginContext:
    """IIPC 插件上下文：1:1 对齐框架 PluginContext 公开面。"""

    def __init__(self, client, plugin_id):
        self._client = client
        self.plugin_id = plugin_id
        self.logger = IIPCLogger(client, plugin_id)
        self._queue = []
        self._qlock = threading.Lock()
        self._config_cache = {}
        self._config_cache_ttl = 30
        self._data_dir = None
        self._role_cache = _RoleCache()
        self._pool = ThreadPoolExecutor(max_workers=8,
                                        thread_name_prefix="iipc-plugin")
        self._loop = None
        self._loop_tid = None
        self._start_async_loop()
        self._start_flush_loop()

    # ---- 装饰器 / 注册（实例方法形态，签名对齐框架 ctx.*）----

    def command(self, pattern, handler=None, priority=50, dynamic=False,
                alias=None, description=None, require_admin=False,
                require_superuser=False, require_perm=None):
        """注册命令。两种形态：
        - ctx.command(pattern, handler)            （框架原版形态）
        - @ctx.command(pattern) 装饰器             （IIPC 便利形态）
        handler 签名 (event, match)；match.group(1) 取参数。
        """
        if handler is None:
            def deco(fn):
                self.command(pattern, fn, priority=priority, dynamic=dynamic,
                             alias=alias, description=description,
                             require_admin=require_admin,
                             require_superuser=require_superuser,
                             require_perm=require_perm)
                return fn
            return deco
        if not callable(handler):
            raise TypeError("command handler 不可调用")
        if require_superuser:
            require_admin = False
        if alias is not None:
            if isinstance(alias, (list, tuple)):
                alias = ','.join(str(a).strip() for a in alias)
            else:
                alias = str(alias).strip()
        if description is None and handler.__doc__:
            description = handler.__doc__.strip().split('\n')[0].strip()
        with _registry_lock:
            _registry["command"].append({
                "pattern": pattern,
                "alias": alias,
                "description": description,
                "priority": priority,
                "handler": handler,
                "handler_name": handler.__name__,
                "is_dynamic": bool(dynamic),
                "require_level": "super" if require_superuser else ("admin" if require_admin else ""),
                "require_perm": (require_perm or "").strip().lower(),
                "matcher": _compile(pattern),
            })
            _registry["command"].sort(key=lambda c: c["priority"])
        return handler

    def on(self, event_name, handler=None):
        """订阅系统事件。ctx.on(name, handler) 或 @ctx.on(name)。"""
        if handler is None:
            def deco(fn):
                self.on(event_name, fn)
                return fn
            return deco
        with _registry_lock:
            _registry["on"].setdefault(event_name, []).append(handler)
        return handler

    def on_raw_message(self, handler):
        """注册原始消息处理器：返回 True 则接管消息（框架跳过后续处理）。"""
        if not callable(handler):
            raise TypeError("on_raw_message handler 必须可调用")
        with _registry_lock:
            _registry["raw_message"].append(handler)
        return handler

    def hook(self, point, handler=None, priority=50):
        """内核扩展点挂接（对齐框架 hooks.py：独立于事件总线的扩展点机制）。

        command.before：命令命中且权限通过后、执行前触发，handler 收单参
        payload dict（{'plugin','handler','command_id','pattern','event','match'}），
        返回 False 跳过该命令（框架 if False in _cb_res: continue 同语义）。
        其余扩展点按同名事件分发（handler 收 Event 单参）。
        """
        if handler is None:
            def deco(fn):
                self.hook(point, fn, priority=priority)
                return fn
            return deco
        if not callable(handler):
            raise TypeError("hook handler 必须可调用")
        with _registry_lock:
            _registry["hook"].setdefault(point, []).append(handler)
        return True

    def unhook(self, point):
        with _registry_lock:
            _registry["hook"].pop(point, None)
        return True

    def task(self, cron_expr, executor=None, description=None):
        """注册 cron 定时任务（对齐 ctx.task；到期内核推 task.tick，本地执行）。"""
        if executor is None:
            def deco(fn):
                self.task(cron_expr, fn, description=description)
                return fn
            return deco
        if not callable(executor):
            raise TypeError("task executor 不可调用")
        with _registry_lock:
            _registry["task"].append({
                "cron": cron_expr,
                "handler": executor,
                "handler_name": executor.__name__,
                "description": description or ("%s 定时任务" % self.plugin_id),
            })
        return executor

    def register_api(self, path, handler=None, methods=None, auth=True,
                     description=None):
        """登记自定义 REST 路由元数据（对齐 ctx.register_api）。

        IIPC 当前无内置 Web 层：路由已登记（日志可见），待 Web 等价物接入后生效。
        """
        if handler is None:
            def deco(fn):
                self.register_api(path, fn, methods=methods, auth=auth,
                                  description=description)
                return fn
            return deco
        with _registry_lock:
            _registry["api"].append({
                "path": path,
                "handler": handler,
                "methods": [m.upper() for m in (methods or ['GET'])],
                "auth": auth,
                "description": description,
            })
        self.log("已登记 API 路由 %s（IIPC Web 层就绪后挂载）" % path)
        return True

    def dashboard_card(self, title, handler=None, icon=None, priority=50):
        """登记仪表盘卡片元数据（对齐 ctx.dashboard_card）。"""
        if handler is None:
            def deco(fn):
                self.dashboard_card(title, fn, icon=icon, priority=priority)
                return fn
            return deco
        with _registry_lock:
            _registry["dashboard_card"].append({
                "title": title, "handler": handler,
                "icon": icon, "priority": priority,
            })
        return handler

    def webui(self, title, entry='index.html', icon=None, order=50, sidebar=False):
        """登记插件 WebUI 页面元数据（对齐 ctx.webui）。"""
        with _registry_lock:
            _registry["webui"] = {"title": title, "entry": entry, "icon": icon,
                                  "order": order, "sidebar": sidebar}
        self.log("已登记 WebUI 页面 %s（IIPC Web 层就绪后挂载）" % title)

    def override_webui(self):
        with _registry_lock:
            _registry["webui"] = {"override": True}
        self.log("已登记 override_webui（IIPC Web 层就绪后生效）")

    def register_group_extension(self, key, title, handler=None, ext_type='column'):
        if handler is None:
            def deco(fn):
                self.register_group_extension(key, title, fn, ext_type=ext_type)
                return fn
            return deco
        with _registry_lock:
            _registry["group_extension"].append(
                {"key": key, "title": title, "handler": handler, "ext_type": ext_type})
        return handler

    def register_user_extension(self, key, title, handler=None, ext_type='column'):
        if handler is None:
            def deco(fn):
                self.register_user_extension(key, title, fn, ext_type=ext_type)
                return fn
            return deco
        with _registry_lock:
            _registry["user_extension"].append(
                {"key": key, "title": title, "handler": handler, "ext_type": ext_type})
        return handler

    # ---- 发送（IIPC 独特写法：本地队列 + 批量 flush）----

    def send_msg(self, user_id=None, group_id=None, message=None,
                 auto_escape=False, bot=None):
        """快捷发送消息（协议中立）。入本地队列，5ms 窗口聚合后批量回传。

        与原框架的差异：不返回 message_id（批量通道无逐条返回值）；
        需要 message_id 的场景请用 ctx.actions.send_msg（阻塞 api.call）。
        """
        with self._qlock:
            self._queue.append({
                "message": message,
                "user_id": user_id,
                "group_id": group_id,
            })

    async def asend_msg(self, user_id=None, group_id=None, message=None,
                        auto_escape=False, bot=None):
        """异步快捷发送（对齐 asend_msg；同样走批量队列，不阻塞）。"""
        self.send_msg(user_id=user_id, group_id=group_id, message=message)

    def send_private_msg(self, user_id, message):
        with self._qlock:
            self._queue.append({"message": message, "user_id": user_id})

    def send_group_msg(self, group_id, message):
        with self._qlock:
            self._queue.append({"message": message, "group_id": group_id})

    def _flush(self):
        with self._qlock:
            if not self._queue:
                return
            batch = self._queue[:]
            self._queue.clear()
        try:
            if len(batch) == 1:
                self._client.call("msg.send", batch[0])
            else:
                self._client.call("msg.send_batch", {"items": batch})
        except Exception as e:
            print("[sdk] flush 失败: %s" % e)

    def _start_flush_loop(self):
        def loop():
            while True:
                time.sleep(0.005)  # 5ms 聚合窗口
                self._flush()
        threading.Thread(target=loop, daemon=True).start()

    # ---- 快捷动作（typed，阻塞 api.call）----

    def ban(self, group_id, user_id, duration=600, bot=None):
        return self.api("set_group_ban", group_id=group_id, user_id=user_id, duration=duration)

    def kick(self, group_id, user_id, reject_add_request=False, bot=None):
        return self.api("set_group_kick", group_id=group_id, user_id=user_id,
                        reject_add_request=reject_add_request)

    def mute_all(self, group_id, enable=True, bot=None):
        return self.api("set_group_whole_ban", group_id=group_id, enable=enable)

    def set_card(self, group_id, user_id, card, bot=None):
        return self.api("set_group_card", group_id=group_id, user_id=user_id, card=card)

    def get_member_list(self, group_id, bot=None):
        return self.api("get_group_member_list", group_id=group_id)

    def get_member_info(self, group_id, user_id, bot=None):
        return self.api("get_group_member_info", group_id=group_id, user_id=user_id)

    def delete_msg(self, message_id):
        return self.api("delete_msg", message_id=message_id)

    def get_login_info(self):
        return self.api("get_login_info")

    def get_stranger_info(self, user_id):
        return self.api("get_stranger_info", user_id=user_id)

    def get_group_info(self, group_id):
        return self.api("get_group_info", group_id=group_id)

    # ---- 身份 / 权限 ----

    def _role_bridge(self, user_id, group_id):
        """Event.role 的惰性查询桥：查 users / group_members（带 60s TTL 缓存）。"""
        key = (str(user_id), str(group_id))
        return self._role_cache.get_or_fetch(key, lambda: self._fetch_role(user_id, group_id))

    def _fetch_role(self, user_id, group_id):
        try:
            urow = self.db_query_one(
                "SELECT role, is_blacklist FROM users WHERE user_id=%s", (user_id,))
            if urow and (urow.get('role') == 'super'):
                return {"role": "super"}
            if urow and int(urow.get('is_blacklist') or 0) == 1:
                return {"role": "blacklist"}
            if str(group_id) not in ("", "0", "None"):
                grow = self.db_query_one(
                    "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                    (group_id, user_id))
                if grow and grow.get('role') in ('owner', 'admin'):
                    return {"role": grow['role']}
            return {"role": "member"}
        except Exception:
            return {"role": "member"}

    def is_group_admin(self, group_id, user_id):
        try:
            row = self.db_query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id))
            return bool(row and row['role'] in ('owner', 'admin'))
        except Exception:
            return False

    def is_group_owner(self, group_id, user_id):
        try:
            row = self.db_query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id))
            return bool(row and row['role'] == 'owner')
        except Exception:
            return False

    def is_superuser(self, user_id):
        try:
            row = self.db_query_one(
                "SELECT role FROM users WHERE user_id=%s", (user_id,))
            return bool(row and row.get('role') == 'super')
        except Exception:
            return False

    def is_blacklisted(self, user_id):
        try:
            row = self.db_query_one(
                "SELECT is_blacklist FROM users WHERE user_id=%s", (user_id,))
            return bool(row and int(row.get('is_blacklist') or 0) == 1)
        except Exception:
            return False

    def get_user_role(self, group_id, user_id):
        if self.is_superuser(user_id):
            return "super"
        if self.is_blacklisted(user_id):
            return "blacklist"
        if self.is_group_owner(group_id, user_id):
            return "owner"
        if self.is_group_admin(group_id, user_id):
            return "admin"
        return "member"

    def has_perm(self, user_id, node, context=None, role=None):
        """节点式权限判断（经内核 perm 解析器；未定义按拒绝）。"""
        ctx = {}
        if context:
            for k, v in context.items():
                ctx[str(k)] = str(v)
        res = self._client.call("perm.has_perm", {
            "user_id": str(user_id), "node": node,
            "context": ctx, "role": role or "",
        })
        return bool(res.get("ok")) if isinstance(res, dict) else False

    def check_perm(self, user_id, node, context=None, role=None):
        return self.has_perm(user_id, node, context=context, role=role)

    def user_groups(self, user_id, context=None, role=None):
        ctx = {}
        if context:
            for k, v in context.items():
                ctx[str(k)] = str(v)
        res = self._client.call("perm.user_groups", {
            "user_id": str(user_id), "context": ctx, "role": role or "",
        })
        return res.get("groups", []) if isinstance(res, dict) else []

    # ---- 群级插件开关 ----

    def enable_plugin_in_group(self, plugin_name, group_id):
        return self._client.call("group_plugin.set",
                                 {"group_id": str(group_id), "plugin": plugin_name,
                                  "enabled": True})

    def disable_plugin_in_group(self, plugin_name, group_id):
        return self._client.call("group_plugin.set",
                                 {"group_id": str(group_id), "plugin": plugin_name,
                                  "enabled": False})

    def is_plugin_enabled_in_group(self, plugin_name, group_id):
        res = self._client.call("group_plugin.get", {"group_id": str(group_id)})
        status = res.get("status", {}) if isinstance(res, dict) else {}
        return bool(status.get(plugin_name, True))

    def get_plugin_status_list(self, group_id):
        res = self._client.call("group_plugin.get", {"group_id": str(group_id)})
        return res.get("status", {}) if isinstance(res, dict) else {}

    # ---- DB ----

    def db_query(self, sql, params=None):
        res = self._client.call("db.query", {"sql": sql, "args": list(params or [])})
        return res.get("rows", []) if isinstance(res, dict) else []

    def db_query_one(self, sql, params=None):
        rows = self.db_query(sql, params)
        return rows[0] if rows else None

    def db_execute(self, sql, params=None):
        res = self._client.call("db.exec", {"sql": sql, "args": list(params or [])})
        return res.get("affected", 0) if isinstance(res, dict) else 0

    def db_execute_many(self, sql, params_list):
        n = 0
        for p in (params_list or []):
            n += int(self.db_execute(sql, p) or 0)
        return n

    def db_insert(self, sql, params=None):
        res = self._client.call("db.insert", {"sql": sql, "args": list(params or [])})
        return res.get("id", 0) if isinstance(res, dict) else 0

    def create_table(self, ddl: str, **kwargs):
        """插件建表统一入口（对齐 framework/ctx/db.py:35 / ipc/remote_db.py:76）。

        原框架语义：DDL 直接交给核心的真实 Database 执行，由核心做方言适配
        （MySQL 写法 AUTO_INCREMENT / ENUM / 长列索引 等在 SQLite 侧翻译）。
        IIPC 同理：走 db.exec，内核 TranslateSQL 负责 %s→? 等翻译。
        """
        try:
            return self.db_execute(ddl)
        except Exception as e:
            try:
                self.log("建表失败: %s" % e, level="error")
            except Exception:
                pass
            raise

    async def create_table_async(self, ddl: str, **kwargs):
        return await asyncio.to_thread(self.create_table, ddl, **kwargs)

    async def db_query_async(self, sql, params=None):
        return await asyncio.to_thread(self.db_query, sql, params)

    async def db_query_one_async(self, sql, params=None):
        return await asyncio.to_thread(self.db_query_one, sql, params)

    async def db_execute_async(self, sql, params=None):
        return await asyncio.to_thread(self.db_execute, sql, params)

    async def db_execute_many_async(self, sql, params_list):
        return await asyncio.to_thread(self.db_execute_many, sql, params_list)

    async def db_insert_async(self, sql, params=None):
        return await asyncio.to_thread(self.db_insert, sql, params)

    def db_pool_status(self):
        return {"mode": "iipc", "backend": "go-sqlite", "note": "DB 在 Go 内核内执行"}

    # 兼容别名：IIPC 早期演示插件用 db_exec/db_exec_many 短名（1:1 正名为 db_execute）
    db_exec = db_execute
    db_exec_many = db_execute_many

    # ---- 配置 ----

    def get_config(self, key, default=None):
        cache_key = key
        now = time.time()
        cached = self._config_cache.get(cache_key)
        if cached is not None and now - cached[1] < self._config_cache_ttl:
            return cached[0]
        try:
            res = self._client.call("config.get", {"key": key, "default": default})
            value = res.get("value") if isinstance(res, dict) else default
            try:
                decoded = json.loads(value) if isinstance(value, str) else value
            except (json.JSONDecodeError, TypeError):
                decoded = value
            self._config_cache[cache_key] = (decoded, now)
            return decoded
        except Exception:
            return default

    def set_config(self, key, value):
        return self._client.call("config.set", {"key": key, "value": str(value)})

    def get_all_config(self):
        """插件全部配置（经 db.query 直查 plugin_configs 表，与框架同源）。"""
        try:
            rows = self.db_query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name=%s",
                (self.plugin_id,))
            result = {}
            for r in rows:
                v = r.get('config_value')
                try:
                    result[r['config_key']] = json.loads(v) if isinstance(v, str) else v
                except (json.JSONDecodeError, TypeError):
                    result[r['config_key']] = v
            return result
        except Exception:
            return {}

    # ---- 协议 API ----

    def api(self, action, bot=None, **params):
        """调用协议 API（同步桥接；对齐 ctx.api(action, bot, **params)）。"""
        return self._client.call("api.call", {
            "action": action, "params": params, "self_id": bot or "",
        })

    async def aapi(self, action, bot=None, **params):
        return await asyncio.to_thread(self.api, action, bot, **params)

    @property
    def actions(self):
        """协议中立动作调用面（对齐 ctx.actions）。"""
        return ActionProxy(self)

    @property
    def onebot(self):
        """兼容别名：等价于 ctx.actions（对齐 ctx.onebot）。"""
        return self.actions

    # ---- 事件发布 ----

    def emit(self, event_name, payload=None):
        return self._client.call("event.emit", {"type": event_name, "data": payload or {}})

    async def aemit(self, event_name, payload=None):
        return await asyncio.to_thread(self.emit, event_name, payload)

    # ---- 文本提取 ----

    def get_text(self, message_or_event):
        """从消息/事件提取纯文本（对齐 ctx.get_text）。"""
        if isinstance(message_or_event, dict):
            text = (message_or_event.get('message')
                    or message_or_event.get('raw_message')
                    or message_or_event.get('text'))
        elif hasattr(message_or_event, 'message'):
            text = getattr(message_or_event, 'message', None) \
                or getattr(message_or_event, 'raw_message', None)
        else:
            text = message_or_event
        from event import _extract_text
        return _extract_text(text or '')

    # ---- 日志 ----

    def log(self, msg, level='info'):
        getattr(self.logger, level.lower(), self.logger.info)(msg)

    # ---- 异步执行 ----

    def run_async(self, func, *args, **kwargs):
        """异步执行耗时操作（线程池，返回 Future；对齐 ctx.run_async）。"""
        return self._pool.submit(func, *args, **kwargs)

    def call_async(self, coro):
        """把协程调度到插件进程的事件循环（对齐 ctx.call_async）。"""
        if self._loop is None:
            raise RuntimeError("插件事件循环未运行")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _start_async_loop(self):
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_tid = threading.get_ident()
            self._loop.run_forever()
        threading.Thread(target=run, daemon=True, name="iipc-plugin-loop").start()
        # 等 loop 就绪（极短）
        for _ in range(200):
            if self._loop is not None:
                break
            time.sleep(0.005)

    # ---- 审计 ----

    def audit_log(self, action, target_type=None, target_name=None, detail=None,
                  result='success', error_message=None):
        """记录插件操作审计日志（对齐 ctx.audit_log）。"""
        try:
            self.db_execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, "
                "target_name, detail, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (0, "plugin:%s" % self.plugin_id, "plugin.%s" % action,
                 target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 result, error_message))
        except Exception as e:
            self.logger.warning("审计日志写入失败: %s" % e)

    # ---- 数据目录 ----

    def get_data_dir(self):
        """插件数据目录（plugins_dat/<plugin>/，由内核创建；对齐 ctx.get_data_dir）。"""
        if self._data_dir is None:
            res = self._client.call("data.dir", {})
            self._data_dir = res.get("path") if isinstance(res, dict) else None
            if self._data_dir:
                try:
                    os.makedirs(self._data_dir, exist_ok=True)
                except Exception:
                    pass
        return self._data_dir

    # ---- 多轮会话 ----

    def wait_for(self, event, prompt=None, timeout=60, handler=None):
        """等待用户下一条消息（对齐 ctx.wait_for；内核 session 管理器挂起）。"""
        return self._session_wait(event, prompt, timeout, handler)

    def _session_wait(self, event, prompt=None, timeout=60, handler=None):
        if prompt:
            gid = getattr(event, 'group_id', None)
            uid = getattr(event, 'user_id', None)
            if gid not in (None, 0, "0", ""):
                self.send_msg(group_id=gid, message=prompt)
            elif uid:
                self.send_msg(user_id=uid, message=prompt)
        deadline = time.time() + float(timeout or 60)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            res = self._client.call("session.wait", {
                "user_id": str(getattr(event, 'user_id', '') or ''),
                "group_id": str(getattr(event, 'group_id', '') or ''),
                "timeout": remaining,
            })
            if not (isinstance(res, dict) and res.get("ok")):
                return None
            ev_raw = res.get("event") or {}
            if handler is not None:
                try:
                    if not handler(ev_raw):
                        continue
                except Exception:
                    continue
            return ev_raw

    def create_session(self, event, timeout=60):
        """创建会话对象（async with / with 均可用；对齐 ctx.create_session）。"""
        return PluginSession(self, event, timeout=timeout)

    @property
    def plugin_name(self):
        return self.plugin_id


# ─────────────────────────── 事件分发 ───────────────────────────

def _perm_denied(prefix):
    """命令是否声明 admin 级权限：内核策略优先，其次装饰器声明。"""
    perm = _policy["cmd_perm"].get(prefix)
    if perm is None:
        with _registry_lock:
            for c in _registry["command"]:
                if c["pattern"] == prefix:
                    perm = c["require_level"]
                    break
    return perm == "admin"


def _on_event(payload):
    """内核事件 → Event 对象 → 原框架式分发（raw → command → on）。"""
    ctx = _current_ctx
    etype = payload.get("type")
    data = payload.get("data", {}) or {}
    bot_name = str(data.get("bot_name") or data.get("self_id") or "default")
    ev = Event(data, bot_name=bot_name,
               perm_bridge=(ctx._role_bridge if ctx else None),
               has_perm_fn=(ctx.has_perm if ctx else None))
    if ctx is not None:
        ev._ctx = ctx  # 便捷反引（部分插件写法需要从事件拿回 ctx）

    # 1) 原始消息处理器（框架命令匹配之前的注入点；返回 True 接管）
    if etype == "message":
        with _registry_lock:
            raw_handlers = list(_registry["raw_message"])
        for h in raw_handlers:
            try:
                if h(ev, bot_name) is True:
                    return
            except Exception as e:
                print("[sdk] raw_message 处理器异常: %s" % e)

    # 2) 命令匹配（每插件首个命中即执行，与原框架一致；返回 False 继续匹配）
    if etype == "message":
        text = ev.message or ""
        uid = str(ev.user_id)
        with _registry_lock:
            commands = list(_registry["command"])
        for c in commands:
            match = None
            kind, matcher = c["matcher"]
            if kind == "rx":
                match = _regex_search(matcher, text)
            else:
                match = _match_simple(matcher, text)
            if not match and c["alias"]:
                for alias in c["alias"].split(','):
                    alias = alias.strip()
                    if alias:
                        match = _match_simple(alias, text)
                        if match:
                            break
            if not match:
                continue
            # 黑名单拦截（对齐框架：命中黑名单直接拒绝执行）
            if ev.role == 'blacklist':
                return
            # 权限检查（require_level / require_perm / 内核下发表）
            require = c["require_level"]
            if require == "admin" and not ev.is_admin:
                ctx.send_msg(user_id=ev.user_id or None, group_id=ev.group_id or None,
                             message="权限不足（需要 admin 权限，当前身份: %s）" % ev.role)
                return
            if require == "super" and not ev.is_superuser:
                ctx.send_msg(user_id=ev.user_id or None, group_id=ev.group_id or None,
                             message="权限不足（需要超级管理员权限）")
                return
            if c["require_perm"] and not ev.has_perm(c["require_perm"]):
                ctx.send_msg(user_id=ev.user_id or None, group_id=ev.group_id or None,
                             message="权限不足（需要权限节点: %s）" % c["require_perm"])
                return
            # command.before 扩展点（对齐框架 router_match：权限通过后、执行前触发；
            # handler 收单参 payload dict，返回 False 跳过该命令继续尝试其它匹配）
            with _registry_lock:
                has_cb_hook = bool(_registry["hook"].get("command.before"))
            if has_cb_hook:
                payload = {
                    "plugin": getattr(ctx, "plugin_id", ""),
                    "handler": getattr(c["handler"], "__name__", ""),
                    "command_id": c["pattern"],
                    "pattern": c["pattern"],
                    "event": ev,
                    "match": match,
                }
                if False in _trigger_hooks("command.before", payload):
                    continue
            # 执行（线程池：单 handler 慢不阻塞其他事件；异常走 on_error 钩子）
            _invoke(c["handler"], ev, match)
            return  # 一个命令命中即止（对齐框架 _match_plugin_commands）

    # 3) 定时任务 tick（内核定向推送）
    if etype == "task.tick":
        handlers = data.get("handlers") or []
        with _registry_lock:
            by_name = {t["handler_name"]: t["handler"] for t in _registry["task"]}
        for name in handlers:
            fn = by_name.get(name)
            if fn:
                _invoke_task(fn)
        return

    # 4) on(etype) 处理器 + hook(point) 非命令扩展点（按同名事件分发，收 Event 单参；
    #    command.before 只在命令路径触发，不参与事件分发）
    with _registry_lock:
        handlers = list(_registry["on"].get(etype, []))
        handlers += [h for p, hs in _registry["hook"].items()
                     if p != "command.before" and p == etype for h in hs]
    for h in handlers:
        _invoke(h, ev)


def _trigger_hooks(point, payload):
    """触发扩展点（对齐框架 hooks.trigger_async）：同步直接调，async 走 loop。

    返回各 handler 返回值列表（供 command.before 的 False 跳过判定）。
    handler 异常不中断命令链（框架 router_match 同语义：记录后继续）。
    """
    with _registry_lock:
        handlers = list(_registry["hook"].get(point, []))
    results = []
    for h in handlers:
        if inspect.iscoroutinefunction(h):
            ctx = _current_ctx
            loop = ctx._loop if ctx else None
            if loop is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(h(payload), loop)
                try:
                    results.append(fut.result(timeout=30))
                except Exception as e:
                    print("[sdk] async hook %s 异常: %s" % (point, e))
            else:
                try:
                    results.append(asyncio.run(h(payload)))
                except Exception as e:
                    print("[sdk] async hook %s 异常: %s" % (point, e))
        else:
            try:
                results.append(h(payload))
            except Exception as e:
                print("[sdk] hook %s 异常: %s" % (point, e))
    return results


def _invoke(fn, *args):
    """执行 handler：同步直接跑在线程池；async 走进程内 loop（不阻塞分发）。"""
    if inspect.iscoroutinefunction(fn):
        ctx = _current_ctx
        loop = ctx._loop if ctx else None
        if loop is not None and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(fn(*args), loop)
            try:
                fut.result(timeout=30)
            except Exception as e:
                print("[sdk] async handler 异常: %s" % e)
        else:
            try:
                asyncio.run(fn(*args))
            except Exception as e:
                print("[sdk] async handler 异常: %s" % e)
        return None
    ctx = _current_ctx
    if ctx is not None:
        fut = ctx._pool.submit(_safe_call, fn, args)
        try:
            return fut.result(timeout=60)
        except Exception as e:
            print("[sdk] handler 异常: %s" % e)
            return None
    return _safe_call(fn, args)


def _invoke_task(fn):
    ctx = _current_ctx
    if ctx is not None:
        ctx._pool.submit(_safe_call, fn, ())
    else:
        _safe_call(fn, ())


def _safe_call(fn, args):
    try:
        return fn(*args)
    except Exception as e:
        print("[sdk] handler %s 异常: %s" % (getattr(fn, '__name__', '?'), e))
        on_error = getattr(_current_module, 'on_error', None) if _current_module else None
        if callable(on_error):
            try:
                on_error(*args, e)
            except Exception:
                pass
        return None


# 当前插件模块（供 on_error 钩子定位）
_current_module = None


# ─────────────────────────── 插件装配 ───────────────────────────

def _load_plugin_module(plugin_dir, plugin_id):
    """加载插件 main.py：挂在合成包 plugin_<id> 下，支持插件内相对导入。

    对齐框架 loader 的三级命名：plugin_<id>.main（规范）/ plugin_<id>_main（旧版）/
    main（短名）。插件目录入 sys.path，`import sibling` 同样可用。
    """
    main_path = os.path.join(plugin_dir, "main.py")
    pkg_name = "plugin_%s" % re.sub(r"\W", "_", plugin_id)
    # 合成包：让 from .mod import X 解析到本插件目录
    pkg_mod = type(sys)(pkg_name)
    pkg_mod.__path__ = [plugin_dir]
    pkg_mod.__package__ = pkg_name
    sys.modules[pkg_name] = pkg_mod

    spec = importlib.util.spec_from_file_location(
        pkg_name + ".main", main_path,
        submodule_search_locations=[plugin_dir])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name + ".main"] = mod
    sys.modules[pkg_name + "_main"] = mod  # 旧版下划线唯一名兼容
    sys.modules["main"] = mod              # 短名兼容（绝对导入）
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    spec.loader.exec_module(mod)
    return mod, pkg_name + ".main"


def run_plugin(plugin_dir, plugin_id, addr, auth):
    """插件进程主流程：连接内核 → 加载模块 → register(ctx) → 模块装饰器 flush → 上报。"""
    global _current_ctx, _current_module
    client = IIPCClient(addr, auth)
    # 先注册事件处理，再上报订阅，避免漏事件
    client.on("event", _on_event)

    ctx = PluginContext(client, plugin_id)
    _current_ctx = ctx

    # 加载用户插件 main.py
    mod, module_name = _load_plugin_module(plugin_dir, plugin_id)
    _current_module = mod
    # 与原框架 loader/base.py:231（module.ctx = ctx）对齐：把 ctx 注入插件模块全局。
    # 官方插件（含 docs/writing-plugins.md 的示例与 plugins/llm_blacklist）在 handler
    # 内直接使用裸 ctx，register(ctx) 里并没有 global 声明 —— 缺这一步会 NameError。
    try:
        mod.ctx = ctx
    except Exception as e:
        print("[plugin %s] ctx 注入模块全局失败: %s" % (plugin_id, e))
    if hasattr(mod, "register"):
        mod.register(ctx)

    # 模块级装饰器（from framework.plugin import ...）落账
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
        import framework.plugin as _fw_plugin
        _fw_plugin.flush(module_name, ctx)
    except Exception as e:
        print("[plugin %s] framework.plugin flush 失败: %s" % (plugin_id, e))

    # 汇总注册面
    with _registry_lock:
        event_types = set(_registry["on"].keys())
        commands = [{
            "pattern": c["pattern"], "alias": c["alias"] or "",
            "description": c["description"] or "", "priority": c["priority"],
            "handler": c["handler_name"], "require_level": c["require_level"],
            "require_perm": c["require_perm"], "is_dynamic": c["is_dynamic"],
        } for c in _registry["command"]]
        tasks = [{"handler": t["handler_name"], "cron": t["cron"]}
                 for t in _registry["task"]]
    if commands or _registry["raw_message"]:
        event_types.add("message")
    if tasks:
        event_types.add("task.tick")

    res = client.call("plugin.register", {
        "plugin_id": plugin_id,
        "event_types": sorted(event_types),
        "commands": commands,
        "tasks": tasks,
    })
    # 内核下发的命令权限策略与全局管理员
    if isinstance(res, dict):
        _policy["cmd_perm"] = res.get("cmd_perm", {}) or {}
        _policy["admins"] = set(str(a) for a in (res.get("admins", []) or []))

    # cron 任务逐个登记到内核调度器
    for t in tasks:
        try:
            client.call("task.register", {"handler": t["handler"], "cron": t["cron"]})
        except Exception as e:
            print("[plugin %s] 任务 %s 登记失败: %s" % (plugin_id, t["handler"], e))

    print("[plugin %s] 已注册，订阅事件=%s，命令=%d，任务=%d，管理员=%s"
          % (plugin_id, sorted(event_types), len(commands), len(tasks),
             sorted(_policy["admins"])))
    ctx.logger.flush()
    # 保持进程存活；一旦与内核连接断开（内核退出 / 被 SIGKILL / 被踢），立即自行退出，
    # 避免成为孤儿进程后又重连到新内核、重复订阅导致每条消息被双份处理。
    while client.is_alive():
        time.sleep(0.5)
    print("[plugin %s] 与内核连接已断开，退出进程（防止孤儿重连）" % plugin_id)
    ctx.logger.flush()
    sys.exit(0)
