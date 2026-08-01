"""
OneBot 11 API 调用器
支持多 OneBot 实例连接，通过 bot 参数指定目标

异步优先：
- acall()   : 异步调用，不阻塞事件循环（插件 async handler 应使用）
- call()    : 同步桥接，供 executor 线程 / Web 线程 / 旧插件使用
              内部通过 run_coroutine_threadsafe 转发到主事件循环
"""
import asyncio
import logging
import threading
import uuid
import os
import yaml

logger = logging.getLogger('zcbot')

# 缓存配置，避免每次调用都读文件
_log_sent_message = None
_SENT_ACTIONS = ('send_msg', 'send_group_msg', 'send_private_msg')


def _should_log_sent_message():
    """读取配置：是否记录发送到 OneBot11 的消息内容"""
    global _log_sent_message
    if _log_sent_message is None:
        try:
            config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.yaml')
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            _log_sent_message = config.get('log', {}).get('log_sent_message', True)
        except Exception:
            _log_sent_message = True
    return _log_sent_message


class BotConnection:
    """单个 OneBot 连接的 API 调用通道"""

    def __init__(self, name: str):
        self.name = name
        self._ws = None       # websocket 连接对象（asyncio 侧）
        self._ws_server = None  # WebSocketServer 引用（用于跨线程/跨协程发送）
        self._pending = {}       # echo -> asyncio.Event
        self._responses = {}     # echo -> response

    def set_ws(self, ws):
        """设置底层 websocket 连接对象"""
        self._ws = ws

    def set_ws_server(self, ws_server):
        """设置 WebSocketServer 引用，用于发送数据"""
        self._ws_server = ws_server

    @property
    def connected(self):
        return self._ws is not None

    async def acall(self, action: str, **params) -> dict:
        """
        异步调用 OneBot API（不阻塞事件循环）
        超时 10 秒，超时返回失败
        """
        if not self.connected or self._ws_server is None:
            logger.error(f"[{self.name}] API调用失败: WebSocket 未连接, action={action}")
            return {"status": "failed", "retcode": -1, "msg": f"[{self.name}] WebSocket 未连接"}

        echo = str(uuid.uuid4())
        payload = {
            "action": action,
            "params": params,
            "echo": echo
        }

        event = asyncio.Event()
        self._pending[echo] = event

        try:
            # 通过 WebSocketServer 在事件循环内发送
            success = await self._ws_server.asend(self.name, payload)
            if not success:
                return {"status": "failed", "retcode": -1, "msg": "发送失败"}

            # 记录发送到 OneBot11 的消息内容（send_msg 等关键操作）
            if action in _SENT_ACTIONS:
                if _should_log_sent_message():
                    msg_content = params.get('message', '')
                    target = f"群{params.get('group_id')}" if 'group_id' in params else f"私聊{params.get('user_id')}"
                    logger.info(f"[{self.name}] 发送消息 → {target}: {str(msg_content)[:200]}")
            else:
                logger.debug(f"[{self.name}] API请求: {action} {params}")

            try:
                await asyncio.wait_for(event.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.name}] API调用超时: {action}")
                return {"status": "failed", "retcode": -2, "msg": "请求超时"}

            resp = self._responses.pop(echo, {})
            return resp

        except Exception as e:
            logger.error(f"[{self.name}] API调用异常: {action} - {e}")
            return {"status": "failed", "retcode": -3, "msg": str(e)}
        finally:
            self._pending.pop(echo, None)

    def call(self, action: str, **params) -> dict:
        """
        同步调用 OneBot API（同步桥接）
        供 executor 线程 / Web 线程 / 旧插件使用，内部转发到主事件循环执行
        """
        loop = getattr(self._ws_server, 'loop', None) if self._ws_server else None
        if loop is None or not loop.is_running():
            logger.error(f"[{self.name}] API调用失败: 主事件循环未运行, action={action}")
            return {"status": "failed", "retcode": -1, "msg": "主事件循环未运行"}

        try:
            future = asyncio.run_coroutine_threadsafe(self.acall(action, **params), loop)
            return future.result(timeout=15)
        except asyncio.TimeoutError:
            logger.error(f"[{self.name}] API调用同步桥接超时: {action}")
            return {"status": "failed", "retcode": -2, "msg": "请求超时"}
        except Exception as e:
            logger.error(f"[{self.name}] API调用同步桥接异常: {action} - {e}")
            return {"status": "failed", "retcode": -3, "msg": str(e)}

    def on_response(self, data: dict):
        """处理 API 响应（在事件循环线程内调用）"""
        echo = data.get("echo")
        if echo and echo in self._pending:
            self._responses[echo] = data
            self._pending[echo].set()


class ApiCaller:
    """多 OneBot 实例 API 管理器"""

    def __init__(self):
        self._connections = {}  # name -> BotConnection

    def register_connection(self, name: str) -> BotConnection:
        """注册一个 OneBot 连接通道"""
        conn = BotConnection(name)
        self._connections[name] = conn
        return conn

    def get_connection(self, name: str = None) -> BotConnection:
        """
        获取指定连接，name=None 返回默认（第一个）连接
        """
        if not self._connections:
            return None

        if name is None or name not in self._connections:
            # 返回第一个可用连接
            return next(iter(self._connections.values()))
        return self._connections.get(name)

    async def acall(self, action: str, bot: str = None, **params) -> dict:
        """异步调用 OneBot 11 API"""
        conn = self.get_connection(bot)
        if conn is None:
            logger.error(f"API调用失败: 无可用连接, action={action}, bot={bot}")
            return {"status": "failed", "retcode": -1, "msg": "无可用 OneBot 连接"}
        return await conn.acall(action, **params)

    def call(self, action: str, bot: str = None, **params) -> dict:
        """
        同步调用 OneBot 11 API（同步桥接）
        :param action: 动作名
        :param bot: 指定 OneBot 实例名称（None=默认）
        :param params: 参数
        """
        conn = self.get_connection(bot)
        if conn is None:
            logger.error(f"API调用失败: 无可用连接, action={action}, bot={bot}")
            return {"status": "failed", "retcode": -1, "msg": "无可用 OneBot 连接"}

        return conn.call(action, **params)

    def broadcast(self, action: str, **params) -> dict:
        """向所有 OneBot 实例广播调用（同步）"""
        results = {}
        for name, conn in self._connections.items():
            results[name] = conn.call(action, **params)
        return results

    def all_connections(self) -> dict:
        """返回所有连接"""
        return self._connections
