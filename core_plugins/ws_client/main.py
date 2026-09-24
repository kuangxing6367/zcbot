# -*- coding: utf-8 -*-
"""
出站 WebSocket 接入端（ws_client）

与 onebot_adapter（反向 WS 服务端，等客户端连入）相反：本适配器作为**客户端**
主动连出到外部 WebSocket 服务，把远端 JSON 事件归一化送入内核，并把
send_msg / send_text / base64 图片等出站动作翻译回远端协议。

典型用途：
  - 桥接到自研网关 / 中控 / 多机器人总线
  - 与另一套 IM 网关做双向透传
  - 本地联调：一条 `ws://` 就能模拟完整收发链路

消息约定（JSON text frame）：
  入站（远端 → 本端）
    {"type":"message","message_type":"private"|"group","user_id":1,
     "group_id":null,"message":"文本或消息段数组","sender":{...}, ...}
    其它 type（notice/request/自定义）原样透传，补 bot_name。

  出站（本端 → 远端）
    {"action":"send_msg","user_id":..,"group_id":..,"message":..}
    message 支持纯文本 / 消息段数组；image 段的 file 支持
    base64://...、file://、http(s)://、本地路径。

依赖：websockets（已在 requirements.txt，无新增三方包）。
"""
import asyncio
import base64
import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import websockets

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "ws_client",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "出站 WebSocket 接入端：主动连出到外部 WS 服务，双向收发",
    "priority": 10,
    "official": True,
    "process": "core",
}

_B64_RE = re.compile(r'^base64://([A-Za-z0-9+/=\s]+)$', re.DOTALL)
_CQ_IMAGE_RE = re.compile(
    r'\[CQ:image,file=([^\],]+)(?:,([^\]]*))?\]', re.IGNORECASE)


def _extract_b64(data: str) -> Optional[str]:
    """把 base64://... 归一成不带前缀的纯 base64（失败返回 None）"""
    if not isinstance(data, str):
        return None
    m = _B64_RE.match(data.strip())
    if m:
        return re.sub(r'\s+', '', m.group(1))
    # 已是裸 base64 且长度合理
    s = re.sub(r'\s+', '', data)
    if len(s) >= 16 and len(s) % 4 == 0 and re.fullmatch(r'[A-Za-z0-9+/=]+', s):
        try:
            base64.b64decode(s, validate=True)
            return s
        except Exception:
            return None
    return None


def normalize_message(message):
    """
    出站消息归一化：
    - str：解析 CQ:image 的 file=base64://... 为消息段（保留其余文本）
    - list：逐段处理 image.file 的 base64:// / 相对路径提示
    返回可 JSON 序列化的 message（str 或 list）。
    """
    if isinstance(message, list):
        out = []
        for seg in message:
            if not isinstance(seg, dict):
                out.append(seg)
                continue
            seg = dict(seg)
            data = dict(seg.get('data') or {})
            if seg.get('type') == 'image':
                file = data.get('file') or data.get('url') or ''
                b64 = _extract_b64(str(file))
                if b64:
                    data['file'] = f'base64://{b64}'
                    data['base64'] = b64
                    data.pop('url', None)
            out.append({'type': seg.get('type', 'text'), 'data': data})
        return out

    if not isinstance(message, str):
        return message

    # CQ 码里的 base64 图片 → 消息段
    if 'base64://' not in message and not _CQ_IMAGE_RE.search(message):
        return message

    parts = []
    last = 0
    for m in _CQ_IMAGE_RE.finditer(message):
        head = message[last:m.start()]
        if head:
            parts.append({'type': 'text', 'data': {'text': head}})
        file = m.group(1)
        b64 = _extract_b64(file)
        img_data = {}
        if b64:
            img_data = {'file': f'base64://{b64}', 'base64': b64}
        else:
            img_data = {'file': file}
            # 其它 KV
            if m.group(2):
                for kv in m.group(2).split(','):
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        img_data.setdefault(k.strip(), v.strip())
        parts.append({'type': 'image', 'data': img_data})
        last = m.end()
    tail = message[last:]
    if tail:
        parts.append({'type': 'text', 'data': {'text': tail}})

    # 裸 base64:// 整串（优先于 CQ 分支，避免被当成普通文本）
    stripped = message.strip()
    if stripped.startswith('base64://'):
        b64 = _extract_b64(stripped)
        if b64:
            return [{'type': 'image',
                     'data': {'file': f'base64://{b64}', 'base64': b64}}]

    if not parts:
        return message
    return parts


def normalize_event(raw: dict, bot_name: str) -> Optional[dict]:
    """远端 JSON → 框架内部事件 dict（与 http_inject 同形状约定）"""
    if not isinstance(raw, dict):
        return None
    msg_type = raw.get('type', 'message')
    if msg_type == 'message':
        event = {
            'type': 'message',
            'message_type': raw.get('message_type', 'private'),
            'sub_type': raw.get('sub_type', ''),
            'bot_name': raw.get('bot_name') or bot_name,
            'user_id': raw.get('user_id', 0),
            'group_id': raw.get('group_id'),
            'message_id': raw.get('message_id'),
            'message': raw.get('message', '') or raw.get('text', ''),
            'raw_message': raw.get('raw_message', ''),
            'sender': raw.get('sender') or {
                'user_id': raw.get('user_id', 0),
                'nickname': raw.get('nickname', bot_name),
            },
            'adapter': 'ws_client',
            'raw': raw,
        }
        return event
    event = dict(raw)
    event.setdefault('bot_name', bot_name)
    event.setdefault('adapter', 'ws_client')
    event.setdefault('raw', raw)
    return event


class WsClientAdapter(ProtocolAdapter):
    """出站 WebSocket 客户端接入端"""

    adapter_id = 'ws_client'

    def __init__(self, framework, url, bot_name='ws_client', token='',
                 reconnect_interval=5, max_queue=256):
        self.framework = framework
        self.url = url
        self.bot_name = bot_name or 'ws_client'
        self.token = token or ''
        self.reconnect_interval = max(1, int(reconnect_interval or 5))
        self.max_queue = max(16, int(max_queue or 256))
        self._ws = None
        self._connected = False
        self._closing = False
        self._recv_task = None
        self._supervisor = None
        self._pending = []          # 连接前积压的出站 dict
        self._loop = None

    # ── 连接自描述 ──────────────────────────────────────────────

    def get_connection_info(self) -> dict:
        try:
            p = urlparse(self.url)
            hint = f"{p.scheme}://{p.netloc}{p.path or '/'}"
        except Exception:
            hint = self.url
        return {
            'id': 'ws_client',
            'name': '出站 WebSocket',
            'config_section': 'ws_client',
            'fields': [
                {'key': 'url', 'label': '远端 URL', 'type': 'string'},
                {'key': 'bot_name', 'label': 'Bot 名', 'type': 'string'},
                {'key': 'token', 'label': 'Token', 'type': 'password'},
                {'key': 'reconnect_interval', 'label': '重连间隔(秒)', 'type': 'number'},
            ],
            'restart_keys': ['url', 'bot_name'],
            'endpoint_hint': hint,
            'guide': '本端主动连出到外部 WebSocket 服务；远端推送 JSON 事件，'
                     '本端经 send_msg 回发。图片支持 base64:// 消息段。',
            'status_extra': {
                'connected': self._connected,
                'url': self.url,
            },
        }

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        try:
            self._loop = self.framework.loop
        except Exception:
            self._loop = None
        loop = self._loop
        if loop is None or not loop.is_running():
            # 尚未进入事件循环：延后到框架就绪（framework.start 内会再调或由 hook）
            logger.warning("ws_client: 事件循环未就绪，连接将在 loop 就绪后建立")
            self._schedule_connect()
            return
        self._schedule_connect()

    def _schedule_connect(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            # 最后手段：在下一个可用 tick 尝试
            try:
                asyncio.get_event_loop().call_soon(
                    lambda: self._schedule_connect())
            except Exception:
                pass
            return
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = loop.create_task(self._supervise())

    async def _supervise(self):
        """断线自动重连，直到 stop()"""
        while not self._closing:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"ws_client 连接异常: {e}")
            finally:
                self._connected = False
                self._ws = None
            if self._closing:
                break
            await asyncio.sleep(self.reconnect_interval)

    async def _connect_once(self):
        headers = {}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        # websockets 14.x 参数名 additional_headers；12/13 为 extra_headers
        try:
            _major = int(str(websockets.__version__).split('.')[0])
        except Exception:
            _major = 0
        _hdr_kw = ('additional_headers' if _major >= 14 else 'extra_headers')
        _kwargs = dict(
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        if headers:
            _kwargs[_hdr_kw] = headers
        async with websockets.connect(self.url, **_kwargs) as ws:
            self._ws = ws
            self._connected = True
            logger.info(f"ws_client 已连接: {self.url}")
            # 冲刷连接前积压
            pending, self._pending = self._pending, []
            for item in pending:
                await ws.send(json.dumps(item, ensure_ascii=False))
            await self._recv_loop(ws)

    async def _recv_loop(self, ws):
        async for message in ws:
            if self._closing:
                break
            try:
                if isinstance(message, (bytes, bytearray)):
                    text = message.decode('utf-8', errors='replace')
                else:
                    text = message
                data = json.loads(text)
            except Exception:
                logger.debug("ws_client 忽略非 JSON 帧")
                continue
            # 远端 ack / pong 等控制帧不进事件流
            if isinstance(data, dict) and data.get('type') in (
                    'pong', 'ack', 'hello', 'ping'):
                if data.get('type') == 'ping':
                    await ws.send(json.dumps({'type': 'pong'}))
                continue
            event = normalize_event(data, self.bot_name)
            if event is None:
                continue
            try:
                await self.framework.dispatch_event(event)
            except Exception as e:
                logger.error(f"ws_client 事件分发失败: {e}")

    async def stop(self):
        self._closing = True
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        for task in (self._supervisor, self._recv_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._supervisor = None
        self._recv_task = None

    # ── ProtocolAdapter 契约 ────────────────────────────────────

    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        return normalize_event(raw_event, bot_name or self.bot_name)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_private_msg',
                      'send_text', 'send_forward_msg'):
            payload = {
                'action': 'send_msg',
                'user_id': params.get('user_id'),
                'group_id': params.get('group_id'),
                'message': normalize_message(
                    params.get('message', params.get('text', ''))),
                'bot': bot or self.bot_name,
            }
            # send_group_msg / send_private_msg 强制目标字段
            if action == 'send_group_msg':
                payload['group_id'] = params.get(
                    'group_id', payload.get('group_id'))
                payload['user_id'] = None
            elif action == 'send_private_msg':
                payload['user_id'] = params.get(
                    'user_id', payload.get('user_id'))
                payload['group_id'] = None
            ok = await self._send_json(payload)
            if not ok:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'ws_client 未连接或发送失败'}
            return {'status': 'ok', 'retcode': 0, 'data': None}

        if action in ('get_status', 'get_login_info', 'get_online_clients'):
            return {
                'status': 'ok', 'retcode': 0,
                'data': {
                    'online': self._connected,
                    'self_id': 0,
                    'nickname': self.bot_name,
                    'url': self.url,
                },
            }

        # 未知动作：透传给远端（若已连接）
        if self._connected and self._ws is not None:
            payload = {'action': action, 'bot': bot or self.bot_name, **params}
            # message 字段一并归一化 base64
            if 'message' in payload:
                payload['message'] = normalize_message(payload['message'])
            ok = await self._send_json(payload)
            if ok:
                return {'status': 'ok', 'retcode': 0, 'data': None}
            return {'status': 'failed', 'retcode': -1, 'msg': '发送失败'}
        return {'status': 'failed', 'retcode': -10,
                'msg': f'ws_client 不支持动作 {action}（未连接）'}

    async def send_text(self, text, *, user_id=None, group_id=None,
                        source=None) -> dict:
        return await self.call_api(
            'send_msg', bot=source, user_id=user_id, group_id=group_id,
            message=text)

    def get_connected_bots(self) -> list:
        return [self.bot_name] if self._connected else []

    # ── 内部发送 ────────────────────────────────────────────────

    async def _send_json(self, obj: dict) -> bool:
        data = json.dumps(obj, ensure_ascii=False)
        ws = self._ws
        if ws is None or not self._connected:
            # 未连上：入队，连上后由 _connect_once 冲刷（有界）
            if len(self._pending) < self.max_queue:
                self._pending.append(obj)
            return False
        try:
            await ws.send(data)
            return True
        except Exception as e:
            logger.warning(f"ws_client 发送失败: {e}")
            self._connected = False
            return False


# ── 注册入口 ──────────────────────────────────────────────────

_adapter_instance = None


def register(ctx):
    global _adapter_instance
    fw = ctx._framework

    # 段名与插件名相同（ws_client），enabled 缺省 False（避免误连外网）
    cfg = fw.config.get('ws_client', {})
    if isinstance(cfg, bool):
        # 兼容 core_plugins.ws_client: true/false 的旧开关写法
        if cfg is not True:
            ctx.log("出站 WS 接入端已禁用 (core_plugins.ws_client: false)")
            return
        cfg = {}
    if cfg.get('enabled') is not True:
        ctx.log("出站 WS 接入端已禁用 (ws_client.enabled: true 才开启)")
        return

    url = str(cfg.get('url') or '').strip()
    if not url or not url.startswith(('ws://', 'wss://')):
        ctx.log("出站 WS 未配置合法 url（ws:// 或 wss://），已跳过")
        return

    _adapter_instance = WsClientAdapter(
        fw,
        url=url,
        bot_name=cfg.get('bot_name') or 'ws_client',
        token=cfg.get('token') or '',
        reconnect_interval=cfg.get('reconnect_interval') or 5,
        max_queue=cfg.get('max_queue') or 256,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    _adapter_instance.start()
    ctx.log(f"出站 WS 接入端已启动: {url}"
            + ("（Token 认证）" if cfg.get('token') else ""))


def unregister():
    global _adapter_instance
    if _adapter_instance:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_adapter_instance.stop())
        _adapter_instance = None
