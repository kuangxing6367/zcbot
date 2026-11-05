# -*- coding: utf-8 -*-
"""
KOOK 接入端（kook）

Gateway WebSocket 收事件 + REST v3 发消息（仅用 websockets + requests）。

协议依据（官方文档 developer.kookapp.cn，2026-11 核对）：
  - 网关地址走 HTTP 取：GET /api/v3/gateway/index?compress=0
    compress 默认是 1（zlib），本端固定传 0 拿明文，少一层解压逻辑与失败点。
  - 信令：s=0 事件（带 sn）/ 1 HELLO / 2 PING / 3 PONG / 4 RESUME /
    5 RECONNECT / 6 RESUME ACK。客户端只发 2 和 4。
  - 心跳：每 30s ± 5s 发一次 {"s":2,"sn":<已处理最大 sn>}，
    6 秒内没收到 PONG 视为超时。官方文档明确写了这个判定。
  - 断线恢复：在 gateway url 后拼 resume=1&sn=&session_id= 重连，
    成功后服务端补发离线消息，最后给 RESUME ACK。
  - 收到 RECONNECT(s=5) 必须清空 sn 与本地队列、重新取 gateway，
    否则会消息错乱。

配置（core_plugins.yaml → kook，默认 enabled: false）：
  token / bot_name / api_base / reconnect_interval
"""
import asyncio
import json
import logging
import threading
import time
from typing import Optional

import requests
import websockets
from websockets.exceptions import ConnectionClosed

from framework.messaging.protocol import ProtocolAdapter
from framework.messaging.backoff import Backoff, now as _mono

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "kook",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "KOOK 接入端：Gateway WebSocket + REST v3 收发",
    "priority": 7,
    "official": True,
    "process": "core",
}

_API_BASE = 'https://www.kookapp.cn'

# 信令
_S_EVENT = 0
_S_HELLO = 1
_S_PING = 2
_S_PONG = 3
_S_RESUME = 4
_S_RECONNECT = 5
_S_RESUME_ACK = 6

# HELLO 失败码：都是配置/凭证问题，重连不会自愈
_HELLO_FATAL = {
    40100: '缺少参数',
    40101: 'token 无效',
    40102: 'token 验证失败',
    40103: 'token 已过期，请到开发者后台重新复制',
}

# 消息类型
_MSG_TEXT = 1
_MSG_IMAGE = 2
_MSG_VIDEO = 3
_MSG_FILE = 4
_MSG_KMARKDOWN = 9
_MSG_CARD = 10

_HEARTBEAT_BASE = 30.0
_HEARTBEAT_JITTER = 5.0
_PONG_TIMEOUT = 6.0


def _safe_text(content) -> str:
    """KOOK 的 content 对卡片/道具消息可能是 dict，统一转成字符串。"""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return ''
    return str(content)


def build_segments(d: dict) -> list:
    """KOOK 消息体 → 规范消息段。"""
    segs = []
    content = _safe_text(d.get('content'))
    if content:
        segs.append({'type': 'text', 'data': {'text': content}})
    extra = d.get('extra') or {}
    att = extra.get('attachments')
    if isinstance(att, dict) and att.get('url'):
        kind = str(att.get('type') or '')
        seg_type = {'image': 'image', 'video': 'video',
                    'file': 'file', 'audio': 'voice'}.get(kind, 'file')
        data = {
            'url': att.get('url'),
            'file_id': att.get('url'),
            'name': att.get('name'),
            'size': att.get('size'),
            'mime': att.get('file_type'),
        }
        segs.append({'type': seg_type, 'data': data})
    return segs


def normalize_event(d: dict, bot_name: str, self_id: str = '') -> Optional[dict]:
    """KOOK 消息体 → 内部事件 dict。"""
    if not isinstance(d, dict):
        return None
    # 只处理消息类事件：1/2/3/4/9/10 是消息，12 是道具，其余系统事件不进来
    msg_type = d.get('type')
    if msg_type not in (_MSG_TEXT, _MSG_IMAGE, _MSG_VIDEO, _MSG_FILE,
                        _MSG_KMARKDOWN, _MSG_CARD):
        return None
    extra = d.get('extra') or {}
    author = extra.get('author') or {}
    # 官方文档明确警告：不过滤机器人消息会形成两个机器人互相回复的死循环
    if author.get('bot'):
        return None
    author_id = str(d.get('author_id') or '')
    if self_id and author_id == str(self_id):
        return None

    channel_type = str(d.get('channel_type') or '')
    target_id = str(d.get('target_id') or '')
    text = _safe_text(d.get('content'))
    is_group = channel_type == 'GROUP'

    return {
        'type': 'message',
        'message_type': 'group' if is_group else 'private',
        'sub_type': '',
        'bot_name': bot_name,
        'user_id': author_id,
        'group_id': target_id if is_group else None,
        'message_id': d.get('msg_id'),
        'message': text,
        'raw_message': text,
        'segments': build_segments(d),
        'sender': {
            'user_id': author_id,
            'nickname': author.get('nickname') or author.get('username')
            or author_id,
        },
        'adapter': 'kook',
        'channel_id': target_id,
        'guild_id': extra.get('guild_id') or '',
        'raw': d,
    }


class KookAdapter(ProtocolAdapter):
    """KOOK 协议接入端。"""

    def __init__(self, framework, token: str = '', bot_name: str = 'kook',
                 api_base: str = _API_BASE, reconnect_interval: int = 5):
        self.framework = framework
        self.token = (token or '').strip()
        self.bot_name = bot_name or 'kook'
        self.api_base = (api_base or _API_BASE).rstrip('/')
        self.reconnect_interval = max(1, int(reconnect_interval or 5))
        self._backoff = Backoff(base=self.reconnect_interval, maximum=60)

        self._ws = None
        self._connected = False
        self._closing = False
        self._supervisor = None
        self._heartbeat_task = None
        self._session_id = ''
        self._sn = 0
        self._self_id = ''
        self._pong_event = None
        self._fatal_reason = ''
        self._loop = None
        self._http = requests.Session()
        self._http.headers.update({
            'Authorization': f'Bot {self.token}',
            'User-Agent': 'ZCBOT (kook adapter, 1.0)',
        })

    # ── 连接自描述 ──────────────────────────────────────────────

    def get_connection_info(self) -> dict:
        return {
            'id': 'kook',
            'name': 'KOOK',
            'config_section': 'kook',
            'fields': [
                {'key': 'token', 'label': 'Bot Token', 'type': 'password',
                 'required': True},
                {'key': 'bot_name', 'label': 'Bot 名', 'type': 'string'},
                {'key': 'api_base', 'label': 'API 地址', 'type': 'string'},
                {'key': 'reconnect_interval', 'label': '重连基础间隔(秒)',
                 'type': 'number'},
            ],
            'restart_keys': ['token', 'bot_name'],
            'endpoint_hint': 'wss://（由 /api/v3/gateway/index 下发）',
            'guide': '在 KOOK 开发者后台创建应用 → 机器人 → 复制 Token 填入；'
                     '开启「WebSocket」模式（与 Webhook 互斥）；'
                     '机器人需邀请进服务器并给频道发言权限。',
            'status': {
                'connected': self._connected,
                'session': bool(self._session_id),
                'sn': self._sn,
                'fatal': self._fatal_reason,
            },
        }

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        self._fatal_reason = ''
        self._schedule_connect()

    def _schedule_connect(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            if not self._closing:
                t = threading.Timer(0.5, self._schedule_connect)
                t.daemon = True
                t.start()
            return
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = loop.create_task(self._supervise())

    async def _supervise(self):
        while not self._closing:
            started = _mono()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning('kook 连接异常: %s', e)
            finally:
                self._connected = False
                self._ws = None
                await self._cancel_heartbeat()
            if self._closing:
                break
            if self._fatal_reason:
                logger.error('kook 接入端已停止重连：%s', self._fatal_reason)
                break
            await asyncio.sleep(self._backoff.next_delay(_mono() - started))

    async def _cancel_heartbeat(self):
        t = self._heartbeat_task
        self._heartbeat_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ── REST ────────────────────────────────────────────────────

    def _fetch_gateway(self) -> str:
        resp = self._http.get(
            f'{self.api_base}/api/v3/gateway/index',
            params={'compress': 0}, timeout=15)
        data = resp.json() if resp.content else {}
        if resp.status_code == 401 or data.get('code') in (401, 40101, 40102):
            raise RuntimeError('KOOK token 无效（HTTP 401），请重新复制 Bot Token')
        if data.get('code') != 0:
            raise RuntimeError(
                f'获取 KOOK gateway 失败: code={data.get("code")} '
                f'{data.get("message") or ""}')
        url = (data.get('data') or {}).get('url') or ''
        if not url:
            raise RuntimeError(f'KOOK gateway 返回空地址: {data}')
        return url

    def _api(self, method: str, path: str, *, params=None,
             json_body=None, timeout: int = 20) -> dict:
        """统一 REST 调用：把 KOOK 的业务码错误翻成框架约定的失败结构。

        KOOK 与 QQ 官方不同：它的 HTTP 状态码基本可信（401/403/429 都真实），
        但 body 里还有一层 code/message，两者都要看。
        """
        url = f'{self.api_base}{path}'
        try:
            resp = self._http.request(
                method, url, params=params, json=json_body, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return {'status': 'failed', 'retcode': -3,
                    'msg': f'KOOK 请求失败: {type(e).__name__} {e}'}
        try:
            body = resp.json() if resp.content else {}
        except Exception:  # noqa: BLE001
            body = {'_text': (resp.text or '')[:300]}

        if resp.status_code == 429:
            ra = resp.headers.get('Retry-After')
            try:
                ra = int(float(ra)) if ra is not None else None
            except (TypeError, ValueError):
                ra = None
            return {'status': 'failed', 'retcode': -4, 'throttled': True,
                    'retry_after': ra,
                    'msg': f'KOOK 限流，建议等待 {ra or "?"}s'}
        if resp.status_code == 401:
            return {'status': 'failed', 'retcode': -1,
                    'http_status': 401,
                    'msg': 'KOOK 鉴权失败（401）：token 无效或已被重置'}
        if resp.status_code == 403:
            return {'status': 'failed', 'retcode': -1,
                    'http_status': 403,
                    'msg': 'KOOK 权限不足（403）：机器人缺少该频道发言/管理权限'}

        code = body.get('code') if isinstance(body, dict) else None
        if code not in (0, None, '0'):
            return {'status': 'failed', 'retcode': -1, 'error_code': code,
                    'http_status': resp.status_code,
                    'msg': f'KOOK 业务错误 code={code}: '
                           f'{body.get("message") or body}'}
        if resp.status_code >= 400:
            return {'status': 'failed', 'retcode': -1,
                    'http_status': resp.status_code, 'msg': str(body)}
        return {'status': 'ok', 'retcode': 0, 'data': body.get('data', body)}

    # ── WebSocket ───────────────────────────────────────────────

    def _ws_url(self, gateway: str) -> str:
        if not self._session_id:
            return gateway
        sep = '&' if '?' in gateway else '?'
        return (f'{gateway}{sep}resume=1&sn={self._sn}'
                f'&session_id={self._session_id}')

    async def _connect_once(self):
        gateway = await asyncio.to_thread(self._fetch_gateway)
        url = self._ws_url(gateway)
        try:
            async with websockets.connect(
                url, max_size=8 * 1024 * 1024, ping_interval=None,
            ) as ws:
                self._ws = ws
                self._pong_event = asyncio.Event()
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(ws))
                await self._recv_loop(ws)
        except ConnectionClosed as e:
            # 正常关闭也会走到这里，交给 supervisor 决定是否 resume
            logger.debug('kook 连接关闭 code=%s', getattr(e, 'code', None))

    async def _heartbeat_loop(self, ws):
        while not self._closing:
            # 官方要求 30s ± 5s 随机，避免所有客户端同一秒打心跳
            import random
            await asyncio.sleep(
                _HEARTBEAT_BASE + random.uniform(-_HEARTBEAT_JITTER,
                                                 _HEARTBEAT_JITTER))
            if self._closing:
                return
            self._pong_event.clear()
            try:
                await ws.send(json.dumps({'s': _S_PING, 'sn': self._sn}))
            except Exception:  # noqa: BLE001
                return
            try:
                await asyncio.wait_for(self._pong_event.wait(),
                                       timeout=_PONG_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning('kook 心跳超时（%.0fs 内无 PONG），断开重连',
                               _PONG_TIMEOUT)
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return

    async def _recv_loop(self, ws):
        async for message in ws:
            if self._closing:
                break
            try:
                text = (message.decode('utf-8', errors='replace')
                        if isinstance(message, (bytes, bytearray)) else message)
                payload = json.loads(text)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(payload, dict):
                continue
            signal = payload.get('s')
            d = payload.get('d')
            if signal == _S_PONG:
                if self._pong_event is not None:
                    self._pong_event.set()
                continue
            if signal == _S_HELLO:
                code = (d or {}).get('code')
                if code not in (0, None):
                    reason = _HELLO_FATAL.get(code, f'未知握手错误 code={code}')
                    self._fatal_reason = f'{code}: {reason}'
                    logger.error('kook 握手失败：%s（%s）', reason, code)
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                old = self._session_id
                self._session_id = (d or {}).get('session_id') or ''
                self._connected = True
                if old and old == self._session_id:
                    logger.info('kook 会话已恢复 (sn=%s)', self._sn)
                else:
                    self._sn = 0 if not old else self._sn
                    logger.info('kook 已连接 (session=%s)', self._session_id)
                continue
            if signal == _S_RESUME_ACK:
                new_sid = (d or {}).get('session_id')
                if new_sid:
                    self._session_id = new_sid
                logger.info('kook RESUME 完成，离线消息已补齐 (sn=%s)', self._sn)
                continue
            if signal == _S_RECONNECT:
                code = (d or {}).get('code')
                logger.warning('kook 收到 RECONNECT (code=%s)：清空 sn 并重连', code)
                # 官方要求：清 sn、清本地队列、重新取 gateway
                self._sn = 0
                self._session_id = ''
                break
            if signal != _S_EVENT:
                continue
            if not self._accept_sn(payload.get('sn')):
                continue
            if isinstance(d, dict):
                d = dict(d)
                d['_self_id'] = self._self_id
                event = normalize_event(d, self.bot_name, self._self_id)
                if event is None:
                    continue
                try:
                    await self.framework.dispatch_event(event)
                except Exception as e:  # noqa: BLE001
                    logger.error('kook 事件分发失败: %s', e)

    def _accept_sn(self, sn) -> bool:
        """推进 sn 并判断是否该处理这条事件。

        官方要求：sn 必须按序处理，重复的（sn <= 已处理最大值）直接丢弃。
        不丢弃会出现同一条消息被执行两次 —— 对「签到」「扣费」这类命令是灾难。
        """
        if not isinstance(sn, int):
            return True
        if sn <= self._sn:
            logger.debug('kook 丢弃重复 sn=%s（已处理到 %s）', sn, self._sn)
            return False
        self._sn = sn
        return True

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_FILE, CAP_IMAGE, CAP_TEXT, CAP_VIDEO, Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VIDEO, CAP_FILE],
            # KOOK 的图片/视频必须由本机器人上传后引用，外部 URL 会被拒；
            # 出站这里只声明文本，避免插件以为能直接发链接图。
            outbound=[CAP_TEXT],
        )

    def normalize_incoming(self, raw_message) -> list:
        if isinstance(raw_message, dict):
            return build_segments(raw_message)
        from framework.messaging.segments import normalize_message
        return normalize_message(raw_message)

    async def handle_event(self, raw_event: dict,
                           bot_name: str) -> Optional[dict]:
        return normalize_event(raw_event, bot_name or self.bot_name,
                               self._self_id)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_text'):
            channel_id = (params.get('channel_id') or params.get('group_id'))
            text = params.get('message', params.get('text', ''))
            if not channel_id:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'KOOK 发送需要 group_id/channel_id（频道 id）'}
            body = {'type': _MSG_KMARKDOWN, 'target_id': str(channel_id),
                    'content': str(text)}
            quote = params.get('reply_to') or params.get('quote')
            if quote:
                body['quote'] = str(quote)
            return await asyncio.to_thread(
                self._api, 'POST', '/api/v3/message/create', json_body=body)

        if action == 'send_private_msg':
            # KOOK 私聊（direct-message）需要 chat_code，只能从私聊事件里拿；
            # 这里不猜，直接说明缺什么，避免调用方以为发出去了。
            return {'status': 'failed', 'retcode': -10,
                    'msg': 'KOOK 私聊需要 chat_code（来自私聊事件），'
                           '本端暂未支持主动私聊'}

        if action in ('get_status', 'get_login_info'):
            return {'status': 'ok', 'retcode': 0,
                    'data': {'online': self._connected,
                             'self_id': self._self_id,
                             'nickname': self.bot_name,
                             'session': self._session_id}}

        if action == 'delete_msg':
            msg_id = params.get('message_id')
            if not msg_id:
                return {'status': 'failed', 'retcode': -1, 'msg': '需要 message_id'}
            return await asyncio.to_thread(
                self._api, 'POST', '/api/v3/message/delete',
                json_body={'msg_id': str(msg_id)})

        return {'status': 'failed', 'retcode': -10,
                'msg': f'kook 不支持动作 {action}'}

    async def send_text(self, text, *, user_id=None, group_id=None,
                        source=None) -> dict:
        return await self.call_api(
            'send_msg', bot=source, user_id=user_id, group_id=group_id,
            message=text)

    def get_connected_bots(self) -> list:
        return [self.bot_name] if self._connected else []

    async def stop(self):
        self._closing = True
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        await self._cancel_heartbeat()
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
        self._supervisor = None
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass


# ── 注册入口 ──────────────────────────────────────────────────

_adapter_instance = None


def register(ctx):
    global _adapter_instance
    fw = ctx._framework
    cfg = fw.config.get('kook', {})
    if isinstance(cfg, bool):
        if cfg is not True:
            ctx.log('KOOK 接入端已禁用 (kook.enabled: true 才开启)')
            return
        cfg = {}
    if cfg.get('enabled') is not True:
        ctx.log('KOOK 接入端已禁用 (kook.enabled: true 才开启)')
        return
    _adapter_instance = KookAdapter(
        fw,
        token=cfg.get('token') or '',
        bot_name=cfg.get('bot_name') or 'kook',
        api_base=cfg.get('api_base') or _API_BASE,
        reconnect_interval=cfg.get('reconnect_interval') or 5,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    if _adapter_instance.token:
        _adapter_instance.start()
        ctx.log('KOOK 接入端已启动')
    else:
        ctx.log('KOOK 接入端缺少 token，已跳过连接')
