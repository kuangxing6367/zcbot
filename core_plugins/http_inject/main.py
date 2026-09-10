# -*- coding: utf-8 -*-
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
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from framework.protocol import ProtocolAdapter

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

    # ---- 请求处理 ----
    def do_POST(self):
        if not self.path.startswith(self.path_prefix):
            self._json(404, {"ok": False, "error": f"未知路径 {self.path}，期望 {self.path_prefix}"})
            return

        # Token 校验（配置了才校验）
        if self.token:
            provided = self.headers.get("Authorization", "")
            if provided != f"Bearer {self.token}" and provided != self.token:
                self._json(401, {"ok": False, "error": "invalid token"})
                return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid JSON body"})
            return
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "body must be a JSON object"})
            return

        event = self._build_event(data)
        # 从 HTTP 线程跨到框架事件循环投递
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.framework.dispatch_event(event), self.framework.loop)
            fut.result(timeout=10)
        except Exception:
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
            event = {
                "type": "message",
                "message_type": data.get("message_type", "private"),
                "bot_name": data.get("bot_name", "http_inject"),
                "user_id": data.get("user_id", 0),
                "group_id": data.get("group_id"),
                "message": data.get("message", "") or data.get("text", ""),
                "sender": data.get("sender") or {
                    "user_id": data.get("user_id", 0),
                    "nickname": data.get("nickname", "http"),
                },
            }
        else:
            # 完整事件透传（notice / request / meta_event / 自定义）
            event = dict(data)
            event.setdefault("bot_name", "http_inject")
        return event

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """不刷默认访问日志（避免刷屏）"""
        pass


class HttpInjectAdapter(ProtocolAdapter):
    """实现 ProtocolAdapter 契约：HTTP 事件注入接入端"""

    def __init__(self, framework, host, port, path, token):
        self.framework = framework
        self.host = host
        self.port = port
        self.path = path
        self.token = token
        self._server = None

    def start(self):
        HttpInjectHandler.framework = self.framework
        HttpInjectHandler.path_prefix = self.path
        HttpInjectHandler.token = self.token
        self._server = HTTPServer((self.host, self.port), HttpInjectHandler)
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

    _adapter_instance = HttpInjectAdapter(fw, host, port, path, token)

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
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_adapter_instance.stop())
        _adapter_instance = None
