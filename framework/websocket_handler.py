"""
OneBot 11 WebSocket 服务端
框架作为服务端监听端口，OneBot 客户端（go-cqhttp/NapCat 等）配置反向 WS 主动连接
支持多个 OneBot 客户端同时连接
向下兼容 websockets 10.x ~ 16.x+

异步模型：
- 服务端运行在主事件循环中（不再单独起线程）
- 收到的每条事件通过有界信号量派发为 asyncio 任务，天然不阻塞事件循环
"""
import asyncio
import json
import logging
import threading
from urllib.parse import urlparse, parse_qs

import websockets

from framework.log_broker import log_broker

logger = logging.getLogger('zcbot')

# 检测 websockets 大版本
_WS_VERSION = tuple(int(p) for p in websockets.__version__.split('.')[:2])
_WS_MAJOR = _WS_VERSION[0] if _WS_VERSION else 0

# 事件并发处理上限（超出后排队等待，防止消息洪峰打爆任务数）
_MAX_CONCURRENT_EVENTS = 64


def _get_request_path(ws, path=None):
    """兼容多版本：获取请求路径"""
    if path is not None:
        return path
    # websockets 13.x+: ws.request.path
    try:
        return ws.request.path
    except (AttributeError, Exception):
        pass
    # websockets 10-12.x: ws.path
    try:
        return ws.path
    except (AttributeError, Exception):
        pass
    return '/'


def _get_request_headers(ws):
    """兼容多版本：获取请求头 dict"""
    # websockets 10-12.x: ws.request_headers
    try:
        return ws.request_headers
    except (AttributeError, Exception):
        pass
    # websockets 13.x+: ws.request.headers
    try:
        return ws.request.headers
    except (AttributeError, Exception):
        pass
    # 某些版本: ws.headers
    try:
        return ws.headers
    except (AttributeError, Exception):
        pass
    return {}


def _extract_token(path, headers):
    """从请求路径或 headers 中提取 access_token"""
    token = ''

    # 方式1: Authorization header (Bearer xxx)
    if headers:
        auth = headers.get('Authorization', '') or headers.get('authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
        elif auth.startswith('bearer '):
            token = auth[7:]

    # 方式2: URL query 参数 access_token=xxx
    if not token and path:
        try:
            parsed = urlparse(path)
            qs = parse_qs(parsed.query)
            if 'access_token' in qs:
                token = qs['access_token'][0]
        except Exception:
            pass

    return token


class WebSocketServer:
    """OneBot 11 反向 WebSocket 服务端"""

    def __init__(self, host: str, port: int, access_token: str,
                 on_connect_callback, on_disconnect_callback,
                 on_message_callback, api_caller):
        self.host = host
        self.port = port
        self.access_token = access_token
        self.on_connect_callback = on_connect_callback
        self.on_disconnect_callback = on_disconnect_callback
        self.on_message_callback = on_message_callback
        self.api_caller = api_caller

        self._server = None
        self._server_task = None
        self._loop = None
        self._running = False
        self._connections = {}  # bot_name -> websocket connection
        self._conn_counter = 0
        self._lock = threading.Lock()
        self._dispatch_semaphore = None

    # ── 生命周期（运行在主事件循环中）──────────────────────────────

    @property
    def loop(self):
        """主事件循环引用（供同步桥接使用）"""
        return self._loop

    def start_async(self):
        """在主事件循环中启动服务端（需在 asyncio.run 内调用）"""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._dispatch_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_EVENTS)
        self._server_task = asyncio.create_task(self._serve(), name="ws-server")
        return self._server_task

    async def _serve(self):
        """启动 WebSocket 服务"""
        serve_kwargs = {
            'ping_interval': 30,
            'ping_timeout': 10,
        }

        # websockets 13+ 支持 process_request(connection, request)
        # websockets 10-12 支持 process_request(path, request_headers)
        if _WS_MAJOR >= 13:
            serve_kwargs['process_request'] = self._process_request_v13
        elif _WS_MAJOR >= 10:
            serve_kwargs['process_request'] = self._process_request_v10

        try:
            self._server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                **serve_kwargs,
            )
        except Exception as e:
            logger.error(f"WebSocket 服务启动失败: {e}", exc_info=True)
            self._running = False
            return
        logger.info(f"WebSocket 服务端已启动: ws://{self.host}:{self.port} (websockets {websockets.__version__})")
        try:
            await asyncio.Future()  # 一直运行直到被取消
        except asyncio.CancelledError:
            logger.info("WebSocket 服务端已停止")
            raise

    async def _process_request_v13(self, connection, request):
        """websockets 13.x+ 握手阶段验证 token"""
        if self.access_token:
            token = _extract_token(request.path, request.headers)
            if token != self.access_token:
                logger.warning(f"连接被拒绝: access_token 错误 (path={request.path})")
                try:
                    return connection.respond(403, "access_token error\n")
                except Exception:
                    # 某些版本可能不支持 respond
                    return None  # 让 handler 中再次验证
        return None

    async def _process_request_v10(self, path, request_headers):
        """websockets 10.x-12.x 握手阶段验证 token"""
        if self.access_token:
            token = _extract_token(path, request_headers)
            if token != self.access_token:
                logger.warning(f"连接被拒绝: access_token 错误 (path={path})")
                from http import HTTPStatus
                return (HTTPStatus.FORBIDDEN, [], b"access_token error\n")
        return None

    async def _handle_connection(self, ws, path=None):
        """
        处理新的 OneBot 客户端连接
        兼容 websockets 10.x (ws, path) 和 13.x+ (ws) 签名
        """
        req_path = _get_request_path(ws, path)
        headers = _get_request_headers(ws)

        # ---- 后备 token 验证（process_request 不支持的版本或失败时）----
        if self.access_token:
            token = _extract_token(req_path, headers)
            if token != self.access_token:
                logger.warning(f"连接被拒绝: access_token 错误 (path={req_path})")
                try:
                    await ws.close(code=4001, reason="access_token error")
                except Exception:
                    pass
                return

        # ---- 分配 bot_name ----
        with self._lock:
            self._conn_counter += 1
            bot_name = f"bot_{self._conn_counter}"
            self._connections[bot_name] = ws

        peer = ws.remote_address if hasattr(ws, 'remote_address') else 'unknown'
        logger.info(f"[{bot_name}] OneBot 客户端已连接: {peer} (path={req_path})")
        log_broker.log_connection(bot_name, 'connect', {
            'peer': str(peer),
            'path': req_path,
        })

        # 触发连接回调
        try:
            cb = self.on_connect_callback
            if asyncio.iscoroutinefunction(cb):
                await cb(bot_name, ws)
            else:
                await asyncio.to_thread(cb, bot_name, ws)
        except Exception as e:
            logger.error(f"[{bot_name}] on_connect 回调异常: {e}")

        # ---- 消息循环 ----
        try:
            async for raw_message in ws:
                try:
                    data = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"[{bot_name}] 收到非JSON消息: {str(raw_message)[:100]}")
                    continue

                # 判断是否为 API 响应（有 echo 字段）— 同步处理，不阻塞
                if "echo" in data:
                    conn = self.api_caller.get_connection(bot_name)
                    if conn:
                        conn.on_response(data)
                    continue

                # 判断是否为事件上报 — 有界并发派发，不阻塞事件循环
                post_type = data.get("post_type")
                if post_type:
                    asyncio.create_task(self._dispatch(data, bot_name))

        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{bot_name}] 连接处理异常: {e}", exc_info=True)
            log_broker.log_connection(bot_name, 'error', {'error': str(e)})
        finally:
            with self._lock:
                self._connections.pop(bot_name, None)
            logger.info(f"[{bot_name}] OneBot 客户端已断开")
            log_broker.log_connection(bot_name, 'disconnect')
            try:
                cb = self.on_disconnect_callback
                if asyncio.iscoroutinefunction(cb):
                    await cb(bot_name)
                else:
                    await asyncio.to_thread(cb, bot_name)
            except Exception as e:
                logger.error(f"[{bot_name}] on_disconnect 回调异常: {e}")

    async def _dispatch(self, data: dict, bot_name: str):
        """有界并发处理一条事件（async handler 直接 await，sync handler 转线程）"""
        async with self._dispatch_semaphore:
            try:
                cb = self.on_message_callback
                if asyncio.iscoroutinefunction(cb):
                    await cb(data, bot_name)
                else:
                    await asyncio.to_thread(cb, data, bot_name)
            except Exception as e:
                logger.error(f"[{bot_name}] 消息处理异常: {e}", exc_info=True)

    # ── 发送 ─────────────────────────────────────────────────────

    async def asend(self, bot_name: str, data: dict) -> bool:
        """异步发送（必须在主事件循环线程内调用）"""
        with self._lock:
            ws = self._connections.get(bot_name)
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception as e:
            logger.error(f"[{bot_name}] 发送数据失败: {e}")
            return False

    def send(self, bot_name: str, data: dict) -> bool:
        """同步发送（线程安全，供 executor/Web 线程调用）"""
        if self._loop is None or not self._loop.is_running():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self.asend(bot_name, data), self._loop)
            return future.result(timeout=5)
        except Exception as e:
            logger.error(f"[{bot_name}] 发送数据失败: {e}")
            return False

    def get_connected_bots(self) -> list:
        """获取已连接的 bot 列表"""
        with self._lock:
            return list(self._connections.keys())

    async def stop_async(self):
        """异步停止服务端（在主事件循环内调用）"""
        self._running = False
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
            self._server_task = None
        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        with self._lock:
            self._connections.clear()
        logger.info("WebSocket 服务端已停止")

    def stop(self):
        """同步停止（供外部线程调用）"""
        if self._loop is None or not self._loop.is_running():
            self._running = False
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.stop_async(), self._loop)
            future.result(timeout=10)
        except Exception as e:
            logger.error(f"WebSocket 停止异常: {e}")
