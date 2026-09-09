"""
协议适配器接口 + 服务注册表
框架核心通过此模块定义服务契约，官方插件实现并注册
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger('zcbot')


class ProtocolAdapter(ABC):
    """协议适配器抽象基类（OneBot/HTTP/自定义协议等）"""

    @abstractmethod
    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        """
        将原始协议事件转换为框架内部事件格式
        :return: 内部事件 dict，或 None 表示丢弃
        """
        ...

    @abstractmethod
    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        """调用协议 API"""
        ...

    @abstractmethod
    def get_connected_bots(self) -> list:
        """返回已连接的 bot 列表"""
        ...

    @abstractmethod
    def start(self):
        """启动适配器"""
        ...

    @abstractmethod
    async def stop(self):
        """停止适配器"""
        ...


class ServiceRegistry:
    """
    服务注册表：官方插件注册自身为核心能力
    核心框架通过 get() 获取服务，不直接 import 官方插件代码
    """

    def __init__(self):
        self._services: Dict[str, Any] = {}

    def register(self, name: str, service: Any):
        """注册服务（官方插件调用）"""
        if name in self._services:
            logger.warning(f"服务 [{name}] 已注册，将被覆盖")
        self._services[name] = service
        logger.debug(f"服务已注册: [{name}]")

    def get(self, name: str, default=None):
        """获取服务（核心框架/插件调用）"""
        return self._services.get(name, default)

    def has(self, name: str) -> bool:
        """检查服务是否已注册"""
        return name in self._services

    def remove(self, name: str):
        """移除服务"""
        self._services.pop(name, None)

    def all(self) -> dict:
        """返回所有已注册服务"""
        return dict(self._services)
