"""
HTTP 事件注入接入端（http_inject）

把外部系统经 HTTP POST 投递的消息/事件注入框架，由插件处理。
它不是"对外暴露框架控制能力"的 REST（那是 http_api），而是一个真正的
ProtocolAdapter：把 HTTP 请求当作事件源，交给 framework.dispatch_event。

典型用途（不依赖任何 IM）：
  - 搭 CMS：发布文章 / 新评论 / 用户操作 → POST 进来 → 插件响应
  - 外部系统（GitHub Webhook / 支付回调 / CI）→ 注入事件
  - 测试 / 联调：curl 一条消息，验证插件逻辑

用法：
  1) config.yaml 里 core_plugins.http_inject: true
  2) http_inject.enabled: true（默认关闭，避免意外开端口）
  3) curl -X POST http://127.0.0.1:8901/hook \\
        -H "Content-Type: application/json" \\
        -d '{"user_id": 10001, "message": "/echo hi"}'
"""
import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from framework.messaging.protocol import ProtocolAdapter
from framework.security.hardening import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_READ_TIMEOUT,
    InputRejected,
    apply_socket_timeout,
    read_request_body,
    sanitize_event,
    strict_json_object,
    token_ok,
)

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "http_inject",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "HTTP 事件注入接入端：外部 POST 注入消息/事件",
    "priority": 10,
    "official": True,
    "process": "core",
}


class HttpInjectHandler(BaseHTTPRequestHandler):
    """把 HTTP POST 负载转成内部事件，投递到框架事件流"""
    framework = None
    path_prefix = "/hook"
    token = ""
    max_body_bytes = DEFAULT_MAX_BODY_BYTES
    read_timeout = DEFAULT_READ_TIMEOUT

    def setup(self):
        super().setup()
        # 无 socket 超时时，慢请求头 / 慢 body 会各占一个线程永不释放
        apply_socket_timeout(self, self.read_timeout)

    # ---- 请求处理 ----
    def do_POST(self):
        # 精确匹配前缀，避免 /hookxyz 这类同前缀路径被当成合法注入端点
        path_only = self.path.split('?', 1)[0]
        if path_only != self.path_prefix and not path_only.startswith(
                self.path_prefix.rstrip('/') + '/'):
            self._json(404, {"ok": False, "error": f"未知路径 {self.path}，期望 {self.path_prefix}"})
            return

        # Token 校验（配置了才校验），常量时间比较避免短路时序侧信道
        if self.token and not token_ok(self.headers.get("Authorization", ""), self.token):
            self._json(401, {"ok": False, "error": "invalid token"})
            return

        try:
            # 旧实现 int(Content-Length) 未校验：非数字抛未捕获 ValueError、
            # 负数/超大值让 rfile.read() 挂到客户端超时，且残留字节会被当成事件派发。
            # 超限请求先排空 body 再回 413，否则客户端被 RST 拿不到响应。
            raw = read_request_body(self, self.max_body_bytes) or b"{}"
            # 拒绝 NaN/Infinity/重复键/顶层标量/非法 UTF-8/孤立代理
            data = strict_json_object(raw, self.max_body_bytes)
        except InputRejected as e:
            self._json(e.code, {"ok": False, "error": str(e)})
            return
        except (OSError, TimeoutError, ValueError) as e:
            self._json(400, {"ok": False, "error": "request read failed: %s" % type(e).__name__})
            return

        event = self._build_event(data)
        # 从 HTTP 线程跨到框架事件循环投递
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.framework.dispatch_event(event), self.framework.loop)
            fut.result(timeout=10)
        except Exception as _exc:
            logger.debug('[do_POST] 捕获异常后按降级路径继续: %s', _exc)
            self._json(500, {"ok": False, "error": "dispatch failed"})
            return

        self._json(200, {"ok": True, "type": event.get("type")})

    def do_GET(self):
        self._json(200, {"ok": True, "hint": f"POST {self.path_prefix} 即可注入事件"})

    # ---- 事件构造 ----
    def _build_event(self, data: dict) -> dict:
        """把 HTTP 负载翻译成框架内部事件 dict（接入端无关）"""
        msg_type = data.get("type", "message")
        if msg_type == "message":
            # ASVS 5.1.4 强类型：message 只允许字符串或 OneBot CQ 段数组，
            # sender 只允许对象；外部塞 dict/int/list-of-anything 不该一路流到
            # Event 构造与插件里再炸成未捕获异常。
            message = data.get("message", "") or data.get("text", "")
            if not isinstance(message, (str, list, tuple)):
                message = ""
            sender = data.get("sender")
            if not isinstance(sender, dict):
                sender = {
                    "user_id": data.get("user_id", 0),
                    "nickname": data.get("nickname", "http"),
                }
            event = {
                "type": "message",
                "message_type": data.get("message_type", "private"),
                "bot_name": data.get("bot_name", "http_inject"),
                "user_id": data.get("user_id", 0),
                "group_id": data.get("group_id"),
                "message": message,
                "sender": sender,
            }
        else:
            # 完整事件透传（notice / request / meta_event / 自定义）
            event = dict(data)
        # 外部输入不得携带 role/permissions/token 等内部控制字段，
        # 也不得伪造 bot_name 冒充其它接入端；同时限制嵌套深度与超长字符串
        return sanitize_event(event, "http_inject")

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """不刷默认访问日志（避免刷屏）"""


class HttpInjectAdapter(ProtocolAdapter):
    """实现 ProtocolAdapter 契约：HTTP 事件注入接入端"""

    adapter_id = 'http_inject'

    def __init__(self, framework, host, port, path, token,
                 max_body_bytes=None, read_timeout=None):
        self.framework = framework
        self.host = host
        self.port = port
        self.path = path
        self.token = token
        self.max_body_bytes = int(max_body_bytes or DEFAULT_MAX_BODY_BYTES)
        self.read_timeout = float(read_timeout or DEFAULT_READ_TIMEOUT)
        self._server = None

    def get_connection_info(self) -> dict:
        """连接自描述：供 WebUI /api/connection 动态渲染"""
        return {
            'id': 'http_inject',
            'name': 'HTTP 事件注入',
            'config_section': 'http_inject',
            'fields': [
                {'key': 'host', 'label': '监听地址', 'type': 'string'},
                {'key': 'port', 'label': '监听端口', 'type': 'number'},
                {'key': 'path', 'label': '注入路径', 'type': 'string'},
                {'key': 'token', 'label': 'Token', 'type': 'password'},
            ],
            'restart_keys': ['host', 'port', 'path'],
            'endpoint_hint': f"http://{self.host}:{self.port}{self.path}",
            'guide': '外部系统 POST JSON 到上述地址即可注入消息/事件（不依赖任何 IM）。',
        }

    def start(self):
        HttpInjectHandler.framework = self.framework
        HttpInjectHandler.path_prefix = self.path
        HttpInjectHandler.token = self.token
        HttpInjectHandler.max_body_bytes = self.max_body_bytes
        HttpInjectHandler.read_timeout = self.read_timeout
        self._server = ThreadingHTTPServer((self.host, self.port), HttpInjectHandler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever,
                         daemon=True, name="http-inject").start()

    async def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    async def handle_event(self, raw_event, bot_name):
        # 事件已在 HttpInjectHandler 构造好，原样透传
        return raw_event

    async def call_api(self, action, bot=None, **params):
        # 注入端不主动发消息；有需要可在此转发到外部通知渠道
        return {}

    def get_connected_bots(self):
        return ["http_inject"]


_adapter_instance = None


def register(ctx):
    """注册为协议适配器服务并启动（默认关闭，需 http_inject.enabled: true）"""
    global _adapter_instance
    fw = ctx._framework

    cfg = fw.config.get('http_inject', {})
    if cfg.get('enabled') is not True:
        ctx.log("HTTP 注入接入端已禁用 (http_inject.enabled: true 才开启)")
        return

    host = cfg.get('host', '127.0.0.1')
    port = cfg.get('port', 8901)
    path = cfg.get('path', '/hook')
    token = cfg.get('token', '')

    _adapter_instance = HttpInjectAdapter(
        fw, host, port, path, token,
        max_body_bytes=cfg.get('max_body_bytes', DEFAULT_MAX_BODY_BYTES),
        read_timeout=cfg.get('read_timeout', DEFAULT_READ_TIMEOUT))

    # 注册为接入端（覆盖优先级按 core_plugins 加载顺序；通常只开一个接入端）
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)

    _adapter_instance.start()
    ctx.log(f"HTTP 注入接入端已启动: http://{host}:{port}{path}"
            + ("（Token 认证开启）" if token else "（未设 Token，仅建议内网使用）"))


def unregister():
    """卸载时停止"""
    global _adapter_instance
    if _adapter_instance:
        inst = _adapter_instance
        _adapter_instance = None
        loop = getattr(inst.framework, 'loop', None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(inst.stop(), loop)
