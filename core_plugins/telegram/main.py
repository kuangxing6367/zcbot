# -*- coding: utf-8 -*-
"""
Telegram 接入端（telegram）

getUpdates 长轮询收消息 + sendMessage/sendPhoto 出站（仅用 requests，无新三方依赖）：
  - 长轮询 offset 自增，断线自动重试
  - 私聊 → message_type=private；超级群/群 → message_type=group
  - 出站图片：base64:// / file:// / http(s) / 本地路径 → multipart sendPhoto

配置（core_plugins.yaml → telegram，默认 enabled: false）：
  token / bot_name / polling_timeout / allowed_updates
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

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "telegram",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "Telegram 接入端：getUpdates 长轮询 + sendMessage/sendPhoto",
    "priority": 6,
    "official": True,
    "process": "core",
}

_API_BASE = 'https://api.telegram.org'

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


#: Telegram 附件字段 → 规范段类型
_TG_MEDIA_FIELDS = (
    ('voice', 'voice'), ('audio', 'voice'), ('video_note', 'video'),
    ('video', 'video'), ('animation', 'video'), ('document', 'file'),
    ('sticker', 'sticker'),
)


def build_segments(msg: dict) -> list:
    """
    Telegram Message → 规范消息段。

    Telegram 把附件挂在 message 对象的各个字段上（photo[] / voice /
    document / sticker …），这里统一翻成规范段，插件用 ev.images /
    ev.voices / ev.files 就能拿到，不用再去 raw 里掏。
    """
    if not isinstance(msg, dict):
        return []
    segs: List[dict] = []

    reply = msg.get('reply_to_message')
    if isinstance(reply, dict) and reply.get('message_id') is not None:
        segs.append({'type': 'reply', 'data': {'id': reply.get('message_id')}})

    content = msg.get('text') or msg.get('caption') or ''
    if content:
        segs.append({'type': 'text', 'data': {'text': content}})

    photos = msg.get('photo')
    if isinstance(photos, list) and photos:
        best = photos[-1] if isinstance(photos[-1], dict) else {}
        if best.get('file_id'):
            segs.append({'type': 'image', 'data': {
                'file_id': best.get('file_id'),
                'width': best.get('width'), 'height': best.get('height'),
                'size': best.get('file_size'),
            }})

    for field, seg_type in _TG_MEDIA_FIELDS:
        obj = msg.get(field)
        if not isinstance(obj, dict) or not obj.get('file_id'):
            continue
        data = {'file_id': obj.get('file_id'), 'size': obj.get('file_size')}
        for src, dst in (('duration', 'duration'), ('file_name', 'name'),
                         ('mime_type', 'mime'), ('width', 'width'),
                         ('height', 'height')):
            if obj.get(src) is not None:
                data[dst] = obj.get(src)
        data['tg_type'] = field
        segs.append({'type': seg_type, 'data': data})
    return segs


def normalize_event(update: dict, bot_name: str) -> Optional[dict]:
    """Telegram Update → 内部事件 dict。"""
    if not isinstance(update, dict):
        return None
    msg = update.get('message') or update.get('edited_message') \
        or update.get('channel_post') or update.get('edited_channel_post')
    if not isinstance(msg, dict):
        # callback_query 等暂透传为 notice
        if update.get('callback_query'):
            cq = update['callback_query'] or {}
            from_user = cq.get('from') or {}
            return {
                'type': 'notice',
                'notice_type': 'callback_query',
                'bot_name': bot_name,
                'user_id': from_user.get('id', 0),
                'message': (cq.get('data') or ''),
                'sender': {
                    'user_id': from_user.get('id', 0),
                    'nickname': from_user.get('first_name')
                    or from_user.get('username') or '',
                },
                'adapter': 'telegram',
                'raw': update,
            }
        return None

    chat = msg.get('chat') or {}
    user = msg.get('from') or {}
    chat_id = chat.get('id')
    chat_type = chat.get('type') or 'private'
    is_group = chat_type in ('group', 'supergroup', 'channel')

    # 文本：caption 兜底。附件不再塞进文本（旧版会拼 [photo:xxx]），
    # 改为规范消息段承载，插件用 ev.images / ev.voices / ev.files 读取。
    text = msg.get('text') or msg.get('caption') or ''

    event = {
        'type': 'message',
        'message_type': 'group' if is_group else 'private',
        'sub_type': '',
        'bot_name': bot_name,
        'user_id': user.get('id', 0),
        'group_id': chat_id if is_group else None,
        'message_id': msg.get('message_id'),
        'message': text,
        'raw_message': text,
        'segments': build_segments(msg),
        'sender': {
            'user_id': user.get('id', 0),
            'nickname': user.get('username')
            or user.get('first_name') or str(user.get('id', '')),
        },
        'adapter': 'telegram',
        'chat_id': chat_id,
        'chat_type': chat_type,
        'raw': msg,
    }
    return event


class TelegramAdapter(ProtocolAdapter):
    """Telegram 长轮询接入端"""

    adapter_id = 'telegram'

    def __init__(self, framework, token: str, bot_name: str = 'telegram',
                 polling_timeout: int = 30, api_base: str = _API_BASE,
                 allowed_updates=None):
        self.framework = framework
        self.token = str(token or '').strip()
        self.bot_name = bot_name or 'telegram'
        self.polling_timeout = max(1, int(polling_timeout or 30))
        self.api_base = (api_base or _API_BASE).rstrip('/')
        self.allowed_updates = allowed_updates or ['message', 'edited_message']
        self._offset = 0
        self._connected = False
        self._closing = False
        self._task = None
        self._me = None
        self._http = requests.Session()

    def _url(self, method: str) -> str:
        return f'{self.api_base}/bot{self.token}/{method}'

    # ── 连接自描述 ──────────────────────────────────────────────

    def get_connection_info(self) -> dict:
        return {
            'id': 'telegram',
            'name': 'Telegram',
            'config_section': 'telegram',
            'fields': [
                {'key': 'token', 'label': 'Bot Token', 'type': 'password'},
                {'key': 'bot_name', 'label': 'Bot 名', 'type': 'string'},
                {'key': 'polling_timeout', 'label': '轮询超时(秒)', 'type': 'number'},
            ],
            'restart_keys': ['token', 'bot_name'],
            'endpoint_hint': f'@{self.bot_name}（getUpdates 长轮询）',
            'guide': '向 @BotFather 申请 token 填入；本端长轮询 getUpdates，'
                     '出站 sendMessage/sendPhoto（图片支持 base64://）。',
            'status_extra': {
                'connected': self._connected,
                'offset': self._offset,
                'username': (self._me or {}).get('username', ''),
            },
        }

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_FILE, CAP_IMAGE, CAP_NOTICE, CAP_STICKER, CAP_TEXT,
            CAP_VIDEO, CAP_VOICE, Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE,
                     CAP_STICKER, CAP_NOTICE],
            outbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE,
                      CAP_STICKER],
        )

    def normalize_incoming(self, raw_message) -> list:
        """Telegram Message / 段数组 → 规范消息段"""
        if isinstance(raw_message, dict):
            return build_segments(raw_message)
        from framework.messaging.segments import normalize_message
        return normalize_message(raw_message)

    def to_native(self, segments: list):
        """
        规范段 → Telegram 发送指令序列。

        返回 [{method, field, data, caption}]，由 send 路径逐条执行；
        文本段合并成 caption 挂在首条媒体上，多余的纯文本另发一条。
        """
        from framework.messaging.segments import (
            canonical_type, media_ref, segments_to_text,
        )
        _METHOD = {'image': ('sendPhoto', 'photo'),
                   'voice': ('sendVoice', 'voice'),
                   'video': ('sendVideo', 'video'),
                   'file': ('sendDocument', 'document'),
                   'sticker': ('sendSticker', 'sticker')}
        ops = []
        text = segments_to_text(segments)
        for s in segments or []:
            if not isinstance(s, dict):
                continue
            stype = canonical_type(s.get('type'))
            if stype not in _METHOD:
                continue
            method, field = _METHOD[stype]
            if not media_ref(s):
                continue
            ops.append({'method': method, 'field': field, 'type': stype,
                        'data': s.get('data') or {},
                        'caption': text if not ops else ''})
        if not ops:
            ops.append({'method': 'sendMessage', 'field': 'text',
                        'data': {'text': text}, 'caption': ''})
        elif text and not ops[0]['caption']:
            ops.append({'method': 'sendMessage', 'field': 'text',
                        'data': {'text': text}, 'caption': ''})
        return ops

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
# 工作线程无事件循环（3.14 下 get_event_loop 直接抛错），线程定时器延后重试
            if not self._closing:
                _t = threading.Timer(0.5, self._reschedule)
                _t.daemon = True  # 守护定时器：loop 永不就绪时不得阻塞进程退出
                _t.start()
            return
        self._reschedule()

    def _reschedule(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            if not self._closing:
                _t = threading.Timer(0.5, self._reschedule)
                _t.daemon = True  # 守护定时器：loop 永不就绪时不得阻塞进程退出
                _t.start()
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._poll_loop())

    async def _poll_loop(self):
        # 首次 getMe 校验 token
        try:
            me = await asyncio.to_thread(self._get_me)
            if me:
                self._me = me
                self.bot_name = me.get('username') or self.bot_name
                logger.info('telegram 已登录: @%s', self.bot_name)
        except Exception as e:
            logger.warning('telegram getMe 失败: %s', e)

        while not self._closing:
            try:
                updates = await asyncio.to_thread(self._get_updates)
                for upd in updates or []:
                    if self._closing:
                        break
                    self._offset = max(self._offset,
                                       int(upd.get('update_id', 0)) + 1)
                    event = normalize_event(upd, self.bot_name)
                    if event is None:
                        continue
                    try:
                        await self.framework.dispatch_event(event)
                    except Exception as e:
                        logger.error('telegram 事件分发失败: %s', e)
                self._connected = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected = False
                logger.warning('telegram 轮询异常: %s', e)
                await asyncio.sleep(3)

    def _get_me(self) -> dict:
        resp = self._http.get(self._url('getMe'), timeout=15)
        data = resp.json() if resp.content else {}
        if data.get('ok'):
            return data.get('result') or {}
        raise RuntimeError(f'getMe 失败: {data}')

    def _get_updates(self) -> list:
        payload = {
            'timeout': self.polling_timeout,
            'limit': 100,
            'allowed_updates': json.dumps(self.allowed_updates),
        }
        if self._offset:
            payload['offset'] = self._offset
        resp = self._http.post(
            self._url('getUpdates'), json=payload,
            timeout=self.polling_timeout + 15,
            headers={'Content-Type': 'application/json; charset=utf-8'})
        data = resp.json() if resp.content else {}
        if not data.get('ok'):
            raise RuntimeError(f'getUpdates 失败: {data}')
        return data.get('result') or []

    async def stop(self):
        self._closing = True
        self._connected = False
        t, self._task = self._task, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self._http.close()
        except Exception:
            pass

    # ── 出站发送 ────────────────────────────────────────────────

    def _send_message(self, chat_id, text: str,
                      reply_to: int = None) -> dict:
        body = {'chat_id': chat_id, 'text': text}
        if reply_to:
            body['reply_parameters'] = json.dumps({'message_id': reply_to})
        resp = self._http.post(
            self._url('sendMessage'),
            json=body, timeout=30,
            headers={'Content-Type': 'application/json; charset=utf-8'})
        data = resp.json() if resp.content else {}
        if data.get('ok'):
            return {'status': 'ok', 'retcode': 0, 'data': data.get('result')}
        return {'status': 'failed', 'retcode': -1, 'msg': str(data)}

    def _send_media(self, chat_id, op: dict, reply_to=None) -> dict:
        """
        通用媒体发送：sendPhoto / sendVoice / sendVideo / sendDocument / sendSticker。

        引用形态自动识别：base64:// 走 multipart、http(s) 直传、本地读盘上传、
        其余按 Telegram file_id 直接引用（ uploads 已在 bot 侧存在时零带宽）。
        """
        from framework.messaging.segments import media_ref
        method = op.get('method') or 'sendDocument'
        field = op.get('field') or 'document'
        data = op.get('data') or {}
        ref = media_ref({'type': op.get('type'), 'data': data})
        if not ref:
            return {'status': 'failed', 'retcode': -2,
                    'msg': f'{method} 缺少文件引用'}

        body = {'chat_id': chat_id}
        caption = op.get('caption') or ''
        if caption:
            body['caption'] = caption
        if reply_to:
            body['reply_parameters'] = json.dumps({'message_id': reply_to})

        name = str(data.get('name') or '')
        files = None
        b64 = _extract_b64(ref)
        if b64:
            try:
                raw = base64.b64decode(re.sub(r'\s+', '', b64))
            except Exception as e:  # noqa: BLE001
                return {'status': 'failed', 'retcode': -3,
                        'msg': f'base64 解码失败: {e}'}
            files = {field: (name or f'{field}.bin', raw,
                             'application/octet-stream')}
        elif ref.startswith(('http://', 'https://')):
            body[field] = ref
        else:
            path = ref[7:] if ref.startswith('file://') else ref
            if os.path.isfile(path):
                with open(path, 'rb') as f:
                    raw = f.read()
                fname = name or os.path.basename(path) or f'{field}.bin'
                files = {field: (fname, raw, 'application/octet-stream')}
            else:
                # 不是本地文件 → 当作 Telegram file_id 直接引用
                body[field] = ref

        try:
            if files is None:
                resp = self._http.post(
                    self._url(method), json=body, timeout=60,
                    headers={'Content-Type': 'application/json; charset=utf-8'})
            else:
                resp = self._http.post(
                    self._url(method), data=body, files=files, timeout=60)
            payload = resp.json() if resp.content else {}
        except Exception as e:  # noqa: BLE001
            return {'status': 'failed', 'retcode': -4, 'msg': f'{method} 失败: {e}'}
        if payload.get('ok'):
            return {'status': 'ok', 'retcode': 0, 'data': payload.get('result')}
        return {'status': 'failed', 'retcode': -1, 'msg': str(payload)}

    def _send_photo(self, chat_id, image: dict, caption: str = '',
                    reply_to: int = None) -> dict:
        file = str(image.get('file') or image.get('url') or '')
        b64 = image.get('base64') or _extract_b64(file)
        data = {'chat_id': chat_id}
        if caption:
            data['caption'] = caption
        if reply_to:
            data['reply_parameters'] = json.dumps({'message_id': reply_to})
        files = None
        if b64:
            raw = base64.b64decode(re.sub(r'\s+', '', b64))
            files = {'photo': ('image.png', raw, 'image/png')}
        elif file.startswith(('http://', 'https://')):
            data['photo'] = file
        else:
            if file.startswith('file://'):
                file = file[7:]
            with open(file, 'rb') as f:
                raw = f.read()
            name = file.rsplit('/', 1)[-1].rsplit('\\', 1)[-1] or 'image.jpg'
            files = {'photo': (name, raw, 'application/octet-stream')}
        if files is None:
            resp = self._http.post(
                self._url('sendPhoto'), json=data, timeout=60,
                headers={'Content-Type': 'application/json; charset=utf-8'})
        else:
            resp = self._http.post(
                self._url('sendPhoto'), data=data, files=files, timeout=60)
        body = resp.json() if resp.content else {}
        if body.get('ok'):
            return {'status': 'ok', 'retcode': 0, 'data': body.get('result')}
        return {'status': 'failed', 'retcode': -1, 'msg': str(body)}

    # ── ProtocolAdapter 契约 ────────────────────────────────────

    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        return normalize_event(raw_event, bot_name or self.bot_name)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_private_msg',
                      'send_text'):
            message = normalize_message(params.get('message',
                                                   params.get('text', '')))
            chat_id = params.get('group_id')
            if chat_id is None:
                chat_id = params.get('user_id')
            if chat_id is None:
                chat_id = params.get('chat_id')
            if chat_id is None:
                return {'status': 'failed', 'retcode': -2,
                        'msg': 'send_msg 需要 chat_id/user_id/group_id'}
            reply_to = params.get('reply_to') or params.get('message_id')
            # 规范消息段 → Telegram 发送指令（支持图/语音/视频/文件/贴纸）
            from framework.messaging.segments import normalize_message as _canon
            ops = self.to_native(_canon(message))
            last = None
            for op in ops:
                if op.get('method') == 'sendMessage':
                    last = await asyncio.to_thread(
                        self._send_message, chat_id,
                        (op.get('data') or {}).get('text', ''), reply_to)
                else:
                    last = await asyncio.to_thread(
                        self._send_media, chat_id, op, reply_to)
            return last or {'status': 'failed', 'retcode': -1, 'msg': '发送无内容'}

        if action == 'get_me':
            return {'status': 'ok', 'retcode': 0, 'data': self._me}

        if action in ('get_status', 'get_login_info', 'get_online_clients'):
            me = self._me or {}
            return {
                'status': 'ok', 'retcode': 0,
                'data': {
                    'online': self._connected,
                    'self_id': me.get('id', 0),
                    'nickname': me.get('username') or self.bot_name,
                    'offset': self._offset,
                },
            }

        return {'status': 'failed', 'retcode': -10,
                'msg': f'telegram 不支持动作 {action}'}

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
    cfg = fw.config.get('telegram', {})
    if isinstance(cfg, bool):
        if cfg is not True:
            ctx.log('Telegram 接入端已禁用')
            return
        cfg = {}
    if cfg.get('enabled') is not True:
        ctx.log('Telegram 接入端已禁用 (telegram.enabled: true 才开启)')
        return
    token = str(cfg.get('token') or '').strip()
    _adapter_instance = TelegramAdapter(
        fw,
        token=token,
        bot_name=cfg.get('bot_name') or 'telegram',
        polling_timeout=cfg.get('polling_timeout') or 30,
        api_base=cfg.get('api_base') or _API_BASE,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    if token:
        _adapter_instance.start()
        ctx.log('Telegram 接入端已启动（getUpdates 长轮询）')
    else:
        ctx.log('Telegram 接入端已注册，待配置 token 后重启生效')


def unregister():
    global _adapter_instance
    if _adapter_instance:
        inst = _adapter_instance
        _adapter_instance = None
        loop = getattr(inst.framework, 'loop', None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(inst.stop(), loop)
