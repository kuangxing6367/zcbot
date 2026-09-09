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
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "HTTP API",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "RESTful HTTP 接口，支持外部程序通过 HTTP 调用框架能力",
    "priority": 0,
    "official": True,
}


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""
    framework = None
    token = ""

    def log_message(self, format, *args):
        """禁用默认日志"""
        pass

    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _check_token(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get('token', [''])[0]
        if not token or token != self.token:
            self._send_json(401, {'ok': False, 'error': 'token 无效'})
            return False
        return True

    def _parse_path(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.strip('/')
        params = urllib.parse.parse_qs(parsed.query)
        return path, params

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body.decode('utf-8'))

    def do_GET(self):
        path, params = self._parse_path()

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

    def do_POST(self):
        path, params = self._parse_path()

        if not self._check_token():
            return

        try:
            body = self._read_body()
        except Exception:
            self._send_json(400, {'ok': False, 'error': 'JSON 解析失败'})
            return

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
            self._handle_db_query(body)
        elif path == 'db/execute':
            self._handle_db_execute(body)
        else:
            self._send_json(404, {'ok': False, 'error': f'未知路径: {path}'})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
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
                ws = fw.services.get('ws_server')
                if ws and hasattr(ws, 'get_connected_bots'):
                    bots = ws.get_connected_bots()
            except Exception:
                pass

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
        limit = int(params.get('limit', ['20'])[0])
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
        message = body.get('message', '')
        if not message:
            self._send_json(400, {'ok': False, 'error': 'message 不能为空'})
            return
        self._do_send(user_id, group_id, message)

    def _handle_send_private_msg(self, body):
        user_id = body.get('user_id')
        message = body.get('message', '')
        if not user_id or not message:
            self._send_json(400, {'ok': False, 'error': 'user_id 和 message 不能为空'})
            return
        self._do_send(int(user_id), None, message)

    def _handle_send_group_msg(self, body):
        group_id = body.get('group_id')
        message = body.get('message', '')
        if not group_id or not message:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 message 不能为空'})
            return
        self._do_send(None, int(group_id), message)

    def _do_send(self, user_id, group_id, message):
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': 'OneBot 适配器未加载'})
            return

        async def _send():
            if group_id:
                await api.send_group_msg(group_id=int(group_id), message=message)
            elif user_id:
                await api.send_private_msg(user_id=int(user_id), message=message)
            else:
                raise ValueError('必须指定 user_id 或 group_id')

        try:
            loop = self.framework.loop
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_send(), loop)
                future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_kick(self, body):
        group_id = body.get('group_id')
        user_id = body.get('user_id')
        if not group_id or not user_id:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 user_id 不能为空'})
            return
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': 'OneBot 适配器未加载'})
            return

        async def _kick():
            await api.set_group_kick(group_id=int(group_id), user_id=int(user_id))

        try:
            future = asyncio.run_coroutine_threadsafe(_kick(), self.framework.loop)
            future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_ban(self, body):
        group_id = body.get('group_id')
        user_id = body.get('user_id')
        duration = body.get('duration', 600)
        if not group_id or not user_id:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 user_id 不能为空'})
            return
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': 'OneBot 适配器未加载'})
            return

        async def _ban():
            await api.set_group_ban(group_id=int(group_id), user_id=int(user_id), duration=int(duration))

        try:
            future = asyncio.run_coroutine_threadsafe(_ban(), self.framework.loop)
            future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_unban(self, body):
        group_id = body.get('group_id')
        user_id = body.get('user_id')
        if not group_id or not user_id:
            self._send_json(400, {'ok': False, 'error': 'group_id 和 user_id 不能为空'})
            return
        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': 'OneBot 适配器未加载'})
            return

        async def _unban():
            await api.set_group_ban(group_id=int(group_id), user_id=int(user_id), duration=0)

        try:
            future = asyncio.run_coroutine_threadsafe(_unban(), self.framework.loop)
            future.result(timeout=10)
            self._send_json(200, {'ok': True})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_broadcast(self, body):
        message = body.get('message', '')
        if not message:
            self._send_json(400, {'ok': False, 'error': 'message 不能为空'})
            return

        api = self.framework.services.get('api_caller')
        if not api:
            self._send_json(503, {'ok': False, 'error': 'OneBot 适配器未加载'})
            return

        try:
            rows = self.framework.db.query("SELECT group_id FROM groups_info WHERE is_active=1")
            group_ids = [r['group_id'] for r in rows]

            async def _broadcast():
                success = 0
                for gid in group_ids:
                    try:
                        await api.send_group_msg(group_id=gid, message=message)
                        success += 1
                    except Exception:
                        pass
                return success, len(group_ids)

            future = asyncio.run_coroutine_threadsafe(_broadcast(), self.framework.loop)
            success, total = future.result(timeout=30)
            self._send_json(200, {'ok': True, 'data': {'success': success, 'total': total}})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_reload(self):
        try:
            loaded = self.framework.plugin_loader.reload_all()
            self._send_json(200, {'ok': True, 'data': {'reloaded': loaded}})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_db_query(self, body):
        sql = body.get('sql', '')
        params = body.get('params', [])
        if not sql:
            self._send_json(400, {'ok': False, 'error': 'sql 不能为空'})
            return
        try:
            rows = self.framework.db.query(sql, tuple(params))
            result = [dict(r) for r in rows]
            self._send_json(200, {'ok': True, 'data': result})
        except Exception as e:
            self._send_json(500, {'ok': False, 'error': str(e)})

    def _handle_db_execute(self, body):
        sql = body.get('sql', '')
        params = body.get('params', [])
        if not sql:
            self._send_json(400, {'ok': False, 'error': 'sql 不能为空'})
            return
        try:
            affected = self.framework.db.execute(sql, tuple(params))
            self._send_json(200, {'ok': True, 'data': {'affected': affected}})
        except Exception as e:
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

    def __init__(self, framework, host, port, token):
        self.framework = framework
        self.host = host
        self.port = port
        self.token = token
        self._server = None
        self._thread = None

    def start(self):
        ApiHandler.framework = self.framework
        ApiHandler.token = self.token

        self._server = HTTPServer((self.host, self.port), ApiHandler)
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

    if not token:
        import secrets
        token = secrets.token_hex(16)
        ctx.log(f"未配置 http_api.token，已自动生成: {token}")
        ctx.log(f"请在 config.yaml 中设置 http_api.token 以固定令牌")

    try:
        _server = HttpApiServer(fw, host, port, token)
        _server.start()
        fw.services.register('http_api', _server)
        ctx.log(f"HTTP API 已启动: http://{host}:{port}")
        ctx.log(f"Token: {token}")
    except Exception as e:
        ctx.log(f"HTTP API 启动失败: {e}", level="error")


def unregister():
    global _server
    if _server:
        _server.stop()
        _server = None
