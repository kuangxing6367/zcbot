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
import re
from typing import Optional

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

    # 文本：caption 兜底
    text = msg.get('text') or msg.get('caption') or ''
    # 附件 URL 提示
    for key in ('photo', 'document', 'video', 'audio', 'voice', 'sticker'):
        arr = msg.get(key)
        if isinstance(arr, list) and arr:
            # photo 是尺寸数组，取最大
            best = arr[-1] if isinstance(arr[-1], dict) else {}
            file_id = best.get('file_id')
            if file_id:
                text = (text + '\n' if text else '') + f'[{key}:{file_id}]'
        elif isinstance(arr, dict) and arr.get('file_id'):
            text = (text + '\n' if text else '') + f'[{key}:{arr["file_id"]}]'

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

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            try:
                asyncio.get_event_loop().call_soon(self._reschedule)
            except Exception:
                pass
            return
        self._reschedule()

    def _reschedule(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            try:
                asyncio.get_event_loop().call_soon(self._reschedule)
            except Exception:
                pass
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
            img = _first_image(message)
            text = _message_to_text(message)
            if img:
                return await asyncio.to_thread(
                    self._send_photo, chat_id, img, text, reply_to)
            return await asyncio.to_thread(
                self._send_message, chat_id, text, reply_to)

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
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_adapter_instance.stop())
        _adapter_instance = None
