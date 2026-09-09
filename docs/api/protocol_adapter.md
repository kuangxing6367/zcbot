# ProtocolAdapter 协议适配器

`ProtocolAdapter` 是协议适配层的抽象基类，定义了框架与 OneBot 客户端交互的接口。

## 类定义

```python
from abc import ABC, abstractmethod

class ProtocolAdapter(ABC):
    @abstractmethod
    def get_api(self, bot_name: str = None) -> Any:
        """获取 API 调用器"""
        pass

    @abstractmethod
    def get_event_bus(self) -> 'EventBus':
        """获取事件总线"""
        pass

    @abstractmethod
    def register(self, bot_name: str):
        """注册新 bot 实例"""
        pass

    @abstractmethod
    def unregister(self, bot_name: str):
        """注销 bot 实例"""
        pass

    @abstractmethod
    def get_clients(self) -> list:
        """获取已连接客户端"""
        pass
```

## 用法

```python
# 获取服务
adapter = ctx.services.get('protocol_adapter')

# 获取 API
api = adapter.get_api('bot_1')
await api.send_group_msg(group_id=123456, message="hello")

# 获取事件总线
bus = adapter.get_event_bus()

# 检查连接状态
clients = adapter.get_clients()
```

## OneBotAdapter 实现

`core_plugins/onebot_adapter/main.py` 中的 `OneBotAdapter` 继承自 `ProtocolAdapter`：

```python
class OneBotAdapter(ProtocolAdapter):
    def __init__(self, fw):
        self._fw = fw
        self._event_bus = EventBus()
        self._event_bus.on("bot.connected", self._on_bot_connected)
        self._event_bus.on("bot.disconnected", self._on_bot_disconnected)
        self._event_bus.on("message", self._on_message)

    def get_api(self, bot_name=None):
        if bot_name:
            caller = self._callers.get(bot_name)
            return ApiCaller(caller) if caller else None
        return ApiCaller(next(iter(self._callers.values())))

    def get_clients(self):
        return list(self._callers.keys())
```

## EventBus 事件总线

```python
bus = adapter.get_event_bus()

# 订阅
bus.on("message", handler)
bus.on("bot.connected", on_connect)

# 取消订阅
bus.off("message", handler)

# 发布
bus.emit("custom.event", {"data": 123})
```
