"""
事件总线模块
支持 ctx.on(event_name, handler) 和 ctx.emit(event_name, payload)

异步模型：
- aemit() 异步发布，订阅 handler 支持 async def（直接 await）和普通 def（转线程）
- emit()  同步桥接，供旧插件/非 loop 线程使用
- 订阅/退订线程安全（插件可能在 executor 线程中注册）

性能：订阅低频、发布高频，采用"写时复制快照"——订阅变更时失效快照，
aemit 热路径无锁读不可变 tuple；无订阅者时零锁零拷贝直接返回。
"""
import asyncio
import logging
import threading
from typing import Callable, Dict, List

logger = logging.getLogger('zcbot')


class EventBus:
    """轻量级事件总线"""

    def __init__(self):
        self._subscribers: Dict[str, List[dict]] = {}  # event_name -> [{plugin_name, handler}]
        self._snapshots: Dict[str, tuple] = {}         # event_name -> ((plugin_name, handler), ...)，写时复制
        self._lock = threading.Lock()

    def _invalidate(self, event_name: str):
        """失效指定事件快照（订阅/退订后调用）"""
        self._snapshots.pop(event_name, None)

    def _snapshot(self, event_name: str) -> tuple:
        """读取订阅者快照（无锁热路径；变更方负责失效重建）"""
        snap = self._snapshots.get(event_name)
        if snap is not None:
            return snap
        with self._lock:
            subs = self._subscribers.get(event_name)
            if not subs:
                self._snapshots[event_name] = ()
                return ()
            snap = tuple((s['plugin_name'], s['handler']) for s in subs)
            self._snapshots[event_name] = snap
            return snap

    def subscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """订阅事件（同一插件同一 handler 重复订阅自动去重，防止心跳重注册后重复触发）"""
        with self._lock:
            subs = self._subscribers.setdefault(event_name, [])
            for s in subs:
                if s['plugin_name'] == plugin_name and s['handler'] == handler:
                    return  # 已订阅，去重
            subs.append({
                'plugin_name': plugin_name,
                'handler': handler
            })
            self._invalidate(event_name)
        logger.debug(f"事件订阅: [{plugin_name}] → {event_name}")

    def once(self, event_name: str, plugin_name: str, handler: Callable):
        """
        一次性订阅：事件首次触发后自动退订（无论 handler 返回什么）。
        适用于"只等一次"的场景（如等待某个异步结果/状态变更）。
        """
        async def _wrapper(payload):
            try:
                if asyncio.iscoroutinefunction(handler):
                    return await handler(payload)
                return handler(payload)
            finally:
                self.unsubscribe(event_name, plugin_name, _wrapper)
        self.subscribe(event_name, plugin_name, _wrapper)

    def unsubscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """按 handler 精确退订某事件的订阅。"""
        with self._lock:
            subs = self._subscribers.get(event_name)
            if not subs:
                return
            kept = [s for s in subs
                    if not (s['plugin_name'] == plugin_name and s['handler'] == handler)]
            if len(kept) == len(subs):
                return
            if kept:
                self._subscribers[event_name] = kept
            else:
                del self._subscribers[event_name]
            self._invalidate(event_name)

    def unsubscribe_plugin(self, plugin_name: str):
        """移除某插件的所有订阅"""
        with self._lock:
            for event_name in list(self._subscribers.keys()):
                self._subscribers[event_name] = [
                    s for s in self._subscribers[event_name]
                    if s['plugin_name'] != plugin_name
                ]
                if not self._subscribers[event_name]:
                    del self._subscribers[event_name]
                self._invalidate(event_name)

    def subscribers_of(self, event_name: str) -> int:
        """查询某事件的当前订阅者数量（调试/监控用）。"""
        return len(self._snapshot(event_name))

    def stats(self) -> dict:
        """返回事件总线订阅统计：{event_name: 订阅数}（调试/监控用）。"""
        with self._lock:
            return {name: len(subs) for name, subs in self._subscribers.items()}

    async def _invoke_one(self, event_name: str, plugin_name: str, handler: Callable, payload):
        """执行单个订阅者（async 直接 await，sync 转线程），异常仅记录不抛出"""
        try:
            if asyncio.iscoroutinefunction(handler):
                return await handler(payload)
            return await asyncio.to_thread(handler, payload)
        except Exception as e:
            logger.error(f"事件处理异常: [{plugin_name}] {event_name} - {e}")
            return None

    async def aemit(self, event_name: str, payload: dict = None, parallel: bool = False) -> bool:
        """
        异步发布事件，通知所有订阅者
        返回 bool：任一订阅 handler 返回 True 视为"已处理"（调用方可用于路由终止判断）
        向后兼容：现有订阅者返回 None/False 不影响

        :param parallel: True 时并发执行各订阅者（asyncio.gather），
            互不依赖的监听型事件可显著降低总耗时；默认 False 保持串行保序语义。
        """
        subscribers = self._snapshot(event_name)
        if not subscribers:
            return False

        payload = payload or {}
        logger.debug(f"事件触发: {event_name} → {len(subscribers)} 个订阅者")

        if parallel and len(subscribers) > 1:
            results = await asyncio.gather(*[
                self._invoke_one(event_name, plugin_name, handler, payload)
                for plugin_name, handler in subscribers
            ])
            return any(r is True for r in results)

        handled = False
        for plugin_name, handler in subscribers:
            result = await self._invoke_one(event_name, plugin_name, handler, payload)
            if result is True:
                handled = True
        return handled

    def emit(self, event_name: str, payload: dict = None):
        """
        同步桥接发布事件
        - 主事件循环内调用 → fire-and-forget 调度
        - 其他线程调用 → 用临时事件循环执行
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            try:
                loop.create_task(self.aemit(event_name, payload))
                return
            except RuntimeError:
                pass
        asyncio.run(self.aemit(event_name, payload))
