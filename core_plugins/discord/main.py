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

官方变更核对（2026-11，discord/discord-api-docs）：
  - API v10 仍是现行版本（v11 只在 changelog 里列为 upcoming）
  - content 上限 2000 字符；正文与附件都空时官方返回 400 Empty Message
  - 2026-11-16 起 Channel Obfuscation 对 bot 强制生效：看不见的频道仍会经
    Gateway 下发，但 name 变 "___hidden___"、flags 置 CHANNEL_OBFUSCATED(1<<17)，
    HTTP 的频道列表直接省略。本端只消费 MESSAGE_CREATE 的 channel_id/guild_id，
    不读频道元数据，因此不受影响；将来若加频道列表功能需按该 flag 过滤。
"""
import asyncio
import base64
import json
import logging
import os
import random
import re
import threading
from typing import List, Optional

import requests
import websockets
from websockets.exceptions import ConnectionClosed

from framework.messaging.backoff import Backoff
from framework.messaging.backoff import now as _mono
from framework.messaging.protocol import ProtocolAdapter
from framework.security.hardening import sanitize_adapter_event

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

# 这些关闭码代表「配置/权限错了」，重连一万次也不会好，还会耗尽配额：
# 官方限 1000 次 IDENTIFY / 24 小时（跨 shard 全局），超了直接重置 bot token。
# 4014 尤其致命——MESSAGE_CONTENT 等特权 Intent 只在门户开了、代码里没请求（或反之）
# 就是这个码，日志里只会看到反复断开。
_FATAL_CLOSE_CODES = {
    4004: 'token 无效，请重新复制 Bot Token',
    4010: '分片参数非法',
    4011: '需要分片（bot 已超过 2500 个服务器），本接入端不支持分片',
    4012: 'Gateway 版本不受支持',
    4013: 'intents 取值非法，请检查 discord.intents',
    4014: 'intents 里有未获批准的特权 Intent（MESSAGE_CONTENT / GUILD_MEMBERS / '
          'GUILD_PRESENCES），需在 Developer Portal 打开对应开关',
}
# 官方：收到 1000/1001 后 session 作废，必须重新 IDENTIFY，不能拿旧 session 去 RESUME
_SESSION_RESET_CODES = {1000, 1001}
_INTENT_MESSAGE_CONTENT = 1 << 15

_B64_RE = re.compile(r'^base64://([A-Za-z0-9+/=\s]+)$', re.DOTALL)
_CQ_IMAGE_RE = re.compile(
    r'\[CQ:image,file=([^\],]+)(?:,([^\]]*))?\]', re.IGNORECASE)

#: 官方上限：消息 content 2000 字符（按 UTF-16 code unit 计），超长直接 400。
_MAX_CONTENT_LEN = 2000


def _utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2


def _split_content(text: str, limit: int = _MAX_CONTENT_LEN) -> list:
    """按上限切分正文，优先在换行/空格处断开。"""
    text = str(text or '')
    if _utf16_len(text) <= limit:
        return [text]
    chunks, buf, buf_len = [], [], 0
    for ch in text:
        n = _utf16_len(ch)
        if buf_len + n > limit:
            piece = ''.join(buf)
            cut = max(piece.rfind('\n'), piece.rfind(' '))
            if cut > len(piece) * 0.5:
                head, tail = piece[:cut + 1], piece[cut + 1:]
            else:
                head, tail = piece, ''
            if head:
                chunks.append(head)
            buf = list(tail)
            buf_len = _utf16_len(tail)
        buf.append(ch)
        buf_len += n
    if buf:
        chunks.append(''.join(buf))
    return [c for c in chunks if c] or ['']


def _extract_b64(data: str) -> str | None:
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


def _first_image(message) -> dict | None:
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
                    self_id: str = '') -> dict | None:
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
        self._backoff = Backoff(base=self.reconnect_interval, maximum=60)
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
        # 致命关闭码（4004/4013/4014…）后置为原因文本，supervisor 据此停止重连
        self._fatal_reason = ''
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
        self._fatal_reason = ''
        self._warn_missing_intents()
        self._schedule_connect()

    def _warn_missing_intents(self):
        """MESSAGE_CONTENT 缺失不会报错，只会让每条消息的 content 变成空串。

        官方明确写过这一点：事件照常到达，被门控的字段为空。
        现象是「机器人收到了消息但内容为空」，极易被当成解析 bug 排查半天，
        所以在启动时直接说清楚，而不是等用户去猜。
        """
        try:
            intents = int(getattr(self, 'intents', 0) or 0)
        except (TypeError, ValueError):
            return
        if intents & _INTENT_MESSAGE_CONTENT:
            return
        logger.warning(
            'discord intents 未包含 MESSAGE_CONTENT(1<<15)，服务器内普通消息的 '
            'content 会是空字符串（只有 @ 机器人 和私聊仍能拿到正文）。'
            '需要完整消息内容请到 Developer Portal → Bot → '
            'Privileged Gateway Intents 打开 MESSAGE_CONTENT，并保持 intents 含该位。')

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
        # 断线重连走统一退避（framework.messaging.backoff）：指数增长 + 抖动，
        # 避免故障期猛敲网关、多实例同时重连撞在一起。
        while not self._closing:
            started = _mono()
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
            if self._fatal_reason:
                # 配置/权限类错误：重连不会自愈，还会把 IDENTIFY 配额烧光
                logger.error('discord 接入端已停止重连：%s', self._fatal_reason)
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
        try:
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
        except ConnectionClosed as e:
            self._note_close_code(getattr(e, 'code', None))
        except Exception as e:
            # 握手阶段失败（HTTP 401/403 等）：token 错、bot 被禁用都属于这一类，
            # 重连不会自愈，标成致命并停止重试。
            status = getattr(e, 'status_code', None)
            if status in (401, 403):
                self._fatal_reason = f'HTTP {status}：token 无效或应用被禁用'
                logger.error('discord 认证失败（HTTP %s），已停止重连', status)
                return
            raise

    def _note_close_code(self, code):
        """按官方关闭码语义决定「可恢复重连」还是「停止并说明原因」。"""
        if code is None:
            return
        if code in _FATAL_CLOSE_CODES:
            self._fatal_reason = f'{code}: {_FATAL_CLOSE_CODES[code]}'
            logger.error('discord 网关关闭码 %s（%s），已停止重连。'
                         '继续重试只会耗尽 IDENTIFY 配额。',
                         code, _FATAL_CLOSE_CODES[code])
            return
        if code in _SESSION_RESET_CODES:
            # 1000/1001 意味着 session 已作废，必须重新 IDENTIFY
            self._session_id = ''
            self._seq = None
            self._resume_gateway = ''
            logger.info('discord 连接正常关闭（%s），session 已作废，'
                        '下次重新 IDENTIFY', code)

    async def _heartbeat_loop(self, ws):
        # 官方要求首次心跳延迟 = heartbeat_interval * random(0, 1)（jitter），
        # 目的是让大量 bot 不在同一毫秒一起心跳/一起重连形成尖峰；
        # 原来固定用一半间隔，正是「同一时刻集体重连」的成因。
        await asyncio.sleep(
            self._heartbeat_interval_ms / 1000.0 * random.random())
        while self._connected and not self._closing:
            if not self._heartbeat_acked:
                # 官方：上个心跳没收到 ACK 说明连接已 zombie（TCP 还在、实际已死），
                # 必须用 4000 关闭后重连，否则会一直挂着收不到任何事件
                logger.warning('discord 心跳未 ACK（zombied 连接），'
                               '以 4000 关闭后重连')
                try:
                    await ws.close(code=4000)
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
            # 入站统一净化：剥顶层注入字段、强制归属为当前接入端。
            event = sanitize_adapter_event(event, self.bot_name)
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
            # retcode 必须用框架约定值，不能直接透 HTTP 状态码：
            # 出站闸门/插件判断的是 -1(业务失败) / -4(限流)，看到 429 会当未知错误。
            if resp.status_code == 429:
                ra = None
                if isinstance(body, dict):
                    ra = body.get('retry_after')
                if ra is None:
                    ra = resp.headers.get('Retry-After')
                try:
                    ra = float(ra) if ra is not None else None
                except (TypeError, ValueError):
                    ra = None
                return {'status': 'failed', 'retcode': -4, 'throttled': True,
                        'retry_after': int(ra) if ra is not None else None,
                        'msg': (f'discord 限流，官方要求等待 '
                                f'{int(ra) if ra is not None else "?"}s')}
            if resp.status_code in (401, 403):
                return {'status': 'failed', 'retcode': -1,
                        'http_status': resp.status_code,
                        'msg': f'discord 拒绝访问（HTTP {resp.status_code}）：'
                               f'token 无效或 bot 缺少该频道的权限'}
            return {'status': 'failed', 'retcode': -1,
                    'http_status': resp.status_code,
                    'error_code': body.get('code') if isinstance(body, dict) else None,
                    'msg': str(body)}
        return {'status': 'ok', 'retcode': 0, 'data': body}

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_FILE,
            CAP_IMAGE,
            CAP_STICKER,
            CAP_TEXT,
            CAP_VIDEO,
            CAP_VOICE,
            Capabilities,
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
            canonical_type,
            media_ref,
            segments_to_text,
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
        # 正文与附件都空时官方返回 400 Cannot send an empty message，本地先挡掉
        if not files and not text.strip():
            return {'status': 'failed', 'retcode': -1, 'msg': 'discord 消息为空'}
        # 正文超 2000 直接 400：切成多条，附件只挂第一条
        chunks = _split_content(text)
        head, rest = chunks[0], chunks[1:]

        if files:
            multipart = []
            if len(files) > 10:
                # 官方单条上限 10 个附件，超出部分无法随这条消息发出。
                # 静默截断会让调用方以为全发出去了，这里必须留下痕迹——
                # 但也不强行拆第二条消息（会打乱正文与附件的时间顺序）。
                logger.warning(
                    'discord 单条消息最多 10 个附件，已丢弃后 %d 个（共 %d 个）',
                    len(files) - 10, len(files))
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
                except Exception as e:
                    logger.warning('discord 附件读取失败: %s', e)
                    continue
                multipart.append((name, raw))
            if multipart:
                form_files = {f'files[{i}]': (name, raw, 'application/octet-stream')
                              for i, (name, raw) in enumerate(multipart)}
                form = {'payload_json': json.dumps({'content': head},
                                                   ensure_ascii=False)}
                result = await asyncio.to_thread(
                    self._rest_post, path, files=form_files, data=form)
                if result.get('status') != 'ok':
                    return result
                for chunk in rest:
                    result = await asyncio.to_thread(
                        self._rest_post, path,
                        json_body={'content': chunk})
                    if result.get('status') != 'ok':
                        return result
                return result

            # multipart 为空：附件全读失败（不是没附件）。正文照发但要说清楚，
            # 否则调用方只看到「发送成功」，以为图发出去了。
            logger.warning(
                'discord 消息的 %d 个附件全部读取失败，本条只发送了正文', len(files))

        result = await asyncio.to_thread(
            self._rest_post, path, json_body={'content': head})
        if result.get('status') != 'ok':
            return result
        for chunk in rest:
            result = await asyncio.to_thread(
                self._rest_post, path, json_body={'content': chunk})
            if result.get('status') != 'ok':
                return result
        return result

    # ── ProtocolAdapter 契约 ────────────────────────────────────

    async def handle_event(self, raw_event: dict, bot_name: str) -> dict | None:
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
                return {'status': 'failed', 'retcode': -1,
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
