# -*- coding: utf-8 -*-
"""
IpcAdapter —— 宿主侧的协议适配器（进程2）

宿主进程不接外部协议，而是通过 IPC 与核心进程对接：
- 事件注入：核心推送归一化事件 → 本适配器把它调度进宿主 framework.dispatch_event
  （走原有 raw_handlers → router → event_bus，session 的 wait_for 天然跨进程可用）。
- 发消息：插件 ctx.send_msg / ctx.actions / ctx.onebot.* 走 services['api_caller'] = 本适配器，
  经 RemoteApiCaller / api.send_text 转发到核心接入端发出。

仿 http_inject 的注册方式：注册为 services['protocol_adapter'] + ['api_caller']。
"""
import asyncio
import logging

from framework.messaging.protocol import ProtocolAdapter
from framework.ipc.remote_api_caller import RemoteApiCaller, _NullConnection

logger = logging.getLogger('zcbot')


class IpcAdapter(ProtocolAdapter):
    """宿主侧 IPC 协议适配器"""

    adapter_id = 'ipc'

    def __init__(self, framework, client):
        self.framework = framework
        self._client = client
        self._api = RemoteApiCaller(client)
        self._loop = None
        self._inject_bound = False

    def get_connection_info(self) -> dict:
        """连接自描述：宿主侧经 IPC 回连核心，无可编辑监听端点"""
        return {
            'id': 'ipc',
            'name': 'IPC（宿主 ↔ 核心）',
            'config_section': '',
            'fields': [],
            'guide': '双进程模式：宿主进程经 IPC 使用核心进程的接入端收发消息。',
        }

    # ---- 事件注入 ----

    def bind_event_loop(self, loop):
        """绑定宿主事件循环并注册事件注入（核心→宿主）"""
        self._loop = loop
        if self._inject_bound:
            return

        def _on_event(payload):
            if self._loop is None or not self._loop.is_running():
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self.framework.dispatch_event(payload), self._loop)
            except Exception as e:
                logger.error(f"IPC 事件注入失败: {e}")

        self._client.on('event', _on_event)
        self._inject_bound = True

    # ---- ProtocolAdapter 契约 ----

    async def handle_event(self, raw_event: dict, bot_name: str):
        # 事件已在核心进程归一化，原样透传（由 bind_event_loop 注入）
        return raw_event

    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        return await self._api.acall(action, bot=bot, **params)

    def get_connected_bots(self) -> list:
        try:
            return self._client.call('bots.list')
        except Exception:
            return []

    def start(self):
        # 宿主无外部协议实体，无需启动
        pass

    async def stop(self):
        pass

    # ---- api_caller / ctx.actions 使用的接口 ----

    def call(self, action: str, bot=None, **params):
        return self._api.call(action, bot=bot, **params)

    async def acall(self, action: str, bot=None, **params):
        return await self._api.acall(action, bot=bot, **params)

    async def send_text(self, text, *, user_id=None, group_id=None, source=None):
        """协议中立文本发送：经 IPC 转发到核心进程的接入端发出"""
        try:
            return await self._client.acall('api.send_text', {
                'text': text, 'user_id': user_id,
                'group_id': group_id, 'source': source,
            })
        except Exception:
            # 旧核心无 api.send_text 时回落通用 send_msg 动作
            if group_id:
                return await self._api.acall('send_msg', group_id=group_id,
                                             message=text, bot=source)
            return await self._api.acall('send_msg', user_id=user_id,
                                         message=text, bot=source)

    def register_connection(self, name):
        return _NullConnection()
