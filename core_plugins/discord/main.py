# -*- coding: utf-8 -*-
"""
Discord 接入端（discord）

Gateway WSS（Hello/Identify/Heartbeat/Resume）收事件 + REST v10 发消息
（仅用 websockets + requests，无新三方依赖）：
  - MESSAGE_CREATE → 内部事件（guild→group，DM→private）
  - 出站：content 文本；图片 base64:// / file:// / http(s) → multipart files[0]
  - intents 默认 GUILDS|GUILD_MESSAGES|DIRECT_MESSAGES|MESSAGE_CONTENT
    （MESSAGE_CONTENT 为特权 Intent，需在 Developer Portal 开启）

配置（core_plugins.yaml → discord，默认 enabled: false）：
  token / bot_name / intents / reconnect_interval
"""
import asyncio
import base64
import json
import logging
import os
import re
import threading
from typing import List, Optional

import requests
import websockets

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "discord",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "Discord 接入端：Gateway WSS + REST v10 收发",
    "priority": 7,
    "official": True,
    "process": "core",
}

_API_BASE = 'https://discord.com/api/v10'
_GATEWAY = 'wss://gateway.discord.gg/'

# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
_DEFAULT_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)

_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

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


def normalize_message(message):
    """出站消息归一化：抽出 base64/CQ 图片段。"""
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
        parts.append({'type': 'image', 'data': img})
        last = m.end()
    tail = message[last:]
    if tail:
        parts.append({'type': 'text', 'data': {'text': tail}})
    return parts or message


def _message_to_text(message) -> str:
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
            if seg.get('type') in ('text', 'at'):
                data = seg.get('data') or {}
                t = data.get('text') or ''
                if t:
                    texts.append(str(t))
        return ''.join(texts)
    return str(message)


def _first_image(message) -> Optional[dict]:
    if isinstance(message, str):
        message = normalize_message(message)
    if not isinstance(message, list):
        return None
    for seg in message:
        if isinstance(seg, dict) and seg.get('type') == 'image':
            return seg.get('data') or {}
    return None


def build_segments(d: dict) -> list:
    """
    Discord MESSAGE_CREATE d → 规范消息段。

    Discord 把附件放在 `attachments[]`（每个带 content_type/url），
    贴纸在 `sticker_items[]`，回复在 `message_reference`。这里统一翻成规范段，
    插件用 ev.images / ev.videos / ev.files / ev.reply_id 读取即可。
    """
    if not isinstance(d, dict):
        return []
    segs = []

    ref = d.get('message_reference')
    if isinstance(ref, dict) and ref.get('message_id'):
        segs.append({'type': 'reply', 'data': {'id': ref.get('message_id')}})

    content = d.get('content') or ''
    if content:
        segs.append({'type': 'text', 'data': {'text': content}})

    for att in d.get('attachments') or []:
        if not isinstance(att, dict):
            continue
        url = att.get('url')
        if not url:
            continue
        ctype = str(att.get('content_type') or '')
        name = att.get('filename') or ''
        if ctype.startswith('image/'):
            stype = 'image'
        elif ctype.startswith('video/'):
            stype = 'video'
        elif ctype.startswith('audio/'):
            stype = 'voice'
        else:
            stype = 'file'
        # 没给 content_type 时按扩展名兜底
        if not ctype:
            low = str(name).lower()
            if low.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')):
                stype = 'image'
            elif low.endswith(('.mp4', '.mov', '.webm', '.mkv')):
                stype = 'video'
            elif low.endswith(('.ogg', '.mp3', '.wav', '.m4a', '.flac')):
                stype = 'voice'
        segs.append({'type': stype, 'data': {
            'url': url, 'name': name, 'mime': ctype,
            'size': att.get('size'),
            'width': att.get('width'), 'height': att.get('height'),
        }})

    for st in d.get('sticker_items') or []:
        if isinstance(st, dict) and st.get('id'):
            segs.append({'type': 'sticker', 'data': {
                'file_id': st.get('id'), 'name': st.get('name') or '',
            }})
    return segs


def normalize_event(d: dict, bot_name: str,
                    self_id: str = '') -> Optional[dict]:
    """Discord MESSAGE_CREATE d → 内部事件 dict。"""
    if not isinstance(d, dict):
        return None
    author = d.get('author') or {}
    if author.get('bot'):
        return None
    if self_id and str(author.get('id', '')) == str(self_id):
        return None

    content = d.get('content') or ''

    guild_id = d.get('guild_id') or ''
    channel_id = str(d.get('channel_id') or '')
    user_id = author.get('id') or ''
    is_group = bool(guild_id)

    event = {
        'type': 'message',
        'message_type': 'group' if is_group else 'private',
        'sub_type': '',
        'bot_name': bot_name,
        'user_id': user_id,
        'group_id': channel_id if is_group else None,
        'message_id': d.get('id'),
        'message': content,
        'raw_message': content,
        'segments': build_segments(d),
        'sender': {
            'user_id': user_id,
            'nickname': author.get('global_name')
            or author.get('username') or str(user_id),
        },
        'adapter': 'discord',
        'channel_id': channel_id,
        'guild_id': guild_id,
        'raw': d,
    }
    return event


class DiscordAdapter(ProtocolAdapter):
    """Discord Gateway + REST 接入端"""

    adapter_id = 'discord'

    def __init__(self, framework, token: str, bot_name: str = 'discord',
                 intents: int = None, reconnect_interval: int = 5,
                 api_base: str = _API_BASE, gateway_url: str = _GATEWAY):
        self.framework = framework
        self.token = str(token or '').strip()
        self.bot_name = bot_name or 'discord'
        self.intents = int(intents) if intents else _DEFAULT_INTENTS
        self.reconnect_interval = max(1, int(reconnect_interval or 5))
        self.api_base = (api_base or _API_BASE).rstrip('/')
        self.gateway_url = gateway_url or _GATEWAY

        self._ws = None
        self._connected = False
        self._closing = False
        self._supervisor = None
        self._heartbeat_task = None
        self._seq = None
        self._session_id = ''
        self._resume_gateway = ''
        self._heartbeat_interval_ms = 41250
        self._heartbeat_acked = True
        self._self_id = ''
        self._loop = None
        self._http = requests.Session()
        self._http.headers.update({
            'Authorization': f'Bot {self.token}',
            'User-Agent': 'ZCBOT (discord adapter, 1.0)',
        })

    # ── 连接自描述 ──────────────────────────────────────────────

    def get_connection_info(self) -> dict:
        return {
            'id': 'discord',
            'name': 'Discord',
            'config_section': 'discord',
            'fields': [
                {'key': 'token', 'label': 'Bot Token', 'type': 'password'},
                {'key': 'bot_name', 'label': 'Bot 名', 'type': 'string'},
                {'key': 'intents', 'label': 'Intents', 'type': 'number'},
                {'key': 'reconnect_interval', 'label': '重连间隔(秒)', 'type': 'number'},
            ],
            'restart_keys': ['token', 'bot_name', 'intents'],
            'endpoint_hint': 'wss://gateway.discord.gg/?v=10&encoding=json',
            'guide': 'Discord Developer Portal 创建 Bot 后填入 token；'
                     'MESSAGE_CONTENT 为特权 Intent 需在门户开启。'
                     '出站图片支持 base64://（multipart files[0]）。',
            'status_extra': {
                'connected': self._connected,
                'self_id': self._self_id,
                'intents': self.intents,
                'session': bool(self._session_id),
            },
        }

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        self._schedule_connect()

    def _schedule_connect(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
# 工作线程无事件循环（3.14 下 get_event_loop 直接抛错），线程定时器延后重试
            if not self._closing:
                _t = threading.Timer(0.5, self._schedule_connect)
                _t.daemon = True  # 守护定时器：loop 永不就绪时不得阻塞进程退出
                _t.start()
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
                logger.warning('discord 连接异常: %s', e)
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

    def _ws_url(self) -> str:
        base = self._resume_gateway or self.gateway_url
        if not base.startswith(('ws://', 'wss://')):
            base = 'wss://' + base.lstrip('/')
        sep = '&' if '?' in base else '?'
        if 'encoding=' in base:
            return base
        return f'{base}{sep}v=10&encoding=json'

    async def _connect_once(self):
        if not self.token:
            raise RuntimeError('未配置 discord token')
        url = self._ws_url()
        async with websockets.connect(
            url, max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20,
        ) as ws:
            self._ws = ws
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            hello = json.loads(raw)
            interval = 41250
            if isinstance(hello, dict) and hello.get('op') == _OP_HELLO:
                interval = int((hello.get('d') or {}).get('heartbeat_interval')
                               or interval)
            self._heartbeat_interval_ms = max(1000, interval)
            self._heartbeat_acked = True

            if self._session_id and self._seq is not None and self.token:
                identify = {
                    'op': _OP_RESUME,
                    'd': {
                        'token': self.token,
                        'session_id': self._session_id,
                        'seq': self._seq,
                    },
                }
            else:
                identify = {
                    'op': _OP_IDENTIFY,
                    'd': {
                        'token': self.token,
                        'intents': self.intents,
                        'properties': {
                            'os': 'windows',
                            'browser': 'zcbot',
                            'device': 'zcbot',
                        },
                    },
                }
            await ws.send(json.dumps(identify))

            self._connected = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            logger.info('discord 已连接: %s (intents=%s)', url, self.intents)
            await self._recv_loop(ws)

    async def _heartbeat_loop(self, ws):
        await asyncio.sleep(self._heartbeat_interval_ms / 2000.0)
        while self._connected and not self._closing:
            if not self._heartbeat_acked:
                logger.warning('discord 心跳未 ACK，断开重连')
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            self._heartbeat_acked = False
            try:
                await ws.send(json.dumps({'op': _OP_HEARTBEAT, 'd': self._seq}))
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
            if op == _OP_HEARTBEAT_ACK:
                self._heartbeat_acked = True
                continue
            if op == _OP_HEARTBEAT:
                try:
                    await ws.send(json.dumps(
                        {'op': _OP_HEARTBEAT, 'd': self._seq}))
                except Exception:
                    pass
                continue
            if op == _OP_RECONNECT:
                logger.info('discord 网关要求重连')
                break
            if op == _OP_INVALID_SESSION:
                resumable = bool(payload.get('d'))
                logger.warning('discord session 无效 (resumable=%s)', resumable)
                if not resumable:
                    self._session_id = ''
                    self._seq = None
                    self._resume_gateway = ''
                break
            if op != _OP_DISPATCH:
                continue
            seq = payload.get('s')
            if seq is not None:
                self._seq = seq
            t = payload.get('t') or ''
            d = payload.get('d')
            if t == 'READY' and isinstance(d, dict):
                self._session_id = d.get('session_id') or ''
                self._resume_gateway = d.get('resume_gateway_url') or ''
                user = d.get('user') or {}
                self._self_id = str(user.get('id') or '')
                self.bot_name = user.get('username') or self.bot_name
                logger.info('discord READY: %s#%s',
                            user.get('username'), user.get('discriminator', ''))
                continue
            if t == 'RESUMED':
                logger.info('discord RESUMED (seq=%s)', self._seq)
                continue
            if t != 'MESSAGE_CREATE' or not isinstance(d, dict):
                continue
            event = normalize_event(d, self.bot_name, self._self_id)
            if event is None:
                continue
            try:
                await self.framework.dispatch_event(event)
            except Exception as e:
                logger.error('discord 事件分发失败: %s', e)

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

    # ── REST 出站 ───────────────────────────────────────────────

    def _rest_post(self, path: str, *, json_body: dict = None,
                   files=None, data=None) -> dict:
        url = f'{self.api_base}{path}'
        if files is not None:
            resp = self._http.post(url, files=files, data=data, timeout=60)
        else:
            resp = self._http.post(
                url, json=json_body, timeout=30,
                headers={'Content-Type': 'application/json'})
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {'_status': resp.status_code, '_text': (resp.text or '')[:400]}
        if resp.status_code >= 400:
            return {'status': 'failed', 'retcode': resp.status_code,
                    'msg': str(body)}
        return {'status': 'ok', 'retcode': 0, 'data': body}

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_FILE, CAP_IMAGE, CAP_STICKER, CAP_TEXT, CAP_VIDEO,
            CAP_VOICE, Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VIDEO, CAP_VOICE, CAP_FILE,
                     CAP_STICKER],
            # 出站只能带附件（本地/base64），远程 URL 会退化成文本链接
            outbound=[CAP_TEXT, CAP_IMAGE, CAP_VIDEO, CAP_VOICE, CAP_FILE],
        )

    def normalize_incoming(self, raw_message) -> list:
        if isinstance(raw_message, dict):
            return build_segments(raw_message)
        from framework.messaging.segments import normalize_message
        return normalize_message(raw_message)

    def to_native(self, segments: list):
        """
        规范段 → Discord 发送指令：{'content': 文本, 'files': [(name, bytes)]}

        Discord 一条消息只能带一个正文，附件以 files[0..n] 上传；
        远程 URL 无法作为附件，退化为正文里的链接（在此标注 remote=True）。
        """
        from framework.messaging.segments import (
            canonical_type, media_ref, segments_to_text,
        )
        text = segments_to_text(segments)
        files, remote = [], []
        for s in segments or []:
            if not isinstance(s, dict):
                continue
            stype = canonical_type(s.get('type'))
            if stype not in ('image', 'video', 'voice', 'file'):
                continue
            ref = media_ref(s)
            if not ref:
                continue
            if ref.startswith(('http://', 'https://')):
                remote.append(ref)
            else:
                files.append({'ref': ref, 'name': (s.get('data') or {}).get('name') or ''})
        if remote:
            text = (text + '\n' if text else '') + '\n'.join(remote)
        return {'content': text, 'files': files}

    async def _send_to_channel(self, channel_id: str, message) -> dict:
        from framework.messaging.segments import normalize_message as _canon
        native = self.to_native(_canon(message))
        text = native.get('content') or ''
        files = native.get('files') or []
        path = f'/channels/{channel_id}/messages'

        if files:
            multipart = []
            for item in files[:10]:  # Discord 单条最多 10 个附件
                ref = item['ref']
                name = item['name'] or 'file.bin'
                b64 = _extract_b64(ref)
                try:
                    if b64:
                        raw = base64.b64decode(re.sub(r'\s+', '', b64))
                    elif ref.startswith('file://'):
                        p = ref[7:]
                        with open(p, 'rb') as f:
                            raw = f.read()
                        name = item['name'] or os.path.basename(p) or 'file.bin'
                    elif os.path.isfile(ref):
                        with open(ref, 'rb') as f:
                            raw = f.read()
                        name = item['name'] or os.path.basename(ref) or 'file.bin'
                    else:
                        continue
                except Exception as e:  # noqa: BLE001
                    logger.warning('discord 附件读取失败: %s', e)
                    continue
                multipart.append((name, raw))
            if multipart:
                form_files = {f'files[{i}]': (name, raw, 'application/octet-stream')
                              for i, (name, raw) in enumerate(multipart)}
                form = {'payload_json': json.dumps({'content': text},
                                                   ensure_ascii=False)}
                return await asyncio.to_thread(
                    self._rest_post, path, files=form_files, data=form)

        return await asyncio.to_thread(
            self._rest_post, path, json_body={'content': text})

    # ── ProtocolAdapter 契约 ────────────────────────────────────

    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        return normalize_event(raw_event, bot_name or self.bot_name,
                               self._self_id)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_private_msg',
                      'send_text'):
            message = params.get('message', params.get('text', ''))
            channel_id = params.get('channel_id') or params.get('group_id')
            if channel_id is None:
                # 私聊：Discord 用 channel id；允许 user_id 直接当 channel（DM 已有）
                channel_id = params.get('user_id')
            if channel_id is None:
                return {'status': 'failed', 'retcode': -2,
                        'msg': 'send_msg 需要 channel_id/group_id'}
            return await self._send_to_channel(str(channel_id), message)

        if action in ('get_status', 'get_login_info', 'get_online_clients'):
            return {
                'status': 'ok', 'retcode': 0,
                'data': {
                    'online': self._connected,
                    'self_id': self._self_id,
                    'nickname': self.bot_name,
                    'session': self._session_id,
                },
            }

        return {'status': 'failed', 'retcode': -10,
                'msg': f'discord 不支持动作 {action}'}

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
    cfg = fw.config.get('discord', {})
    if isinstance(cfg, bool):
        if cfg is not True:
            ctx.log('Discord 接入端已禁用')
            return
        cfg = {}
    if cfg.get('enabled') is not True:
        ctx.log('Discord 接入端已禁用 (discord.enabled: true 才开启)')
        return
    token = str(cfg.get('token') or '').strip()
    _adapter_instance = DiscordAdapter(
        fw,
        token=token,
        bot_name=cfg.get('bot_name') or 'discord',
        intents=cfg.get('intents'),
        reconnect_interval=cfg.get('reconnect_interval') or 5,
        api_base=cfg.get('api_base') or _API_BASE,
        gateway_url=cfg.get('gateway_url') or _GATEWAY,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    if token:
        _adapter_instance.start()
        ctx.log('Discord 接入端已启动（Gateway + REST v10）')
    else:
        ctx.log('Discord 接入端已注册，待配置 token 后重启生效')


def unregister():
    global _adapter_instance
    if _adapter_instance:
        inst = _adapter_instance
        _adapter_instance = None
        loop = getattr(inst.framework, 'loop', None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(inst.stop(), loop)
