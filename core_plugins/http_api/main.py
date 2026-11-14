"""
HTTP API 插件（官方插件，默认关闭）
提供 RESTful HTTP 接口，支持通过 HTTP 调用框架能力
启动后监听指定端口，接受 JSON 请求
"""
import asyncio
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from framework.security.hardening import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_READ_TIMEOUT,
    InputRejected,
    apply_socket_timeout,
    coerce_int,
    coerce_sql_params,
    coerce_text,
    read_request_body,
    strict_json_object,
    token_ok,
)

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "HTTP API",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "RESTful HTTP 接口，支持外部程序通过 HTTP 调用框架能力",
    "priority": 0,
    "official": True,
    "process": "core",
}


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""
    framework = None
    token = ""
    allow_db = False
    max_body_bytes = DEFAULT_MAX_BODY_BYTES
    read_timeout = DEFAULT_READ_TIMEOUT
    # 默认不再向任意来源反射 CORS；需要跨域时由 register() 显式配置白名单
    cors_origin = ""

    def setup(self):
        super().setup()
        apply_socket_timeout(self, self.read_timeout)

    def log_message(self, format, *args):
        """禁用默认日志"""

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if self.cors_origin:
            self.send_header('Access-Control-Allow-Origin', self.cors_origin)
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get('token', [''])[0]
        if not token or not token_ok(token, self.token):
            self._send_json(401, {'ok': False, 'error': 'token 无效'})
            return False
        return True

    def _parse_path(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.strip('/')
        params = urllib.parse.parse_qs(parsed.query)
        return path, params

    def _read_body(self):
        # 旧实现 int(Content-Length) 未校验：负数/超大值挂起线程，
        # 非数字抛未捕获异常；且 json.loads 默认接受 NaN/Infinity 与重复键
        # read_request_body 会在超限时先排空 body 再抛 413，保证客户端能收到响应
        raw = read_request_body(self, self.max_body_bytes)
        if not raw:
            return {}
        return strict_json_object(raw, self.max_body_bytes)

    def do_GET(self):
        path, params = self._parse_path()
        # 统一兜底：任何处理函数里的类型异常都不能冒泡到 socketserver
        # （实测 `?limit=20;DROP` 会让 BaseHTTPRequestHandler 直接断连、零响应）。
        try:
            if path == 'status':
                if not self._check_token():
                    return
                self._handle_status()
            elif path == 'plugins':
                if not self._check_token():
                    return
                self._handle_plugins()
            elif path == 'users':
                if not self._check_token():
                    return
                self._handle_users(params)
            elif path == 'groups':
                if not self._check_token():
                    return
                self._handle_groups()
            elif path == 'help':
                self._handle_help()
            else:
                self._send_json(404, {'ok': False, 'error': f'未知路径: {path}'})
        except (ValueError, TypeError, OverflowError):
            self._send_json(400, {'ok': False, 'error': '请求参数类型不合法'})

    def do_POST(self):
        path, params = self._parse_path()

        if not self._check_token():
            return

        try:
            body = self._read_body()
        except InputRejected as e:
            self._send_json(e.code, {'ok': False, 'error': str(e)})
            return
        except (OSError, TimeoutError, ValueError) as e:
            self._send_json(400, {'ok': False, 'error': 'JSON 解析失败: %s' % type(e).__name__})
            return

        try:
            if path == 'sendmsg':
                self._handle_sendmsg(body)
            elif path == 'send_private_msg':
                self._handle_send_private_msg(body)
            elif path == 'send_group_msg':
                self._handle_send_group_msg(body)
            elif path == 'kick':
                self._handle_kick(body)
            elif path == 'ban':
                self._handle_ban(body)
            elif path == 'unban':
                self._handle_unban(body)
            elif path == 'broadcast':
                self._handle_broadcast(body)
            elif path == 'reload':
                self._handle_reload()
            elif path == 'db/query':
                if not self.allow_db:
                    self._send_json(403, {'ok': False, 'error': '数据库网关未开启（http_api.allow_db 默认 false）'})
                    return
                self._handle_db_query(body)
            elif path == 'db/execute':
                if not self.allow_db:
                    self._send_json(403, {'ok': False, 'error': '数据库网关未开启（http_api.allow_db 默认 false）'})
                    return
                self._handle_db_execute(body)
            else:
                self._send_json(404, {'ok': False, 'error': f'未知路径: {path}'})
        except (ValueError, TypeError, OverflowError):
            self._send_json(400, {'ok': False, 'error': '请求参数类型不合法'})

    def do_OPTIONS(self):
        self.send_response(200)
        if self.cors_origin:
            self.send_header('Access-Control-Allow-Origin', self.cors_origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    # ── 处理函数 ──

    def _handle_status(self):
        fw = self.framework
        try:
            import psutil
            proc = psutil.Process()
            mem = proc.memory_info().rss / 1024 / 1024
            uptime = fw._format_uptime()

            bots = []
            try:
                adapter = fw.services.get('protocol_adapter')
                if adapter is None:
                    adapter = getattr(fw, 'protocol_adapter', None)
                if adapter is not None and hasattr(adapter, 'get_connected_bots'):
                    bots = adapter.get_connected_bots()
                else:
                    ws = fw.services.get('ws_server')
                    if ws and hasattr(ws, 'get_connected_bots'):
                        bots = ws.get_connected_bots()
            except Exception as _exc:
                logger.debug('[_handle_status] 捕获异常后按降级路径继续: %s', _exc)

            plugins = list(fw.plugin_loader.get_loaded_plugins().keys())

            self._send_json(200, {
                'ok': True,
                'data': {
                    'version': open('VERSION').read().strip() if __import__('os').path.exists('VERSION') else 'unknown',
                    'uptime': uptime,
                    'memory_mb': round(mem, 1),
                    'clients': bots,
                    'plugins': plugins,
                }
            })
        except Exception as e:
            logger.debug('[_handle_status] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_plugins(self):
        plugins = self.framework.plugin_loader.get_loaded_plugins()
        result = []
        for name, info in plugins.items():
            meta = info.get('meta', {})
            result.append({
                'name': name,
                'version': meta.get('version', '?'),
                'desc': meta.get('desc', ''),
            })
        self._send_json(200, {'ok': True, 'data': result})

    def _handle_users(self, params):
        # ASVS 5.1.4 / API4:2023：limit 必须是有界正整数。旧实现直接 int()
        # 再 f-string 拼进 SQL：畸形值抛未捕获 ValueError（连接直接断、零响应），
        # -1 与 10^20 这类值虽被 int() 挡下注入字符，却完全没有上界。
        raw = params.get('limit', ['20'])
        limit = coerce_int(raw[0] if isinstance(raw, list) and raw else None,
                           minimum=1, maximum=1000)
        if limit is None:
            self._send_json(400, {'ok': False, 'error': 'limit 必须是 1..1000 的整数'})
            return
        rows = self.framework.db.query(
            f"SELECT user_id, nickname, last_active_at FROM users ORDER BY last_active_at DESC LIMIT {limit}"
        )
        result = [{'user_id': r['user_id'], 'nickname': r['nickname'], 'last_active': str(r['last_active_at'])} for r in rows]
        self._send_json(200, {'ok': True, 'data': result})

    def _handle_groups(self):
        rows = self.framework.db.query(
            "SELECT group_id, group_name FROM groups_info WHERE is_active=1"
        )
        result = [{'group_id': r['group_id'], 'group_name': r['group_name']} for r in rows]
        self._send_json(200, {'ok': True, 'data': result})

    def _handle_sendmsg(self, body):
        user_id = body.get('user_id')
        group_id = body.get('group_id')
        if not coerce_text(body.get('message', '')):
            self._send_json(400, {'ok': False, 'error': 'message 不能为空或超长'})
            return
        uid = coerce_int(user_id, minimum=0) if user_id not in (None, '') else None
        gid = coerce_int(group_id, minimum=0) if group_id not in (None, '') else None
        if (user_id not in (None, '') and uid is None) or (group_id not in (None, '') and gid is None):
            self._send_json(400, {'ok': False, 'error': 'user_id / group_id 必须是非负整数'})
            return
        self._do_send(uid, gid, body['message'])

    def _handle_send_private_msg(self, body):
        message = coerce_text(body.get('message', ''))
        user_id = coerce_int(body.get('user_id'), minimum=0)
        if not message or user_id is None:
            self._send_json(400, {'ok': False, 'error': 'user_id 和 message 不能为空或不合法'})
            return
        self._do_send(user_id, None, message)

    def _handle_send_group_msg(self, body):
        message = coerce_text(body.get('message', ''))
        group_id = coerce_int(body.get('group_id'), minimum=0)
        if not message or group_id is None:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 message 不能为空或不合法'})
            return
        self._do_send(None, group_id, message)

    def _do_send(self, user_id, group_id, message):
        # 优先中立 send_text（协议翻译在适配器内），无则回落通用 send_msg 动作
        adapter = self.framework.services.get('protocol_adapter')
        api = self.framework.services.get('api_caller')
        if adapter is None and api is None:
            self._send_json(503, {'ok': False, 'error': '协议适配器未加载'})
            return

        async def _send():
            from framework.messaging.protocol import ProtocolAdapter
            if (adapter is not None
                    and type(adapter).send_text is not ProtocolAdapter.send_text):
                result = await adapter.send_text(
                    message,
                    group_id=int(group_id) if group_id else None,
                    user_id=int(user_id) if user_id else None,
                )
                if isinstance(result, dict) and result.get('status') == 'unsupported':
                    raise RuntimeError(result.get('msg', '当前接入端不支持主动发送'))
                return result
            if api is None:
                raise RuntimeError('协议适配器未加载')
            if group_id:
                return await api.acall('send_msg', group_id=int(group_id), message=message)
            if user_id:
                return await api.acall('send_msg', user_id=int(user_id), message=message)
            raise ValueError('必须指定 user_id 或 group_id')

        try:
            loop = self.framework.loop
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_send(), loop)
                future.result(timeout=10)
            else:
                asyncio.run(_send())
            self._send_json(200, {'ok': True})
        except Exception as e:
            logger.debug('[_do_send] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_kick(self, body):
        group_id = coerce_int(body.get('group_id'), minimum=1)
        user_id = coerce_int(body.get('user_id'), minimum=1)
        if group_id is None or user_id is None:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 user_id 不能为空或不合法'})
            return
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': '协议适配器未加载'})
            return

        async def _kick():
            result = await api.acall('set_group_kick',
                                     group_id=group_id,
                                     user_id=user_id)
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise RuntimeError(result.get('msg') or 'set_group_kick 失败')
            return result

        try:
            future = asyncio.run_coroutine_threadsafe(_kick(), self.framework.loop)
            future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            logger.debug('[_handle_kick] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_ban(self, body):
        group_id = coerce_int(body.get('group_id'), minimum=1)
        user_id = coerce_int(body.get('user_id'), minimum=1)
        # 上限 30 天：旧实现 int(duration) 无上界，10^30 会一路传给协议端
        duration = coerce_int(body.get('duration', 600), minimum=0, maximum=2592000)
        if group_id is None or user_id is None or duration is None:
            self._send_json(400, {'ok': False, 'error': 'group_id / user_id / duration 不合法'})
            return
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': '协议适配器未加载'})
            return

        async def _ban():
            result = await api.acall('set_group_ban',
                                     group_id=group_id,
                                     user_id=user_id,
                                     duration=duration)
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise RuntimeError(result.get('msg') or 'set_group_ban 失败')
            return result

        try:
            future = asyncio.run_coroutine_threadsafe(_ban(), self.framework.loop)
            future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            logger.debug('[_handle_ban] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_unban(self, body):
        group_id = coerce_int(body.get('group_id'), minimum=1)
        user_id = coerce_int(body.get('user_id'), minimum=1)
        if group_id is None or user_id is None:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 user_id 不能为空或不合法'})
            return
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': '协议适配器未加载'})
            return

        async def _unban():
            result = await api.acall('set_group_ban',
                                     group_id=group_id,
                                     user_id=user_id,
                                     duration=0)
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise RuntimeError(result.get('msg') or 'set_group_ban 失败')
            return result

        try:
            future = asyncio.run_coroutine_threadsafe(_unban(), self.framework.loop)
            future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            logger.debug('[_handle_unban] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_broadcast(self, body):
        message = coerce_text(body.get('message', ''))
        if not message:
            self._send_json(400, {'ok': False, 'error': 'message 不能为空或超长'})
            return

        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': '协议适配器未加载'})
            return

        try:
            rows = self.framework.db.query("SELECT group_id FROM groups_info WHERE is_active=1")
            group_ids = [r['group_id'] for r in rows]

            async def _broadcast():
                success = 0
                for gid in group_ids:
                    try:
                        await api.acall('send_msg', group_id=gid, message=message)
                        success += 1
                    except Exception as _exc:
                        logger.debug('[_broadcast] 捕获异常后按降级路径继续: %s', _exc)
                return success, len(group_ids)

            future = asyncio.run_coroutine_threadsafe(_broadcast(), self.framework.loop)
            success, total = future.result(timeout=30)
            self._send_json(200, {'ok': True, 'data': {'success': success, 'total': total}})
        except Exception as e:
            logger.debug('[_handle_broadcast] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_reload(self):
        try:
            loaded = self.framework.plugin_loader.reload_all()
            self._send_json(200, {'ok': True, 'data': {'reloaded': loaded}})
        except Exception as e:
            logger.debug('[_handle_reload] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_db_query(self, body):
        sql = coerce_text(body.get('sql', ''), max_len=64 * 1024)
        params = coerce_sql_params(body.get('params', []))
        if not sql or params is None:
            self._send_json(400, {'ok': False, 'error': 'sql / params 不合法（params 须为标量数组）'})
            return
        try:
            rows = self.framework.db.query(sql, params)
            result = [dict(r) for r in rows]
            self._send_json(200, {'ok': True, 'data': result})
        except Exception as e:
            logger.debug('[_handle_db_query] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_db_execute(self, body):
        sql = coerce_text(body.get('sql', ''), max_len=64 * 1024)
        params = coerce_sql_params(body.get('params', []))
        if not sql or params is None:
            self._send_json(400, {'ok': False, 'error': 'sql / params 不合法（params 须为标量数组）'})
            return
        try:
            affected = self.framework.db.execute(sql, params)
            self._send_json(200, {'ok': True, 'data': {'affected': affected}})
        except Exception as e:
            logger.debug('[_handle_db_execute] 捕获异常后按降级路径继续: %s', e)
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_help(self):
        self._send_json(200, {
            'ok': True,
            'data': {
                'endpoints': {
                    'GET /status': '框架状态',
                    'GET /plugins': '已加载插件列表',
                    'GET /users?limit=20': '用户列表',
                    'GET /groups': '群列表',
                    'GET /help': '本帮助',
                    'POST /sendmsg': '发送消息 {user_id, group_id, message}',
                    'POST /send_private_msg': '私聊 {user_id, message}',
                    'POST /send_group_msg': '群聊 {group_id, message}',
                    'POST /kick': '踢人 {group_id, user_id}',
                    'POST /ban': '禁言 {group_id, user_id, duration}',
                    'POST /unban': '解禁 {group_id, user_id}',
                    'POST /broadcast': '广播 {message}',
                    'POST /reload': '重载插件',
                    'POST /db/query': '查询 {sql, params}',
                    'POST /db/execute': '执行 {sql, params}',
                },
                'auth': '所有接口（除 /help）需带 ?token=xxx 参数',
            }
        })


class HttpApiServer:
    """HTTP API 服务"""

    def __init__(self, framework, host, port, token, allow_db=False,
                 max_body_bytes=None, read_timeout=None, cors_origin=''):
        self.framework = framework
        self.host = host
        self.port = port
        self.token = token
        self.allow_db = allow_db
        self.max_body_bytes = int(max_body_bytes or DEFAULT_MAX_BODY_BYTES)
        self.read_timeout = float(read_timeout or DEFAULT_READ_TIMEOUT)
        self.cors_origin = str(cors_origin or '')
        self._server = None
        self._thread = None

    def start(self):
        ApiHandler.framework = self.framework
        ApiHandler.token = self.token
        ApiHandler.allow_db = self.allow_db
        ApiHandler.max_body_bytes = self.max_body_bytes
        ApiHandler.read_timeout = self.read_timeout
        ApiHandler.cors_origin = self.cors_origin
        self._server = ThreadingHTTPServer((self.host, self.port), ApiHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="http-api")
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()


_server = None


def register(ctx):
    """注册 HTTP API 插件"""
    global _server
    fw = ctx._framework

    http_cfg = fw.config.get('http_api', {})
    if http_cfg.get('enabled') is False:
        ctx.log("HTTP API 已禁用 (http_api.enabled: false)")
        return

    host = http_cfg.get('host', '127.0.0.1')
    port = http_cfg.get('port', 1145)
    token = http_cfg.get('token', '')
    allow_db = http_cfg.get('allow_db', False)

    if not token:
        import secrets
        token = secrets.token_hex(16)
        ctx.log(f"未配置 http_api.token，已自动生成: {token}")
        ctx.log("请在 config.yaml 中设置 http_api.token 以固定令牌")

    try:
        _server = HttpApiServer(
            fw, host, port, token, allow_db,
            max_body_bytes=http_cfg.get('max_body_bytes', DEFAULT_MAX_BODY_BYTES),
            read_timeout=http_cfg.get('read_timeout', DEFAULT_READ_TIMEOUT),
            cors_origin=http_cfg.get('cors_origin', ''))
        _server.start()
        fw.services.register('http_api', _server)
        ctx.log(f"HTTP API 已启动: http://{host}:{port}")
        ctx.log(f"Token: {token}")
        ctx.log(f"数据库网关(db/query, db/execute): {'已开启' if allow_db else '已关闭 (http_api.allow_db 默认 false)'}")
    except Exception as e:
        ctx.log(f"HTTP API 启动失败: {e}", level="error")


def unregister():
    global _server
    if _server:
        _server.stop()
        _server = None
