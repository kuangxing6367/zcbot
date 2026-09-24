# -*- coding: utf-8 -*-
"""
QQ 官方机器人接入端（qq_official）

走 QQ 开放平台官方 OpenAPI + WSS 网关（不依赖 botpy，仅用 websockets + requests）：
  1. getAppAccessToken 获取 access_token（7200s，自动刷新）
  2. GET /gateway/bot 取 WSS 地址，Hello → Identify → 心跳 → Dispatch
  3. 群/单聊事件归一化为内部事件 dict；出站 send_msg 翻回 OpenAPI

消息约定（内部）：
  - 群：message_type=group，group_id=group_openid，user_id=member_openid
  - 单聊：message_type=private，user_id=user_openid
  - 出站图片：base64:// / file:// / http(s) / 本地路径 → 群文件上传 → msg_type=7

配置（core_plugins.yaml → qq_official，默认 enabled: false）：
  app_id / app_secret / bot_name / intents / reconnect_interval
"""
import asyncio
import base64
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests
import websockets

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "qq_official",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "QQ 官方机器人接入端：WSS 网关 + OpenAPI 收发",
    "priority": 5,
    "official": True,
    "process": "core",
}

_API_BASE = 'https://api.bot.qq.com'
_TOKEN_URL = f'{_API_BASE}/app/getAppAccessToken'
_GATEWAY_URL = f'{_API_BASE}/gateway/bot'

# GROUP_AND_C2C_EVENT：群 @/全量 + 单聊
_DEFAULT_INTENTS = 1 << 25

_B64_RE = re.compile(r'^base64://([A-Za-z0-9+/=\s]+)$', re.DOTALL)
_CQ_IMAGE_RE = re.compile(
    r'\[CQ:image,file=([^\],]+)(?:,([^\]]*))?\]', re.IGNORECASE)


def _extract_b64(data: str) -> Optional[str]:
    if not isinstance(data, str):
        return None
    m = _B64_RE.match(data.strip())
    if m:
        return re.sub(r'\s+', '', m.group(1))
    s = re.sub(r'\s+', '', data)
    if len(s) >= 16 and len(s) % 4 == 0 and re.fullmatch(r'[A-Za-z0-9+/=]+', s):
        try:
            base64.b64decode(s, validate=True)
            return s
        except Exception:
            return None
    return None


def _b64_to_bytes(b64: str) -> Optional[bytes]:
    try:
        return base64.b64decode(re.sub(r'\s+', '', b64))
    except Exception:
        return None


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/png'


def normalize_message(message):
    """出站消息归一化：CQ 码 / 消息段里的 base64 图片统一抽出。"""
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

    if 'base64://' not in message and not _CQ_IMAGE_RE.search(message):
        return message

    stripped = message.strip()
    if stripped.startswith('base64://'):
        b64 = _extract_b64(stripped)
        if b64:
            return [{'type': 'image',
                     'data': {'file': f'base64://{b64}', 'base64': b64}}]

    parts = []
    last = 0
    for m in _CQ_IMAGE_RE.finditer(message):
        head = message[last:m.start()]
        if head:
            parts.append({'type': 'text', 'data': {'text': head}})
        file = m.group(1)
        b64 = _extract_b64(file)
        img = {'file': f'base64://{b64}', 'base64': b64} if b64 else {'file': file}
        if not b64 and m.group(2):
            for kv in m.group(2).split(','):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    img.setdefault(k.strip(), v.strip())
        parts.append({'type': 'image', 'data': img})
        last = m.end()
    tail = message[last:]
    if tail:
        parts.append({'type': 'text', 'data': {'text': tail}})
    return parts or message


def _message_to_text(message) -> str:
    """把出站 message 压成纯文本（QQ 富媒体另走 media）。"""
    if message is None:
        return ''
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        texts = []
        for seg in message:
            if not isinstance(seg, dict):
                texts.append(str(seg))
                continue
            if seg.get('type') in ('text', 'at', 'node'):
                data = seg.get('data') or {}
                t = data.get('text') or data.get('name') or ''
                if t:
                    texts.append(str(t))
        return ''.join(texts)
    return str(message)


def _first_image(message) -> Optional[dict]:
    """取出第一个 image 段（含 CQ 码转段后的）。"""
    if isinstance(message, str):
        message = normalize_message(message)
    if not isinstance(message, list):
        return None
    for seg in message:
        if isinstance(seg, dict) and seg.get('type') == 'image':
            return seg.get('data') or {}
    return None


def normalize_event(raw: dict, bot_name: str) -> Optional[dict]:
    """QQ 官方 Dispatch d → 框架内部事件 dict。"""
    if not isinstance(raw, dict):
        return None
    # 上层传入的可能是完整 payload 或已拆出的 d
    event_t = raw.get('_t') or raw.get('t')
    d = raw.get('d') if isinstance(raw.get('d'), dict) else raw
    if event_t in (None, '', 'READY', 'RESUMED'):
        if event_t in ('READY', 'RESUMED'):
            return None
        # 纯 d 且无事件名：尝试按消息字段识别
        if not (d.get('group_openid') or d.get('content') is not None
                or (d.get('author') or {}).get('id')):
            return None

    author = d.get('author') or {}
    content = d.get('content') or ''
    attachments = d.get('attachments') or []
    msg_id = d.get('id') or ''
    group_openid = d.get('group_openid') or ''
    user_openid = author.get('user_openid') or author.get('id') or ''
    member_openid = author.get('member_openid') or author.get('id') or ''

    # 附件补进文本提示
    if attachments:
        urls = []
        for att in attachments:
            if isinstance(att, dict) and att.get('url'):
                urls.append(att['url'])
        if urls:
            content = (content + '\n' if content else '') + '\n'.join(urls)

    # 频道消息：channel_id 当群
    channel_id = d.get('channel_id') or ''
    guild_id = d.get('guild_id') or ''

    if group_openid or channel_id:
        message_type = 'group'
        group_id = group_openid or channel_id
        user_id = member_openid or user_openid
    else:
        message_type = 'private'
        group_id = None
        user_id = user_openid or member_openid

    event = {
        'type': 'message',
        'message_type': message_type,
        'sub_type': '',
        'bot_name': bot_name,
        'user_id': user_id,
        'group_id': group_id,
        'message_id': msg_id,
        'message': content,
        'raw_message': content,
        'sender': {
            'user_id': user_id,
            'nickname': author.get('username') or str(user_id),
            'role': author.get('member_role') or '',
        },
        'adapter': 'qq_official',
        # 供被动回复：原消息 id / 事件 id
        'reply_msg_id': msg_id,
        'event_id': raw.get('id') or d.get('event_id') or '',
        'guild_id': guild_id,
        'raw': d,
    }
    if event_t:
        event['qq_event'] = event_t
    return event


class QQOfficialAdapter(ProtocolAdapter):
    """QQ 官方机器人：WSS 收事件 + OpenAPI 发消息"""

    adapter_id = 'qq_official'

    def __init__(self, framework, app_id: str, app_secret: str,
                 bot_name: str = 'qq_official', intents: int = None,
                 reconnect_interval: int = 5, api_base: str = _API_BASE):
        self.framework = framework
        self.app_id = str(app_id or '').strip()
        self.app_secret = str(app_secret or '').strip()
        self.bot_name = bot_name or 'qq_official'
        self.intents = int(intents) if intents else _DEFAULT_INTENTS
        self.reconnect_interval = max(1, int(reconnect_interval or 5))
        self.api_base = (api_base or _API_BASE).rstrip('/')

        self._access_token = ''
        self._token_expire_at = 0.0
        self._ws = None
        self._connected = False
        self._closing = False
        self._supervisor = None
        self._heartbeat_task = None
        self._seq = None
        self._session_id = ''
        self._heartbeat_interval_ms = 41250
        self._last_msg_id = ''          # 最近入站消息 id（被动回复）
        self._pending_reply = {}        # {(group|user): (msg_id, ts)}
        self._loop = None
        self._http = requests.Session()

    # ── 连接自描述 ──────────────────────────────────────────────

    def get_connection_info(self) -> dict:
        hint = f"wss://…/websocket/（app_id: {self.app_id or '未配置'}）"
        return {
            'id': 'qq_official',
            'name': 'QQ 官方机器人',
            'config_section': 'qq_official',
            'fields': [
                {'key': 'app_id', 'label': 'AppID', 'type': 'string'},
                {'key': 'app_secret', 'label': 'AppSecret', 'type': 'password'},
                {'key': 'bot_name', 'label': 'Bot 名', 'type': 'string'},
                {'key': 'intents', 'label': 'Intents', 'type': 'number'},
                {'key': 'reconnect_interval', 'label': '重连间隔(秒)', 'type': 'number'},
            ],
            'restart_keys': ['app_id', 'app_secret', 'bot_name', 'intents'],
            'endpoint_hint': hint,
            'guide': 'QQ 开放平台创建机器人后填写 AppID/AppSecret；'
                     '本端经 getAppAccessToken + /gateway/bot 建立 WSS，'
                     '群消息走 group_openid、单聊走 user_openid。'
                     '出站图片支持 base64://（先传群文件再 msg_type=7）。',
            'status_extra': {
                'connected': self._connected,
                'app_id': self.app_id,
                'intents': self.intents,
                'token_valid': bool(self._access_token and
                                    time.time() < self._token_expire_at),
            },
        }

    # ── token / gateway ────────────────────────────────────────

    async def _ensure_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expire_at - 60:
            return self._access_token
        payload = {'appId': self.app_id, 'clientSecret': self.app_secret}
        resp = await asyncio.to_thread(
            self._http.post, _TOKEN_URL, json=payload, timeout=15)
        data = resp.json() if resp.content else {}
        token = data.get('access_token') or data.get('accessToken') or ''
        if not token:
            raise RuntimeError(f'获取 access_token 失败: HTTP {resp.status_code} {data}')
        expires = int(data.get('expires_in') or data.get('expiresIn') or 7200)
        self._access_token = token
        self._token_expire_at = now + max(60, expires)
        logger.info('qq_official access_token 已刷新，有效期 %ss', expires)
        return token

    async def _fetch_gateway(self) -> str:
        token = await self._ensure_token()
        headers = {'Authorization': f'QQBot {token}'}
        resp = await asyncio.to_thread(
            self._http.get, f'{self.api_base}/gateway/bot',
            headers=headers, timeout=15)
        data = resp.json() if resp.content else {}
        url = data.get('url') or data.get('wss') or ''
        if not url:
            raise RuntimeError(f'获取 gateway 失败: HTTP {resp.status_code} {data}')
        if '://' not in url:
            url = f'wss://{url}'
        if not url.startswith(('ws://', 'wss://')):
            url = 'wss://' + url.lstrip('/')
        # 去掉可能的 http(s) 前缀统一成 wss
        if url.startswith('https://'):
            url = 'wss://' + url[8:]
        elif url.startswith('http://'):
            url = 'ws://' + url[7:]
        return url

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        self._schedule_connect()

    def _schedule_connect(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            try:
                asyncio.get_event_loop().call_soon(self._schedule_connect)
            except Exception:
                pass
            return
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = loop.create_task(self._supervise())

    async def _supervise(self):
        while not self._closing:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning('qq_official 连接异常: %s', e)
            finally:
                self._connected = False
                self._ws = None
                await self._cancel_heartbeat()
            if self._closing:
                break
            await asyncio.sleep(self.reconnect_interval)

    async def _cancel_heartbeat(self):
        t = self._heartbeat_task
        self._heartbeat_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _connect_once(self):
        if not self.app_id or not self.app_secret:
            raise RuntimeError('未配置 app_id / app_secret')
        url = await self._fetch_gateway()
        token = self._access_token
        identify_token = f'Bot {self.app_id}.{token}'
        async with websockets.connect(
            url, max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20,
        ) as ws:
            self._ws = ws
            # Hello (op 10)
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            hello = json.loads(raw)
            interval = 41250
            if isinstance(hello, dict) and hello.get('op') == 10:
                d = hello.get('d') or {}
                interval = int(d.get('heartbeat_interval') or interval)
            self._heartbeat_interval_ms = max(1000, interval)

            # Identify (op 2) — 有 session 则 Resume (op 6)
            if self._session_id and self._seq is not None:
                identify = {
                    'op': 6,
                    'd': {
                        'token': identify_token,
                        'session_id': self._session_id,
                        'seq': self._seq,
                    },
                }
            else:
                identify = {
                    'op': 2,
                    'd': {
                        'token': identify_token,
                        'intents': self.intents,
                        'shard': [0, 1],
                        'properties': {
                            '$os': 'windows',
                            '$browser': 'zcbot',
                            '$device': 'zcbot',
                        },
                    },
                }
            await ws.send(json.dumps(identify, ensure_ascii=False))

            self._connected = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            logger.info('qq_official 已连接: %s (intents=%s)',
                        url, self.intents)
            await self._recv_loop(ws)

    async def _heartbeat_loop(self, ws):
        # 首跳按规范 jitter（半周期）
        await asyncio.sleep(self._heartbeat_interval_ms / 2000.0)
        while self._connected and not self._closing:
            try:
                await ws.send(json.dumps({'op': 1, 'd': self._seq}))
            except Exception:
                return
            await asyncio.sleep(self._heartbeat_interval_ms / 1000.0)

    async def _recv_loop(self, ws):
        async for message in ws:
            if self._closing:
                break
            try:
                text = message.decode('utf-8', errors='replace') \
                    if isinstance(message, (bytes, bytearray)) else message
                payload = json.loads(text)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            op = payload.get('op')
            if op == 11:  # heartbeat ack
                continue
            if op == 1:   # server heartbeat
                try:
                    await ws.send(json.dumps({'op': 1, 'd': self._seq}))
                except Exception:
                    pass
                continue
            if op == 7:   # reconnect
                logger.info('qq_official 网关要求重连')
                break
            if op == 9:   # invalid session
                logger.warning('qq_official session 失效，将重新 Identify')
                self._session_id = ''
                self._seq = None
                break
            if op != 0:
                continue
            seq = payload.get('s')
            if seq is not None:
                self._seq = seq
            event_t = payload.get('t') or ''
            d = payload.get('d')
            if event_t == 'READY' and isinstance(d, dict):
                self._session_id = d.get('session_id') or ''
                user = d.get('user') or {}
                logger.info('qq_official READY: %s',
                            user.get('username') or user.get('id'))
                continue
            if event_t == 'RESUMED':
                logger.info('qq_official RESUMED (seq=%s)', self._seq)
                continue
            if not isinstance(d, dict):
                continue
            # 仅处理消息类事件
            if event_t not in (
                    'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE',
                    'C2C_MESSAGE_CREATE', 'MESSAGE_CREATE'):
                continue
            wrapped = dict(d)
            wrapped['_t'] = event_t
            wrapped['id'] = d.get('id') or payload.get('id') or ''
            # 被动回复缓存
            key = d.get('group_openid') or d.get('channel_id') \
                or (d.get('author') or {}).get('user_openid') or ''
            if key and d.get('id'):
                self._pending_reply[key] = (d['id'], time.time())
                self._last_msg_id = d['id']
            event = normalize_event(wrapped, self.bot_name)
            if event is None:
                continue
            try:
                await self.framework.dispatch_event(event)
            except Exception as e:
                logger.error('qq_official 事件分发失败: %s', e)

    async def stop(self):
        self._closing = True
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
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
        except Exception:
            pass

    # ── OpenAPI 出站 ───────────────────────────────────────────

    def _reply_msg_id(self, group_id=None, user_id=None) -> str:
        now = time.time()
        # 清理过期（群 5 分钟 / 单聊 60 分钟，统一按 5 分钟保守）
        expired = [k for k, (_, ts) in self._pending_reply.items()
                   if now - ts > 300]
        for k in expired:
            self._pending_reply.pop(k, None)
        key = group_id or user_id or ''
        hit = self._pending_reply.get(key)
        if hit:
            return hit[0]
        return self._last_msg_id if not group_id and not user_id else ''

    def _api_request(self, method: str, path: str, *,
                     json_body: dict = None, files=None, data=None,
                     timeout: int = 20) -> dict:
        """同步 HTTP（在 to_thread 里调用）。"""
        token = self._access_token
        if not token or time.time() >= self._token_expire_at - 30:
            # 同步刷新一次（to_thread 场景）
            resp = self._http.post(_TOKEN_URL, json={
                'appId': self.app_id, 'clientSecret': self.app_secret,
            }, timeout=15)
            body = resp.json() if resp.content else {}
            token = body.get('access_token') or body.get('accessToken') or ''
            if token:
                self._access_token = token
                self._token_expire_at = time.time() + int(
                    body.get('expires_in') or body.get('expiresIn') or 7200)
        headers = {'Authorization': f'QQBot {token}'}
        url = f'{self.api_base}{path}'
        if files is not None:
            h = {'Authorization': headers['Authorization']}
            resp = self._http.request(
                method, url, headers=h, files=files, data=data, timeout=timeout)
        else:
            resp = self._http.request(
                method, url,
                headers={**headers,
                         'Content-Type': 'application/json; charset=utf-8'},
                json=json_body, data=data, timeout=timeout)
        try:
            return resp.json() if resp.content else {}
        except Exception:
            return {'_http_status': resp.status_code,
                    '_text': (resp.text or '')[:500]}

    async def _upload_group_image(self, group_openid: str,
                                  image_data: dict) -> Optional[dict]:
        """上传图片到群，返回 media dict。"""
        file = str(image_data.get('file') or image_data.get('url') or '')
        b64 = image_data.get('base64') or _extract_b64(file)
        path = f'/v2/groups/{group_openid}/files'
        try:
            if b64:
                raw = _b64_to_bytes(b64)
                if not raw:
                    return None
                mime = _sniff_image_mime(raw)
                ext = {'image/png': 'png', 'image/jpeg': 'jpg',
                       'image/gif': 'gif', 'image/webp': 'webp'}.get(mime, 'png')
                files = {'file': (f'image.{ext}', raw, mime)}
                form = {'file_type': '1', 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
            elif file.startswith(('http://', 'https://')):
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    json_body={'file_type': 1, 'url': file,
                               'srv_send_msg': False})
            elif file.startswith('file://'):
                local = file[7:]
                with open(local, 'rb') as f:
                    raw = f.read()
                mime = _sniff_image_mime(raw)
                ext = {'image/png': 'png', 'image/jpeg': 'jpg',
                       'image/gif': 'gif', 'image/webp': 'webp'}.get(mime, 'png')
                files = {'file': (f'image.{ext}', raw, mime)}
                form = {'file_type': '1', 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
            else:
                # 本地路径
                with open(file, 'rb') as f:
                    raw = f.read()
                mime = _sniff_image_mime(raw)
                files = {'file': ('image.png', raw, mime)}
                form = {'file_type': '1', 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
        except Exception as e:
            logger.warning('qq_official 图片上传失败: %s', e)
            return None
        if not isinstance(data, dict):
            return None
        file_info = data.get('file_info')
        if not file_info:
            logger.warning('qq_official 上传响应无 file_info: %s', data)
            return None
        return {'file_info': file_info}

    async def _send_group(self, group_openid: str, message,
                          reply_msg_id: str = '') -> dict:
        text = _message_to_text(message)
        img = _first_image(message)
        msg_seq = int(time.time()) % 10000 + 1
        body: dict = {}
        if reply_msg_id:
            body['msg_id'] = reply_msg_id
            body['msg_seq'] = msg_seq
        if img:
            media = await self._upload_group_image(group_openid, img)
            if media:
                body.update({'msg_type': 7, 'media': media})
                if text:
                    body['content'] = text
            else:
                body.update({'msg_type': 0, 'content': text or '[图片上传失败]'})
        else:
            body.update({'msg_type': 0, 'content': text})
        path = f'/v2/groups/{group_openid}/messages'
        data = await asyncio.to_thread(
            self._api_request, 'POST', path, json_body=body)
        return self._wrap(data, ok=bool(isinstance(data, dict) and (
            data.get('id') or data.get('msg_id') or 'ret' not in str(data))))

    async def _send_c2c(self, user_openid: str, message,
                        reply_msg_id: str = '') -> dict:
        text = _message_to_text(message)
        img = _first_image(message)
        msg_seq = int(time.time()) % 10000 + 1
        body: dict = {}
        if reply_msg_id:
            body['msg_id'] = reply_msg_id
            body['msg_seq'] = msg_seq
        if img:
            # 单聊图片走 /v2/users/{openid}/files
            media = await self._upload_c2c_image(user_openid, img)
            if media:
                body.update({'msg_type': 7, 'media': media})
                if text:
                    body['content'] = text
            else:
                body.update({'msg_type': 0, 'content': text or '[图片上传失败]'})
        else:
            body.update({'msg_type': 0, 'content': text})
        path = f'/v2/users/{user_openid}/messages'
        data = await asyncio.to_thread(
            self._api_request, 'POST', path, json_body=body)
        return self._wrap(data, ok=bool(isinstance(data, dict) and data.get('id')))

    async def _upload_c2c_image(self, user_openid: str,
                                image_data: dict) -> Optional[dict]:
        file = str(image_data.get('file') or image_data.get('url') or '')
        b64 = image_data.get('base64') or _extract_b64(file)
        path = f'/v2/users/{user_openid}/files'
        try:
            if b64:
                raw = _b64_to_bytes(b64)
                if not raw:
                    return None
                mime = _sniff_image_mime(raw)
                files = {'file': ('image.png', raw, mime)}
                form = {'file_type': '1', 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
            elif file.startswith(('http://', 'https://')):
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    json_body={'file_type': 1, 'url': file,
                               'srv_send_msg': False})
            else:
                if file.startswith('file://'):
                    file = file[7:]
                with open(file, 'rb') as f:
                    raw = f.read()
                mime = _sniff_image_mime(raw)
                files = {'file': ('image.png', raw, mime)}
                form = {'file_type': '1', 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
        except Exception as e:
            logger.warning('qq_official 单聊图片上传失败: %s', e)
            return None
        if isinstance(data, dict) and data.get('file_info'):
            return {'file_info': data['file_info']}
        return None

    def _wrap(self, data, ok: bool = True) -> dict:
        if ok and isinstance(data, dict):
            return {'status': 'ok', 'retcode': 0, 'data': data}
        return {'status': 'failed', 'retcode': -1,
                'msg': f'qq_official 调用失败: {data}'}

    # ── ProtocolAdapter 契约 ────────────────────────────────────

    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        return normalize_event(raw_event, bot_name or self.bot_name)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_private_msg',
                      'send_text'):
            message = params.get('message', params.get('text', ''))
            message = normalize_message(message)
            group_id = params.get('group_id')
            user_id = params.get('user_id')
            reply = params.get('reply_msg_id') or params.get('msg_id') \
                or self._reply_msg_id(group_id, user_id)
            if action == 'send_group_msg':
                user_id = None
            elif action == 'send_private_msg':
                group_id = None
            if group_id:
                return await self._send_group(str(group_id), message, reply)
            if user_id:
                return await self._send_c2c(str(user_id), message, reply)
            return {'status': 'failed', 'retcode': -2,
                    'msg': 'send_msg 需要 group_id 或 user_id'}

        if action in ('get_status', 'get_login_info', 'get_online_clients'):
            return {
                'status': 'ok', 'retcode': 0,
                'data': {
                    'online': self._connected,
                    'self_id': self.app_id,
                    'nickname': self.bot_name,
                    'app_id': self.app_id,
                    'intents': self.intents,
                },
            }

        return {'status': 'failed', 'retcode': -10,
                'msg': f'qq_official 不支持动作 {action}'}

    async def send_text(self, text, *, user_id=None, group_id=None,
                        source=None) -> dict:
        return await self.call_api(
            'send_msg', bot=source, user_id=user_id, group_id=group_id,
            message=text)

    def get_connected_bots(self) -> list:
        return [self.bot_name] if self._connected else []


# ── 注册入口 ──────────────────────────────────────────────────

_adapter_instance = None


def register(ctx):
    global _adapter_instance
    fw = ctx._framework
    cfg = fw.config.get('qq_official', {})
    if isinstance(cfg, bool):
        if cfg is not True:
            ctx.log('QQ 官方接入端已禁用')
            return
        cfg = {}
    if cfg.get('enabled') is not True:
        ctx.log('QQ 官方接入端已禁用 (qq_official.enabled: true 才开启)')
        return
    app_id = str(cfg.get('app_id') or '').strip()
    app_secret = str(cfg.get('app_secret') or '').strip()
    _adapter_instance = QQOfficialAdapter(
        fw,
        app_id=app_id,
        app_secret=app_secret,
        bot_name=cfg.get('bot_name') or 'qq_official',
        intents=cfg.get('intents'),
        reconnect_interval=cfg.get('reconnect_interval') or 5,
        api_base=cfg.get('api_base') or _API_BASE,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    if app_id and app_secret:
        _adapter_instance.start()
        ctx.log(f'QQ 官方接入端已启动 (app_id={app_id})')
    else:
        ctx.log('QQ 官方接入端已注册，待配置 app_id / app_secret 后重启生效')


def unregister():
    global _adapter_instance
    if _adapter_instance:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_adapter_instance.stop())
        _adapter_instance = None
