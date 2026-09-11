"""
OneBot 11 协议适配器（官方插件）
将 OneBot WS 事件转换为框架内部格式，提供 API 调用能力

本模块是 OneBot 协议专属实现，框架核心（framework/）不包含任何 OneBot 代码：
- WebSocket 接入 / 连接管理 / API 调用通道都在本插件内；
- 标准动作封装见同目录 onebot_api.py；
- 通过 framework.messaging.protocol.ProtocolAdapter 抽象与 ServiceRegistry 接入框架。
"""
import asyncio
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import Optional
from urllib.parse import urlparse, parse_qs

import websockets

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "OneBot 11 适配器",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "OneBot 11 协议适配：WebSocket 连接 + API 调用",
    "priority": 0,
    "official": True,
    # 双进程归属：协议接入基础设施，只在「核心进程」与单进程加载，宿主进程排除
    "process": "core",
}

# 同目录加载标准动作封装（core 插件以合成模块名装载，不能用常规相对导入）
def _load_sibling_onebot_api():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'onebot_api.py')
    spec = importlib.util.spec_from_file_location(
        'core_plugin_onebot_adapter_api', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.OneBotAPI


OneBotAPI = _load_sibling_onebot_api()

# websockets 版本兼容
_WS_VERSION = tuple(int(p) for p in websockets.__version__.split('.')[:2])
_WS_MAJOR = _WS_VERSION[0] if _WS_VERSION else 0

_MAX_CONCURRENT_EVENTS = 64
_MAX_PENDING_EVENTS = 256

_SENT_ACTIONS = ('send_msg', 'send_group_msg', 'send_private_msg')

# 是否记录发送到 OneBot11 的消息内容（register 时从框架配置填充，缺省 True）
_log_sent_message = None


def _should_log_sent_message() -> bool:
    return True if _log_sent_message is None else _log_sent_message


# ── 内部事件格式 ────────────────────────────────────────────────

def normalize_event(raw: dict, bot_name: str) -> Optional[dict]:
    """将 OneBot 11 事件转换为框架内部格式"""
    post_type = raw.get('post_type', '')
    if not post_type:
        return None

    event = {
        'type': post_type,
        'sub_type': raw.get(f'{post_type}_type', ''),
        'message_type': raw.get('message_type', ''),
        'user_id': raw.get('user_id', 0),
        'group_id': raw.get('group_id'),
        'message_id': raw.get('message_id'),
        'message': raw.get('message', ''),
        'raw_message': raw.get('raw_message', ''),
        'sender': raw.get('sender', {}),
        'bot_name': bot_name,
        'adapter': 'onebot',
        'raw': raw,
    }
    return event


# ── WebSocket 连接管理 ─────────────────────────────────────────

def _extract_token(path, headers):
    token = ''
    if headers:
        auth = headers.get('Authorization', '') or headers.get('authorization', '')
        if auth.startswith('Bearer ') or auth.startswith('bearer '):
            token = auth[7:]
    if not token and path:
        try:
            qs = parse_qs(urlparse(path).query)
            if 'access_token' in qs:
                token = qs['access_token'][0]
        except Exception:
            pass
    return token


def _get_request_path(ws, path=None):
    if path is not None:
        return path
    try:
        return ws.request.path
    except (AttributeError, Exception):
        pass
    try:
        return ws.path
    except (AttributeError, Exception):
        pass
    return '/'


def _get_request_headers(ws):
    for attr in ('request_headers', 'headers'):
        try:
            return getattr(ws, attr)
        except (AttributeError, Exception):
            pass
    try:
        return ws.request.headers
    except (AttributeError, Exception):
        pass
    return {}


def _get_header_value(headers, name: str) -> str:
    if not headers:
        return ''
    try:
        v = headers.get(name) or headers.get(name.lower()) or headers.get(name.upper())
        if v:
            return str(v)
    except Exception:
        pass
    lname = name.lower()
    for k, val in headers.items():
        try:
            if str(k).lower() == lname:
                return str(val)
        except Exception:
            continue
    return ''


# ── Bot 连接 ──────────────────────────────────────────────────

class BotConnection:
    """单个 OneBot 连接的 API 调用通道"""

    def __init__(self, name: str):
        self.name = name
        self._ws = None
        self._ws_server = None
        self._pending = {}
        self._responses = {}

    def set_ws(self, ws):
        self._ws = ws

    def set_ws_server(self, ws_server):
        self._ws_server = ws_server

    @property
    def connected(self):
        return self._ws is not None

    async def acall(self, action: str, **params) -> dict:
        if not self.connected or self._ws_server is None:
            return {"status": "failed", "retcode": -1, "msg": f"[{self.name}] WebSocket 未连接"}

        echo = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo}
        event = asyncio.Event()
        self._pending[echo] = event

        try:
            success = await self._ws_server.asend(self.name, payload)
            if not success:
                return {"status": "failed", "retcode": -1, "msg": "发送失败"}

            if action in _SENT_ACTIONS:
                if _should_log_sent_message():
                    msg = params.get('message', '')
                    target = f"群{params.get('group_id')}" if 'group_id' in params else f"私聊{params.get('user_id')}"
                    logger.info(f"[{self.name}] 发送消息 → {target}: {str(msg)[:200]}")

            try:
                await asyncio.wait_for(event.wait(), timeout=10)
            except asyncio.TimeoutError:
                return {"status": "failed", "retcode": -2, "msg": "请求超时"}

            return self._responses.pop(echo, {})
        except Exception as e:
            return {"status": "failed", "retcode": -3, "msg": str(e)}
        finally:
            self._pending.pop(echo, None)

    def call(self, action: str, **params) -> dict:
        loop = getattr(self._ws_server, 'loop', None) if self._ws_server else None
        if loop is None or not loop.is_running():
            return {"status": "failed", "retcode": -1, "msg": "主事件循环未运行"}
        try:
            future = asyncio.run_coroutine_threadsafe(self.acall(action, **params), loop)
            return future.result(timeout=15)
        except Exception as e:
            return {"status": "failed", "retcode": -3, "msg": str(e)}

    def on_response(self, data: dict):
        echo = data.get("echo")
        if echo and echo in self._pending:
            self._responses[echo] = data
            self._pending[echo].set()


# ── API 调用器 ────────────────────────────────────────────────

class ApiCaller:
    """多 OneBot 实例 API 管理器"""

    def __init__(self):
        self._connections = {}
        self.on_message_sent = None

    def register_connection(self, name: str) -> BotConnection:
        dead = [n for n, c in self._connections.items() if not c.connected]
        for n in dead:
            self._connections.pop(n, None)
        conn = BotConnection(name)
        conn._caller = self
        self._connections[name] = conn
        return conn

    def get_connection(self, name: str = None) -> BotConnection:
        if not self._connections:
            return None
        if name is not None and name in self._connections:
            return self._connections.get(name)
        for conn in self._connections.values():
            if conn.connected:
                return conn
        return next(iter(self._connections.values()))

    async def acall(self, action: str, bot: str = None, **params) -> dict:
        conn = self.get_connection(bot)
        if conn is None:
            return {"status": "failed", "retcode": -1, "msg": "无可用 OneBot 连接"}
        return await conn.acall(action, **params)

    def call(self, action: str, bot: str = None, **params) -> dict:
        conn = self.get_connection(bot)
        if conn is None:
            return {"status": "failed", "retcode": -1, "msg": "无可用 OneBot 连接"}
        return conn.call(action, **params)

    def broadcast(self, action: str, **params) -> dict:
        return {n: c.call(action, **params) for n, c in self._connections.items()}

    def all_connections(self) -> dict:
        return self._connections


# ── WebSocket 服务端 ──────────────────────────────────────────

class OneBotWebSocketServer:
    """OneBot 11 反向 WebSocket 服务端（作为适配器的一部分）"""

    def __init__(self, config: dict, on_event_callback, api_caller: ApiCaller):
        self.host = config.get('listen_host', '0.0.0.0')
        self.port = config.get('listen_port', 6830)
        self.access_token = config.get('access_token', '')
        self.on_event_callback = on_event_callback
        self.api_caller = api_caller

        self._server = None
        self._server_task = None
        self._loop = None
        self._running = False
        self._connections = {}
        self._conn_counter = 0
        self._lock = threading.Lock()
        self._dispatch_semaphore = None
        self._dispatch_pending = 0
        self._dispatch_dropped = 0
        self._conn_chains = {}

    @property
    def loop(self):
        return self._loop

    def start(self):
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._dispatch_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_EVENTS)
        self._server_task = asyncio.create_task(self._serve(), name="ws-server")

    async def _serve(self):
        serve_kwargs = {'ping_interval': 30, 'ping_timeout': 10}
        if _WS_MAJOR >= 13:
            serve_kwargs['process_request'] = self._process_request_v13
        elif _WS_MAJOR >= 10:
            serve_kwargs['process_request'] = self._process_request_v10

        try:
            self._server = await websockets.serve(
                self._handle_connection, self.host, self.port, **serve_kwargs)
        except Exception as e:
            logger.error(f"WebSocket 服务启动失败: {e}")
            self._running = False
            return
        logger.info(f"OneBot WebSocket 服务端已启动: ws://{self.host}:{self.port}")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    async def _process_request_v13(self, connection, request):
        if self.access_token:
            token = _extract_token(request.path, request.headers)
            if token != self.access_token:
                try:
                    return connection.respond(403, "access_token error\n")
                except Exception:
                    return None
        return None

    async def _process_request_v10(self, path, request_headers):
        if self.access_token:
            token = _extract_token(path, request_headers)
            if token != self.access_token:
                from http import HTTPStatus
                return (HTTPStatus.FORBIDDEN, [], b"access_token error\n")
        return None

    async def _handle_connection(self, ws, path=None):
        req_path = _get_request_path(ws, path)
        headers = _get_request_headers(ws)

        if self.access_token:
            token = _extract_token(req_path, headers)
            if token != self.access_token:
                try:
                    await ws.close(code=4001, reason="access_token error")
                except Exception:
                    pass
                return

        self_id = _get_header_value(headers, 'X-Self-ID')
        if self_id:
            bot_name = self_id
        else:
            with self._lock:
                self._conn_counter += 1
                bot_name = f"bot_{self._conn_counter}"
        with self._lock:
            self._connections[bot_name] = ws

        logger.info(f"[{bot_name}] OneBot 客户端已连接")
        conn = self.api_caller.register_connection(bot_name)
        conn.set_ws(ws)
        conn.set_ws_server(self)

        try:
            async for raw_message in ws:
                logger.debug(f"[{bot_name}] 收到原始数据 {len(raw_message)} 字节: {str(raw_message)[:300]}")
                try:
                    data = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"[{bot_name}] JSON 解析失败: {e}")
                    continue

                if "echo" in data:
                    conn.on_response(data)
                    continue

                post_type = data.get("post_type")
                if post_type:
                    logger.debug(f"[{bot_name}] 收到事件 post_type={post_type} sub_type={data.get(f'{post_type}_type','')}")
                    if self._dispatch_pending >= _MAX_PENDING_EVENTS:
                        self._dispatch_dropped += 1
                        logger.warning(f"[{bot_name}] 事件丢弃 pending={self._dispatch_pending} dropped={self._dispatch_dropped}")
                        continue
                    self._dispatch_pending += 1
                    prev = self._conn_chains.get(bot_name)
                    task = asyncio.create_task(self._dispatch_ordered(prev, data, bot_name))
                    self._conn_chains[bot_name] = task
                    task.add_done_callback(lambda t: setattr(self, '_dispatch_pending', self._dispatch_pending - 1))
                else:
                    logger.debug(f"[{bot_name}] 收到无 post_type 的消息: {str(data)[:200]}")

        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{bot_name}] 连接处理异常: {e}")
        finally:
            with self._lock:
                if self._connections.get(bot_name) is ws:
                    self._connections.pop(bot_name, None)
            self._conn_chains.pop(bot_name, None)
            conn.set_ws(None)
            logger.info(f"[{bot_name}] OneBot 客户端已断开")

    async def _dispatch_ordered(self, prev, data: dict, bot_name: str):
        if prev is not None and not prev.done():
            try:
                await prev
            except Exception:
                pass
        await self._dispatch(data, bot_name)

    async def _dispatch(self, data: dict, bot_name: str):
        try:
            async with self._dispatch_semaphore:
                if asyncio.iscoroutinefunction(self.on_event_callback):
                    await self.on_event_callback(data, bot_name)
                else:
                    await asyncio.to_thread(self.on_event_callback, data, bot_name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{bot_name}] 消息处理异常: {e}")

    async def asend(self, bot_name: str, data: dict) -> bool:
        with self._lock:
            ws = self._connections.get(bot_name)
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            return False

    def send(self, bot_name: str, data: dict) -> bool:
        if self._loop is None or not self._loop.is_running():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self.asend(bot_name, data), self._loop)
            return future.result(timeout=5)
        except Exception:
            return False

    def get_connected_bots(self) -> list:
        with self._lock:
            return list(self._connections.keys())

    async def stop(self):
        self._running = False
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass


# ── 适配器主类 ────────────────────────────────────────────────

class OneBotAdapter(ProtocolAdapter):
    """OneBot 11 协议适配器"""

    def __init__(self, framework):
        self.framework = framework
        self.config = framework.config.get('onebot', {})
        self.api_caller = ApiCaller()
        self.api_caller.on_message_sent = self._on_message_sent
        self.ws_server = OneBotWebSocketServer(
            self.config, self._on_raw_event, self.api_caller)
        self._onebot_api = OneBotAPI(self.api_caller)

    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        return normalize_event(raw_event, bot_name)

    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        return await self.api_caller.acall(action, bot=bot, **params)

    async def send_text(self, text: str, *, user_id=None, group_id=None,
                        source: str = None) -> dict:
        """协议中立文本发送：翻译成 OneBot 的群/私聊动作"""
        if group_id:
            return await self.api_caller.acall(
                'send_group_msg', group_id=group_id, message=text, bot=source)
        return await self.api_caller.acall(
            'send_private_msg', user_id=user_id, message=text, bot=source)

    def get_connected_bots(self) -> list:
        return self.ws_server.get_connected_bots()

    def start(self):
        self.ws_server.start()

    async def stop(self):
        await self.ws_server.stop()

    async def _on_raw_event(self, data: dict, bot_name: str):
        """收到原始 OneBot 事件，转换后交给框架"""
        event = normalize_event(data, bot_name)
        if event is None:
            return
        await self.framework.dispatch_event(event)

    def _on_message_sent(self, bot_name, action, params, resp):
        """消息发送后的生命周期钩子"""
        try:
            self.framework.event_bus.emit('after_message_sent', {
                'bot': bot_name, 'action': action, 'params': params, 'response': resp,
            })
        except Exception:
            pass


# ── 插件注册入口 ──────────────────────────────────────────────

_adapter_instance = None


def register(ctx):
    """注册 OneBot 适配器为官方插件"""
    global _adapter_instance, _log_sent_message
    fw = ctx._framework

    # 检查配置是否启用
    onebot_cfg = fw.config.get('onebot', {})
    if onebot_cfg.get('enabled') is False:
        ctx.log("OneBot 适配器已禁用 (onebot.enabled: false)")
        return

    # 日志开关：是否记录发送到 OneBot11 的消息内容
    _log_sent_message = fw.config.get('log', {}).get('log_sent_message', True)

    _adapter_instance = OneBotAdapter(fw)

    # 注册为协议适配器服务
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance.api_caller)
    fw.services.register('onebot_api', _adapter_instance._onebot_api)
    fw.services.register('ws_server', _adapter_instance.ws_server)

    # OneBot 专属安全提示（框架核心不再假设具体协议，故由本适配器自行提示）
    web_host = fw.config.get('web', {}).get('host', '0.0.0.0')
    if web_host in ('0.0.0.0', '::') and not onebot_cfg.get('access_token', ''):
        logger.warning(
            "⚠ 安全提示: Web 面板监听 0.0.0.0 且 OneBot access_token 为空，"
            "公网部署存在被接管风险。请设置 onebot.access_token，并将 web.host 改为 127.0.0.1。"
        )

    # 启动 WebSocket 服务
    _adapter_instance.start()

    ctx.log(f"OneBot 适配器已启动 (ws://{_adapter_instance.config.get('listen_host', '0.0.0.0')}:{_adapter_instance.config.get('listen_port', 6830)})")


def unregister():
    """卸载时停止"""
    global _adapter_instance
    if _adapter_instance:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_adapter_instance.stop())
        _adapter_instance = None
