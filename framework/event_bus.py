"""
事件总线模块
支持 ctx.on(event_name, handler) 和 ctx.emit(event_name, payload)
"""
import logging
from typing import Callable, Dict, List

logger = logging.getLogger('zcbot')


class EventBus:
    """轻量级事件总线"""

    def __init__(self):
        self._subscribers: Dict[str, List[dict]] = {}  # event_name -> [{plugin_name, handler}]

    def subscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """订阅事件"""
        if event_name not in self._subscribers:
            self._subscribers[event_name] = []
        self._subscribers[event_name].append({
            'plugin_name': plugin_name,
            'handler': handler
        })
        logger.debug(f"事件订阅: [{plugin_name}] → {event_name}")

    def unsubscribe_plugin(self, plugin_name: str):
        """移除某插件的所有订阅"""
        for event_name in list(self._subscribers.keys()):
            self._subscribers[event_name] = [
                s for s in self._subscribers[event_name]
                if s['plugin_name'] != plugin_name
            ]
            if not self._subscribers[event_name]:
                del self._subscribers[event_name]

    def emit(self, event_name: str, payload: dict = None):
        """发布事件，通知所有订阅者"""
        subscribers = self._subscribers.get(event_name, [])
        if not subscribers:
            return

        payload = payload or {}
        logger.debug(f"事件触发: {event_name} → {len(subscribers)} 个订阅者")

        for sub in subscribers:
            try:
                sub['handler'](payload)
            except Exception as e:
                logger.error(f"事件处理异常: [{sub['plugin_name']}] {event_name} - {e}")