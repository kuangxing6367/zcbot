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
  token / bot_name / api_base / reconnect_interval / media_max_bytes

出站媒体依据（2026-11 核对 developer.kookapp.cn/doc/http/asset 与 http/message）：
  - POST /api/v3/asset/create，Header 必须 form-data、字段名 file，
    返回 data.url；官方原文明确「发送的图片必须由你的机器人上传，否则
    会失败并提示找不到资源」，此规则对视频同样适用。故本端对 http(s)
    外部地址会先下载再上传（受 media_max_bytes 限制）。

两处依据不足的地方，写清楚免得后人当成事实：
  - 消息 type 8 = 语音（audio）在官方文档里没有集中枚举页，只有 attachments
    的 kind=audio 说明与各 SDK 常量；本端按 8 放行，并且「放行后没解析出任何
    段落」时直接丢弃事件，不会造出空消息。
  - 出站没有语音消息类型，send_media 把 voice 段按文件(type 4)发送。
"""
import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional

import requests
import websockets
from websockets.exceptions import ConnectionClosed

from framework.messaging.backoff import Backoff
from framework.messaging.backoff import now as _mono
from framework.messaging.protocol import ProtocolAdapter
from framework.security.hardening import sanitize_adapter_event

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
#: 8 = 语音（audio）。官方消息类型表里它跟 4 是并列项，漏掉会让语音消息
#: 整条被丢弃——build_segments 里已经按 audio→voice 映射，这里必须放行。
_MSG_AUDIO = 8
_MSG_KMARKDOWN = 9
_MSG_CARD = 10

_HEARTBEAT_BASE = 30.0
_HEARTBEAT_JITTER = 5.0
_PONG_TIMEOUT = 6.0

#: 单次媒体上传的读取上限。官方 /api/v3/asset/create 未写明体积上限，
#: 这里只防「把整段视频读进内存把进程打死」，不当作平台限制声明。
_DEFAULT_MEDIA_MAX = 30 * 1024 * 1024


def _is_kook_cdn(url: str) -> bool:
    """该地址是否已是 KOOK 自家 CDN（消息里引用的媒体就是这种）。

    这类 url 直接发，不再下载重传——省一次上下行，也少一个失败点。
    """
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return False
    host = url.split('://', 1)[1].split('/', 1)[0].split('?', 1)[0]
    host = host.split(':', 1)[0].lower()
    return any(host == h or host.endswith('.' + h)
               for h in ('kookapp.cn', 'kaiheila.cn'))


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


def normalize_event(d: dict, bot_name: str, self_id: str = '') -> dict | None:
    """KOOK 消息体 → 内部事件 dict。"""
    if not isinstance(d, dict):
        return None
    # 只处理消息类事件：1/2/3/4/8/9/10 是消息，12 是道具，其余系统事件不进来
    msg_type = d.get('type')
    if msg_type not in (_MSG_TEXT, _MSG_IMAGE, _MSG_VIDEO, _MSG_FILE,
                        _MSG_AUDIO, _MSG_KMARKDOWN, _MSG_CARD):
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
    segs = build_segments(d)
    if not segs:
        # 白名单里的 type 但没有任何可用内容（例如只有附件的消息却没带 extra）
        # 不要往下做成一条空事件
        return None

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
        'segments': segs,
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
                 api_base: str = _API_BASE, reconnect_interval: int = 5,
                 media_max_bytes: int = _DEFAULT_MEDIA_MAX):
        self.framework = framework
        self.token = (token or '').strip()
        self.bot_name = bot_name or 'kook'
        self.api_base = (api_base or _API_BASE).rstrip('/')
        self.reconnect_interval = max(1, int(reconnect_interval or 5))
        self.media_max_bytes = max(1, int(media_max_bytes or _DEFAULT_MEDIA_MAX))
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
             json_body=None, files=None, data=None, timeout: int = 20) -> dict:
        """统一 REST 调用：把 KOOK 的业务码错误翻成框架约定的失败结构。

        KOOK 与 QQ 官方不同：它的 HTTP 状态码基本可信（401/403/429 都真实），
        但 body 里还有一层 code/message，两者都要看。
        files/data 用于 /api/v3/asset/create 的 form-data 上传；requests 不允许
        json= 与 files= 同时出现，两者互斥，这里显式择一。
        """
        url = f'{self.api_base}{path}'
        try:
            if files is not None or data is not None:
                resp = self._http.request(
                    method, url, params=params, files=files, data=data,
                    timeout=timeout)
            else:
                resp = self._http.request(
                    method, url, params=params, json=json_body,
                    timeout=timeout)
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

    # ── 媒体出站（asset/create）─────────────────────────────────

    def upload_asset(self, raw: bytes, name: str = 'file',
                     mime: str = '') -> dict:
        """POST /api/v3/asset/create 上传文件，返回 {'status','data':{'url'}}。

        官方要求 Content-Type 必须是 form-data、字段名 file，支持图片、
        视频(.mp4/.mov) 与普通文件；返回的 url 才能用来发消息。
        """
        if not raw:
            return {'status': 'failed', 'retcode': -1, 'msg': '上传内容为空'}
        return self._api(
            'POST', '/api/v3/asset/create',
            files={'file': (name or 'file', raw,
                            mime or 'application/octet-stream')},
            timeout=120)

    def _read_media_ref(self, ref: str, seg: dict):
        """媒体引用 → (bytes, 文件名, mime)。http(s) 远程地址也代下。

        和 Matrix 不同：KOOK 官方明确「发送的图片必须由你的机器人上传，
        否则会失败并提示找不到资源」，所以外部 URL 必须下载后重传，
        否则整条消息发不出去。下载受 media_max_bytes 限制。
        """
        import base64
        import re as _re
        data = seg.get('data') or {}
        name = str(data.get('name') or '')
        mime = str(data.get('mime') or '')
        try:
            m = _re.match(r'^base64://([A-Za-z0-9+/=\s]+)$', ref, _re.DOTALL)
            if m:
                # 先按 base64 长度估算解码后大小再解码：直接 b64decode 一段
                # 几百 MB 的串，峰值内存是原文 + 明文两份，且失败时已经吃满了。
                b64 = _re.sub(r'\s+', '', m.group(1))
                if len(b64) * 3 // 4 > self.media_max_bytes:
                    logger.warning(
                        'kook base64 媒体超过 %s 字节上限，拒绝解码',
                        self.media_max_bytes)
                    return b'', name, mime
                raw = base64.b64decode(b64)
                return raw, name or 'file', mime
            if ref.startswith(('http://', 'https://')):
                # 必须 with：requests 的流式响应不关闭会漏连接，重连几次后
                # 连接池就满了，后面的请求全卡在等待上。
                with self._http.get(ref, timeout=30, stream=True) as resp:
                    if resp.status_code >= 400:
                        logger.warning('kook 下载媒体失败 HTTP %s: %s',
                                       resp.status_code, ref[:100])
                        return b'', name, mime
                    buf = bytearray()
                    for chunk in resp.iter_content(64 * 1024):
                        buf.extend(chunk)
                        if len(buf) > self.media_max_bytes:
                            logger.warning(
                                'kook 媒体超过 %s 字节上限，放弃代下: %s',
                                self.media_max_bytes, ref[:100])
                            return b'', name, mime
                    if not mime:
                        mime = str(resp.headers.get('Content-Type') or '')
                return bytes(buf), name or os.path.basename(
                    ref.split('?')[0]) or 'file', mime
            path = ref[7:] if ref.startswith('file://') else ref
            if os.path.isfile(path):
                if os.path.getsize(path) > self.media_max_bytes:
                    logger.warning('kook 本地媒体超过上限，放弃上传: %s', path)
                    return b'', name, mime
                with open(path, 'rb') as f:
                    raw = f.read()
                return raw, name or os.path.basename(path) or 'file', mime
        except Exception as e:  # noqa: BLE001
            logger.warning('kook 读取媒体失败: %s', e)
        return b'', name, mime

    def send_media(self, channel_id: str, seg: dict,
                   quote: str = None) -> dict:
        """上传并发送一个媒体段（image / video / voice / file）。

        KOOK 一条消息只能有一种 type，所以媒体各自成条。
        语音没有对外发送的类型（type 8 只有入站语义），降级为文件发送。
        已经是 KOOK 自家 CDN 的地址不再重复上传（消息里引用的就是这种 url）。
        """
        from framework.messaging.segments import canonical_type, media_ref
        stype = canonical_type(seg.get('type'))
        msg_type = {'image': _MSG_IMAGE, 'video': _MSG_VIDEO,
                    'file': _MSG_FILE, 'voice': _MSG_FILE}.get(stype)
        if msg_type is None:
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'KOOK 不支持发送 {stype}'}
        ref = media_ref(seg)
        if not ref:
            return {'status': 'failed', 'retcode': -1, 'msg': '媒体段缺少引用'}
        if _is_kook_cdn(ref):
            return self._send_media_url(channel_id, msg_type, ref, quote)
        raw, name, mime = self._read_media_ref(ref, seg)
        if not raw:
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'KOOK 无法读取媒体引用（本地路径/base64/'
                           f'可下载的 http 地址）：{ref[:120]}'}
        up = self.upload_asset(raw, name, mime)
        if up.get('status') != 'ok':
            return up
        url = ((up.get('data') or {}) if isinstance(up.get('data'), dict)
               else {}).get('url')
        if not url:
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'KOOK 上传成功但响应里没有 url: {up}'}
        return self._send_media_url(channel_id, msg_type, url, quote)

    def _send_media_url(self, channel_id: str, msg_type: int, url: str,
                        quote: str = None) -> dict:
        """用已就绪的资源 url 发一条媒体消息。"""
        body = {'type': msg_type, 'target_id': str(channel_id),
                'content': str(url)}
        if quote:
            body['quote'] = str(quote)
        return self._api('POST', '/api/v3/message/create', json_body=body)


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
                # 入站统一净化：剥顶层注入字段、强制归属为当前接入端。
                event = sanitize_adapter_event(event, self.bot_name)
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
            CAP_FILE,
            CAP_IMAGE,
            CAP_TEXT,
            CAP_VIDEO,
            CAP_VOICE,
            Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VIDEO, CAP_VOICE, CAP_FILE],
            # 出站：图片/视频都必须先 POST /api/v3/asset/create 上传再引用 url
            # （官方原文：发送的图片必须由你的机器人上传，否则提示找不到资源），
            # 因此 send_media 会读本地/base64，并把 http 地址代下后重传。
            # 语音没有对外发送类型，按文件发。
            outbound=[CAP_TEXT, CAP_IMAGE, CAP_VIDEO, CAP_FILE],
        )

    def normalize_incoming(self, raw_message) -> list:
        if isinstance(raw_message, dict):
            return build_segments(raw_message)
        from framework.messaging.segments import normalize_message
        return normalize_message(raw_message)

    async def handle_event(self, raw_event: dict,
                           bot_name: str) -> dict | None:
        return normalize_event(raw_event, bot_name or self.bot_name,
                               self._self_id)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_text'):
            channel_id = (params.get('channel_id') or params.get('group_id'))
            if not channel_id:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'KOOK 发送需要 group_id/channel_id（频道 id）'}
            from framework.messaging.segments import (
                canonical_type,
            )
            from framework.messaging.segments import (
                normalize_message as _canon,
            )
            segs = _canon(params.get('message', params.get('text', '')))
            text = ''.join(str((s.get('data') or {}).get('text') or '')
                           for s in segs
                           if canonical_type(s.get('type')) == 'text')
            medias = [s for s in segs
                      if canonical_type(s.get('type')) in
                      ('image', 'video', 'voice', 'file')]
            quote = params.get('reply_to') or params.get('quote')
            last = None
            if not text and not medias:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'KOOK 发送内容为空'}
            # 一条 KOOK 消息只有一种 type：文本先发，媒体随后各自成条
            if text:
                body = {'type': _MSG_KMARKDOWN, 'target_id': str(channel_id),
                        'content': text}
                if quote:
                    body['quote'] = str(quote)
                last = await asyncio.to_thread(
                    self._api, 'POST', '/api/v3/message/create',
                    json_body=body)
                if last.get('status') != 'ok':
                    return last
            for seg in medias:
                last = await asyncio.to_thread(
                    self.send_media, str(channel_id), seg, None)
                if last.get('status') != 'ok':
                    return last
            return last or {'status': 'ok', 'retcode': 0, 'data': None}

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
        media_max_bytes=cfg.get('media_max_bytes') or _DEFAULT_MEDIA_MAX,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    if _adapter_instance.token:
        _adapter_instance.start()
        ctx.log('KOOK 接入端已启动')
    else:
        ctx.log('KOOK 接入端缺少 token，已跳过连接')
