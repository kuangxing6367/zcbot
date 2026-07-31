"""
OneBot 11 WebSocket 服务端
框架作为服务端监听端口，OneBot 客户端（go-cqhttp/NapCat 等）配置反向 WS 主动连接
支持多个 OneBot 客户端同时连接
向下兼容 websockets 10.x ~ 16.x+
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
        self._loop = None
        self._thread = None
        self._running = False
        self._connections = {}  # bot_name -> websocket connection
        self._conn_counter = 0
        self._lock = threading.Lock()

    def start(self):
        """启动 WebSocket 服务端（在独立线程中运行 asyncio 事件循环）"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-server")
        self._thread.start()

    def _run(self):
        """运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"WebSocket 服务端异常: {e}")
        finally:
            self._loop.close()

    async def _serve(self):
        """启动 WebSocket 服务"""
        serve_kwargs = {
            'ping_interval': 30,
            'ping_timeout': 10,
        }

        # websockets 13+ 支持 process_request(connection, request)
        # websockets 10-12 支持 process_request(path, request_headers)
        # 使用版本检测来选择合适的签名
        if _WS_MAJOR >= 13:
            serve_kwargs['process_request'] = self._process_request_v13
        elif _WS_MAJOR >= 10:
            serve_kwargs['process_request'] = self._process_request_v10

        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            **serve_kwargs,
        )
        logger.info(f"WebSocket 服务端已启动: ws://{self.host}:{self.port} (websockets {websockets.__version__})")
        await asyncio.Future()

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
            self.on_connect_callback(bot_name, ws)
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

                # 判断是否为事件上报 — 放到独立线程处理，避免阻塞事件循环
                post_type = data.get("post_type")
                if post_type:
                    threading.Thread(
                        target=self._safe_message_callback,
                        args=(data, bot_name),
                        daemon=True,
                        name=f"msg-{bot_name}"
                    ).start()

        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"[{bot_name}] 连接处理异常: {e}", exc_info=True)
            log_broker.log_connection(bot_name, 'error', {'error': str(e)})
        finally:
            with self._lock:
                self._connections.pop(bot_name, None)
            logger.info(f"[{bot_name}] OneBot 客户端已断开")
            log_broker.log_connection(bot_name, 'disconnect')
            try:
                self.on_disconnect_callback(bot_name)
            except Exception as e:
                logger.error(f"[{bot_name}] on_disconnect 回调异常: {e}")

    def _safe_message_callback(self, data: dict, bot_name: str):
        """在独立线程中安全执行消息回调"""
        try:
            self.on_message_callback(data, bot_name)
        except Exception as e:
            logger.error(f"[{bot_name}] 消息处理异常: {e}", exc_info=True)

    def send(self, bot_name: str, data: dict) -> bool:
        """向指定 OneBot 客户端发送数据（线程安全，从任意线程调用）"""
        with self._lock:
            ws = self._connections.get(bot_name)
        if ws is None:
            return False

        if self._loop is None or not self._loop.is_running():
            return False

        future = asyncio.run_coroutine_threadsafe(
            ws.send(json.dumps(data, ensure_ascii=False)),
            self._loop
        )
        try:
            future.result(timeout=5)
            return True
        except Exception as e:
            logger.error(f"[{bot_name}] 发送数据失败: {e}")
            return False

    def get_connected_bots(self) -> list:
        """获取已连接的 bot 列表"""
        with self._lock:
            return list(self._connections.keys())

    def stop(self):
        """停止服务端"""
        self._running = False
        if self._server and self._loop and self._loop.is_running():
            # websockets 16+: close() 是普通方法，wait_closed() 是协程
            # websockets 10-12: close() 是协程
            close_result = self._server.close()
            if asyncio.iscoroutine(close_result):
                asyncio.run_coroutine_threadsafe(close_result, self._loop)
            elif hasattr(self._server, 'wait_closed'):
                asyncio.run_coroutine_threadsafe(self._server.wait_closed(), self._loop)
        logger.info("WebSocket 服务端已停止")
