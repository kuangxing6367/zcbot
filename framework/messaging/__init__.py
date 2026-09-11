"""
消息事件通讯模块

集中框架的消息 / 事件通讯能力：
- event:        消息事件模型与解析
- event_bus:    事件发布订阅总线
- protocol:     接入端 / 协议抽象（ProtocolAdapter / ServiceRegistry / ActionProxy）
- router:       消息路由分发器
"""

from .event import Event
from .event_bus import EventBus
from .protocol import ProtocolAdapter, ServiceRegistry, ActionProxy
from .router import MessageRouter

__all__ = [
    'Event',
    'EventBus',
    'ProtocolAdapter',
    'ServiceRegistry',
    'ActionProxy',
    'MessageRouter',
]
