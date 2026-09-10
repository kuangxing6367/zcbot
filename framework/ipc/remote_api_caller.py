# -*- coding: utf-8 -*-
"""
RemoteApiCaller —— 宿主侧协议 API 调用代理

插件发消息（ctx.send_msg / ctx.api / ctx.onebot.*）最终走到 services['api_caller']。
宿主侧该服务指向此代理：把 action 经 IPC 转发到核心进程，由核心的
onebot_adapter 经 WebSocket 发出并回传结果。插件侧零改动。
"""
import logging

logger = logging.getLogger('zcbot')


class _NullConnection:
    """占位连接：宿主进程不存在真实协议连接实体"""

    def set_ws(self, ws):
        pass

    def set_ws_server(self, srv):
        pass

    def send(self, *a, **k):
        return {}

    async def asend(self, *a, **k):
        return {}


class RemoteApiCaller:
    """把 API 调用转发到核心进程执行"""

    def __init__(self, client):
        self._client = client

    def call(self, action, bot=None, **params):
        """同步调用（阻塞当前线程等核心返回）"""
        return self._client.call('api.call', {
            'action': action, 'bot': bot, 'params': params,
        })

    async def acall(self, action, bot=None, **params):
        """异步调用（推荐 async handler 使用）"""
        return await self._client.acall('api.call', {
            'action': action, 'bot': bot, 'params': params,
        })

    def register_connection(self, name):
        """宿主无连接实体，返回占位对象"""
        return _NullConnection()
