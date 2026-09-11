# -*- coding: utf-8 -*-
"""
扩展点系统（Hook Registry）—— 微内核的核心契约

微内核只负责最小必要能力：加载插件、路由事件、提供公共服务
（数据库 / 权限 / 服务注册 / 事件总线 / 运行时上下文）。其余一切"行为"
都通过**扩展点（hook point）**向插件与扩展开放，使任意功能都能挂载到
几乎每一个运行环节：

  lifecycle.startup / lifecycle.shutdown          进程启动与关闭
  http.before_request / http.after_request       Web 请求前后（before 可短路）
  event.before_dispatch / event.after_dispatch   事件进入内核前后（before 返回 False 丢弃）
  command.before / command.after                命令执行前后（before 返回 False 跳过）
  message.before_send / message.after_send      框架主动发文本前后
  action.before / action.after                  任意协议动作调用前后（通知，不短路）

插件侧用 `ctx.hook(point, handler, priority=50)` 注册；同名（插件内）重复注册自动去重。
handler 既可以是普通函数，也可以是 `async def`：
- 事件循环内（内核管线）用 `trigger_async`，可安全 `await`；
- Web / 线程上下文（Flask 请求）用 `trigger_sync`，async handler 会经内核事件循环
  fire-and-forget 执行，绝不阻塞请求线程。
"""
import asyncio
import logging
import threading
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger('zcbot')


class HookPoints:
    """标准扩展点常量（插件也可注册自定义扩展点，字符串点位即可）。"""
    LIFECYCLE_STARTUP = 'lifecycle.startup'
    LIFECYCLE_SHUTDOWN = 'lifecycle.shutdown'
    HTTP_BEFORE_REQUEST = 'http.before_request'
    HTTP_AFTER_REQUEST = 'http.after_request'
    EVENT_BEFORE_DISPATCH = 'event.before_dispatch'
    EVENT_AFTER_DISPATCH = 'event.after_dispatch'
    COMMAND_BEFORE = 'command.before'
    COMMAND_AFTER = 'command.after'
    MESSAGE_BEFORE_SEND = 'message.before_send'
    MESSAGE_AFTER_SEND = 'message.after_send'
    ACTION_BEFORE = 'action.before'
    ACTION_AFTER = 'action.after'


# 各扩展点约定返回 False 时是否触发"短路"语义
_SHORT_CIRCUIT = {
    HookPoints.HTTP_BEFORE_REQUEST: True,     # 返回 Flask Response 即短路
    HookPoints.EVENT_BEFORE_DISPATCH: True,   # 返回 False 丢弃事件
    HookPoints.COMMAND_BEFORE: True,          # 返回 False 跳过该命令
    HookPoints.MESSAGE_BEFORE_SEND: True,     # 返回 False 取消发送
}

# 这些扩展点在无可用事件循环时，async handler 允许被跳过（避免阻塞）
_ALLOW_SKIP_ASYNC = {
    HookPoints.HTTP_BEFORE_REQUEST,
    HookPoints.HTTP_AFTER_REQUEST,
}


class HookRegistry:
    """扩展点注册表：按扩展点收集 handler，按优先级触发。"""

    def __init__(self, framework=None):
        self._framework = framework
        self._hooks = {}                 # point -> list[[priority, name, handler]]
        self._lock = threading.Lock()

    def register(self, point: str, name: str, handler: Callable, priority: int = 50):
        """注册一个扩展点处理器。同名（同一 name）重复注册自动覆盖（去重）。"""
        if not callable(handler):
            raise TypeError(f"扩展点 [{point}] 的 handler 必须可调用: {name}")
        with self._lock:
            entries = self._hooks.setdefault(point, [])
            for entry in entries:
                if entry[1] == name:
                    entry[0], entry[2] = priority, handler
                    break
            else:
                entries.append([priority, name, handler])
            entries.sort(key=lambda e: e[0])

    def unregister(self, point: str, name: str):
        """注销指定 name 的扩展点处理器。"""
        with self._lock:
            entries = self._hooks.get(point)
            if entries:
                self._hooks[point] = [e for e in entries if e[1] != name]

    def clear_plugin(self, plugin_name: str):
        """插件卸载时清除其全部扩展点（按 name 前缀 `plugin_name:` 识别）。"""
        prefix = f"{plugin_name}:"
        with self._lock:
            for point, entries in list(self._hooks.items()):
                kept = [e for e in entries if not e[1].startswith(prefix)]
                if len(kept) != len(entries):
                    self._hooks[point] = kept

    def _sorted(self, point: str) -> List[Tuple[int, str, Callable]]:
        with self._lock:
            return list(self._hooks.get(point, []))

    def has(self, point: str) -> bool:
        """该扩展点是否存在已注册处理器。"""
        return bool(self._hooks.get(point))

    async def trigger_async(self, point: str, *args, **kwargs):
        """
        在事件循环内触发（可安全 await async handler）。
        返回各 handler 的结果列表；调用方可用 `False in results` 判断是否短路。
        """
        results = []
        for _, name, handler in self._sorted(point):
            try:
                if asyncio.iscoroutinefunction(handler):
                    results.append(await handler(*args, **kwargs))
                else:
                    results.append(handler(*args, **kwargs))
            except Exception as e:
                logger.error(f"扩展点 [{point}] 处理器 [{name}] 异常: {e}", exc_info=True)
        return results

    def trigger_sync(self, point: str, *args, **kwargs):
        """
        在 Web / 线程上下文触发：
        - sync handler 直接执行；
        - async handler 经内核 loop fire-and-forget（不阻塞当前线程），
          若该扩展点不允许跳过且无可用 loop，则记告警并跳过。
        """
        loop = getattr(self._framework, 'loop', None) if self._framework else None
        for _, name, handler in self._sorted(point):
            try:
                if asyncio.iscoroutinefunction(handler):
                    if loop is not None and loop.is_running():
                        asyncio.run_coroutine_threadsafe(handler(*args, **kwargs), loop)
                    elif point not in _ALLOW_SKIP_ASYNC:
                        logger.warning(
                            f"扩展点 [{point}] 的 async 处理器 [{name}] 无可用事件循环，已跳过")
                else:
                    handler(*args, **kwargs)
            except Exception as e:
                logger.error(f"扩展点 [{point}] 处理器 [{name}] 异常: {e}")
        return None
