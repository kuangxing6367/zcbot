"""
Telegram 接入端（telegram）

getUpdates 长轮询收消息 + sendMessage/sendPhoto 出站（仅用 requests，无新三方依赖）：
  - 长轮询 offset 自增，断线自动重试
  - offset 落盘（data/telegram_offset.json）：重启后接着上次位置，不重放旧消息
  - 私聊 → message_type=private；超级群/群 → message_type=group
  - 出站图片：base64:// / file:// / http(s) / 本地路径 → multipart sendPhoto

配置（core_plugins.yaml → telegram，默认 enabled: false）：
  token / bot_name / polling_timeout / parse_mode / allowed_updates

官方变更核对（2026-11，core.telegram.org/bots/api → Bot API 10.3）：
  - 文本上限 4096、caption 上限 1024，计数单位是「解析实体之后」的 UTF-16 码元
  - parse_mode 合法值只有 HTML / MarkdownV2 / Markdown
  - message_thread_id 用于论坛话题；reply_parameters 才是引用（reply_to_message_id 已废）
  - 10.x 新增的 RichMessage / EphemeralMessage 本端**未接**：字段多、对纯文本
    场景无收益，接一半反而容易出现实体解析失败
"""
import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
from typing import List, Optional

import requests

from framework.messaging.backoff import Backoff
from framework.messaging.protocol import ProtocolAdapter
from framework.security.hardening import sanitize_adapter_event

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


class TelegramApiError(RuntimeError):
    """Telegram 返回 ok=false 时的结构化错误。

    官方错误响应形如：
      {"ok": false, "error_code": 429,
       "description": "Too Many Requests: retry after 27",
       "parameters": {"retry_after": 27}}
    retry_after 只在 429 出现，单位秒，是**必须遵守**的等待时间：
    自己拍一个更短的间隔重试，会被继续限流，越试越久。
    """

    def __init__(self, error_code, description='', retry_after=None,
                 payload=None):
        self.error_code = int(error_code or 0)
        self.description = description or ''
        self.retry_after = retry_after
        self.payload = payload or {}
        super().__init__(
            f'telegram {self.error_code}: {self.description or "未知错误"}')

    @classmethod
    def from_payload(cls, data):
        if not isinstance(data, dict):
            return cls(-1, str(data)[:200])
        params = data.get('parameters') or {}
        retry_after = params.get('retry_after')
        if retry_after is None and data.get('error_code') == 429:
            # 少数情况下只有 description 里带 "retry after N"
            m = re.search(r'retry after (\d+)', str(data.get('description')))
            if m:
                retry_after = int(m.group(1))
        try:
            retry_after = int(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after = None
        return cls(data.get('error_code') or -1,
                   str(data.get('description') or ''),
                   retry_after, data)

_B64_RE = re.compile(r'^base64://([A-Za-z0-9+/=\s]+)$', re.DOTALL)
_CQ_IMAGE_RE = re.compile(
    r'\[CQ:image,file=([^\],]+)(?:,([^\]]*))?\]', re.IGNORECASE)

#: 官方上限：sendMessage.text 4096、媒体 caption 1024。
#: 超长不是「截断」而是 400 整条失败，所以必须切分后分条发送。
#: 计数单位是 UTF-16 code unit（非 BMP 的 emoji 占 2），用 _utf16_len 精确算。
_MAX_TEXT_LEN = 4096
_MAX_CAPTION_LEN = 1024
#: parse_mode 取值；空串 = 纯文本，不带格式化（默认，行为与旧版一致）
_PARSE_MODES = ('', 'HTML', 'MarkdownV2', 'Markdown')


def _utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2


def _split_text(text: str, limit: int) -> list:
    """按 UTF-16 长度切分，优先在换行/空格处断开。

    切点处不做 parse_mode 闭合补偿：调用方在解析失败时整体降级纯文本，
    比在任意位置插入闭合标签更安全（实体可能跨行）。
    """
    text = str(text or '')
    if limit <= 0 or _utf16_len(text) <= limit:
        return [text]
    chunks, buf, buf_len = [], [], 0
    for ch in text:
        n = _utf16_len(ch)
        if buf_len + n > limit:
            piece = ''.join(buf)
            # 回退到最后一个换行/空格，避免把单词或一行切两半
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
    segs: list[dict] = []

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


def normalize_event(update: dict, bot_name: str) -> dict | None:
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
        # 论坛/话题群：回帖时要带上同一个 thread，否则会掉到 General 话题
        'thread_id': msg.get('message_thread_id'),
        'raw': msg,
    }
    return event


class TelegramAdapter(ProtocolAdapter):
    """Telegram 长轮询接入端"""

    adapter_id = 'telegram'

    def __init__(self, framework, token: str, bot_name: str = 'telegram',
                 polling_timeout: int = 30, api_base: str = _API_BASE,
                 allowed_updates=None, parse_mode: str = ''):
        self.framework = framework
        self.token = str(token or '').strip()
        self.bot_name = bot_name or 'telegram'
        self.polling_timeout = max(1, int(polling_timeout or 30))
        self.api_base = (api_base or _API_BASE).rstrip('/')
        self.parse_mode = self._normalize_parse_mode(parse_mode)
        # callback_query 必须显式订阅：getUpdates 的 allowed_updates 是白名单，
        # 不写就永远收不到，下面的按钮回调归一化就成了死代码
        self.allowed_updates = allowed_updates or [
            'message', 'edited_message', 'callback_query']
        self._offset = self._load_offset()
        # 409（webhook 冲突）只完整提示一次，否则日志会被同一条刷满
        self._webhook_conflict_warned = False
        self._connected = False
        self._closing = False
        self._task = None
        self._me = None
        self._http = requests.Session()

    def _url(self, method: str) -> str:
        return f'{self.api_base}/bot{self.token}/{method}'

    @staticmethod
    def _target_from(params: dict):
        """(chat_id, message_id)：chat_id 允许用 group_id/user_id 代替。"""
        chat_id = params.get('chat_id')
        if chat_id is None:
            chat_id = params.get('group_id')
        if chat_id is None:
            chat_id = params.get('user_id')
        mid = params.get('message_id')
        if mid is None:
            mid = params.get('msg_id')
        try:
            mid = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid = None
        return chat_id, mid

    @staticmethod
    def _normalize_parse_mode(value) -> str:
        """配置值 → 官方 parse_mode 常量；不认识的值按纯文本处理，不静默报错。"""
        mode = str(value or '').strip()
        for allowed in _PARSE_MODES:
            if allowed and allowed.lower() == mode.lower():
                return allowed
        if mode:
            logger.warning('telegram parse_mode 取值 %r 不受支持，已按纯文本发送'
                           '（可选：HTML / MarkdownV2 / Markdown）', mode)
        return ''

    # ── offset 持久化 ────────────────────────────────────────────
    # offset 是「已确认到哪一条」的唯一凭据。只放内存时：进程重启 → offset 归 0
    # → getUpdates 把最近 24 小时未确认的旧消息重新投递一遍，用户收到重复回复；
    # 而入站去重窗口（300s）在重启后已清空，挡不住。
    # 写入失败只告警，绝不因为状态文件影响收消息。

    def _offset_file(self) -> str:
        from framework.config import get_home
        return os.path.join(get_home(), 'data', 'telegram_offset.json')

    def _token_hint(self) -> str:
        import hashlib
        return hashlib.sha1(self.token.encode('utf-8')).hexdigest()[:12]

    def _load_offset(self) -> int:
        try:
            with open(self._offset_file(), 'r', encoding='utf-8') as f:
                state = json.load(f) or {}
            if state.get('token_hint') != self._token_hint():
                logger.info('telegram offset 记录属于另一个 bot，忽略并从头开始')
                return 0
            value = int(state.get('offset') or 0)
            if value > 0:
                logger.info('telegram offset 已从磁盘恢复: %s', value)
            return value
        except FileNotFoundError:
            return 0
        except Exception as e:
            logger.warning('telegram offset 读取失败，从 0 开始: %s', e)
            return 0

    def _save_offset(self):
        try:
            path = self._offset_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'token_hint': self._token_hint(),
                           'offset': int(self._offset),
                           'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}, f)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning('telegram offset 保存失败（不影响收消息）: %s', e)

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
                {'key': 'parse_mode', 'label': '解析模式(HTML/MarkdownV2/空)',
                 'type': 'string'},
            ],
            'restart_keys': ['token', 'bot_name', 'parse_mode'],
            'endpoint_hint': f'@{self.bot_name}（getUpdates 长轮询）',
            'guide': '向 @BotFather 申请 token 填入；本端长轮询 getUpdates，'
                     '出站 sendMessage/sendPhoto（图片支持 base64://）。'
                     'parse_mode 留空=纯文本（默认），填 HTML 或 MarkdownV2 时'
                     '正文按该格式解析，解析失败会自动降级纯文本重发一次。',
            'status_extra': {
                'connected': self._connected,
                'offset': self._offset,
                'username': (self._me or {}).get('username', ''),
            },
        }

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_EDIT,
            CAP_FILE,
            CAP_IMAGE,
            CAP_NOTICE,
            CAP_REACT,
            CAP_RECALL,
            CAP_STICKER,
            CAP_TEXT,
            CAP_VIDEO,
            CAP_VOICE,
            Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE,
                     CAP_STICKER, CAP_NOTICE],
            outbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE,
                      CAP_STICKER, CAP_RECALL, CAP_EDIT, CAP_REACT],
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
            canonical_type,
            media_ref,
            segments_to_text,
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

        backoff = Backoff(base=3, maximum=60)
        while not self._closing:
            try:
                updates = await asyncio.to_thread(self._get_updates)
                _offset_before = self._offset
                for upd in updates or []:
                    if self._closing:
                        break
                    self._offset = max(self._offset,
                                       int(upd.get('update_id', 0)) + 1)
                    event = normalize_event(upd, self.bot_name)
                    if event is None:
                        continue
                    # 入站统一净化：剥顶层注入字段、强制归属为当前接入端。
                    event = sanitize_adapter_event(event, self.bot_name)
                    try:
                        await self.framework.dispatch_event(event)
                    except Exception as e:
                        logger.error('telegram 事件分发失败: %s', e)
                # 整批处理完再落盘：崩溃在批中间会重放这一批（at-least-once），
                # 但不会静默跳过任何一条。
                if self._offset != _offset_before:
                    self._save_offset()
                self._connected = True
                backoff.reset()
            except asyncio.CancelledError:
                raise
            except TelegramApiError as e:
                self._connected = False
                if e.error_code == 409:
                    # 409 = 该 bot 还挂着 webhook，getUpdates 与 webhook 互斥。
                    # 这不是网络抖动，重试一万次也一样失败，必须说清楚怎么修。
                    if not self._webhook_conflict_warned:
                        self._webhook_conflict_warned = True
                        logger.error(
                            'telegram 收消息失败：该 bot 仍配置了 webhook（409 Conflict）。'
                            'getUpdates 与 webhook 只能二选一。两种修法：'
                            '① 浏览器打开 https://api.telegram.org/bot<token>/deleteWebhook 删除；'
                            '② 或改成本端不轮询、由你的服务接 webhook 后转 http_inject。')
                    await asyncio.sleep(300)
                    continue
                if e.error_code == 429:
                    wait = e.retry_after or 30
                    logger.warning(
                        'telegram 被限流：按官方 retry_after 等待 %ss 后继续', wait)
                    await asyncio.sleep(wait)
                    continue
                if e.error_code == 401:
                    logger.error('telegram token 无效（401 Unauthorized），'
                                 '轮询已无意义，请检查配置里的 token')
                    await asyncio.sleep(300)
                    continue
                logger.warning('telegram 轮询失败: %s', e)
                await asyncio.sleep(backoff.next_delay(0))
            except Exception as e:
                self._connected = False
                logger.warning('telegram 轮询异常: %s', e)
                # 长轮询正常返回即算成功（上面已 reset）；这里的退避只在连续失败时增长
                await asyncio.sleep(backoff.next_delay(0))

    def _get_me(self) -> dict:
        resp = self._http.get(self._url('getMe'), timeout=15)
        data = resp.json() if resp.content else {}
        if data.get('ok'):
            return data.get('result') or {}
        raise TelegramApiError.from_payload(data)

    def _result_or_error(self, payload, action='发送') -> dict:
        """把 ok=false 统一翻成带 retry_after 的失败结构。

        retcode 沿用出站闸门的语义：
          -4 = 被限流（throttled），插件/闸门据此判断「等一等可以重发」；
          -1 = 其它平台业务错误（参数错、chat 不存在、权限不足等）。
        """
        if isinstance(payload, dict) and payload.get('ok'):
            return {'status': 'ok', 'retcode': 0, 'data': payload.get('result')}
        err = TelegramApiError.from_payload(payload)
        if err.error_code == 429:
            return {'status': 'failed', 'retcode': -4, 'throttled': True,
                    'retry_after': err.retry_after,
                    'msg': f'{action}被限流，官方要求等待 '
                           f'{err.retry_after or "?"}s：{err.description}'}
        return {'status': 'failed', 'retcode': -1,
                'error_code': err.error_code,
                'msg': f'{action}失败：{err.description or payload}'}

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
            raise TelegramApiError.from_payload(data)
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
                      reply_to: int = None,
                      thread_id: int = None) -> dict:
        """发文本：按 4096 上限切条，引用只挂在第一条上。"""
        chunks = _split_text(text, _MAX_TEXT_LEN)
        last = None
        for i, chunk in enumerate(chunks):
            last = self._send_text_once(
                chat_id, chunk, reply_to if i == 0 else None, thread_id)
            if last.get('status') != 'ok':
                break
        return last or {'status': 'failed', 'retcode': -1, 'msg': '消息为空'}

    def _send_text_once(self, chat_id, text: str, reply_to: int = None,
                        thread_id: int = None) -> dict:
        body = {'chat_id': chat_id, 'text': text}
        if reply_to:
            body['reply_parameters'] = json.dumps({'message_id': reply_to})
        if thread_id:
            body['message_thread_id'] = int(thread_id)
        if self.parse_mode:
            body['parse_mode'] = self.parse_mode
        data = self._post_json('sendMessage', body)
        # 实体解析失败（未转义的 _ * [ 等）是 400 且描述固定含 can't parse entities：
        # 这时整条消息是发不出去的，降级成纯文本重发一次，而不是让插件看到失败。
        if (self.parse_mode and data.get('error_code') == 400
                and "can't parse entities" in str(data.get('description') or '')):
            logger.warning('telegram %s 解析失败，本条降级为纯文本重发：%s',
                           self.parse_mode, data.get('description'))
            body.pop('parse_mode', None)
            data = self._post_json('sendMessage', body)
        return self._result_or_error(data, 'sendMessage')

    def _post_json(self, method: str, body: dict, timeout: int = 30) -> dict:
        resp = self._http.post(
            self._url(method), json=body, timeout=timeout,
            headers={'Content-Type': 'application/json; charset=utf-8'})
        return resp.json() if resp.content else {}

    def _delete_message(self, chat_id, message_id) -> dict:
        """官方限制：只能删 48 小时内的消息，超时返回 400。"""
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'message_id 非法: {message_id!r}'}
        data = self._post_json('deleteMessage',
                               {'chat_id': chat_id, 'message_id': mid})
        if data.get('ok'):
            return {'status': 'ok', 'retcode': 0, 'data': True}
        err = TelegramApiError.from_payload(data)
        desc = err.description.lower()
        if err.error_code == 400 and ('not found' in desc or 'too old' in desc):
            return {'status': 'failed', 'retcode': -1,
                    'msg': '删除失败：消息不存在或已超过 48 小时删除窗口'
                           '（Telegram 只允许删 48 小时内的消息）'}
        return self._result_or_error(data, 'deleteMessage')

    def _edit_message(self, chat_id, message_id, text: str) -> dict:
        # 不收 thread_id：editMessageText 按 message_id 定位，没有该字段；
        # 留一个不读的参数只会让人以为论坛话题的编辑需要它。
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'message_id 非法: {message_id!r}'}
        body = {'chat_id': chat_id, 'message_id': mid, 'text': text}
        if self.parse_mode:
            body['parse_mode'] = self.parse_mode
        data = self._post_json('editMessageText', body)
        if (self.parse_mode and data.get('error_code') == 400
                and "can't parse entities" in str(data.get('description') or '')):
            body.pop('parse_mode', None)
            data = self._post_json('editMessageText', body)
        return self._result_or_error(data, 'editMessageText')

    def _set_reaction(self, chat_id, message_id, emoji: str) -> dict:
        """emoji 为空 = 清空该消息上本 bot 的回应。"""
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'message_id 非法: {message_id!r}'}
        reaction = [{'type': 'emoji', 'emoji': emoji}] if emoji else []
        data = self._post_json(
            'setMessageReaction',
            {'chat_id': chat_id, 'message_id': mid, 'reaction': reaction})
        if data.get('ok'):
            return {'status': 'ok', 'retcode': 0, 'data': True}
        return self._result_or_error(data, 'setMessageReaction')

    def _send_media(self, chat_id, op: dict, reply_to=None,
                    thread_id=None) -> dict:
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
            return {'status': 'failed', 'retcode': -1,
                    'msg': f'{method} 缺少文件引用'}

        body = {'chat_id': chat_id}
        caption = op.get('caption') or ''
        overflow = ''
        if _utf16_len(caption) > _MAX_CAPTION_LEN:
            # caption 上限 1024，超了整条会 400；正文改由随后的文本消息承载
            overflow, caption = caption, ''
        if caption:
            body['caption'] = caption
            # caption 与正文用同一个 parse_mode：配置开了 HTML/MarkdownV2 却只
            # 对纯文本生效，是让配置项静默失效。解析失败由下面统一降级。
            if self.parse_mode:
                body['parse_mode'] = self.parse_mode
        if reply_to:
            body['reply_parameters'] = json.dumps({'message_id': reply_to})
        if thread_id:
            body['message_thread_id'] = int(thread_id)

        name = str(data.get('name') or '')
        files = None
        b64 = _extract_b64(ref)
        if b64:
            try:
                raw = base64.b64decode(re.sub(r'\s+', '', b64))
            except Exception as e:  # noqa: BLE001
                return {'status': 'failed', 'retcode': -1,
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

        def _post_media():
            if files is None:
                resp = self._http.post(
                    self._url(method), json=body, timeout=60,
                    headers={'Content-Type': 'application/json; charset=utf-8'})
            else:
                resp = self._http.post(
                    self._url(method), data=body, files=files, timeout=60)
            return resp.json() if resp.content else {}

        try:
            payload = _post_media()
        except Exception as e:  # noqa: BLE001
            return {'status': 'failed', 'retcode': -3, 'msg': f'{method} 失败: {e}'}
        # caption 的实体解析失败同样是整条 400（媒体和 caption 一起发不出去），
        # 与文本路径一样降级纯文本重发；files 是内存 bytes，重发不产生二次读盘。
        if (self.parse_mode and payload.get('error_code') == 400
                and "can't parse entities" in str(payload.get('description') or '')):
            logger.warning('telegram %s 的 caption 解析失败，本条降级为纯文本重发：%s',
                           self.parse_mode, payload.get('description'))
            body.pop('parse_mode', None)
            try:
                payload = _post_media()
            except Exception as e:  # noqa: BLE001
                return {'status': 'failed', 'retcode': -3,
                        'msg': f'{method} 失败: {e}'}
        result = self._result_or_error(payload, method)
        if overflow and result.get('status') == 'ok':
            sent = self._send_message(chat_id, overflow, None, thread_id)
            if sent.get('status') != 'ok':
                return sent
        return result

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
        return self._result_or_error(body, 'sendPhoto')

    # ── ProtocolAdapter 契约 ────────────────────────────────────

    async def handle_event(self, raw_event: dict, bot_name: str) -> dict | None:
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
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'send_msg 需要 chat_id/user_id/group_id'}
            reply_to = params.get('reply_to') or params.get('message_id')
            thread_id = (params.get('message_thread_id')
                         or params.get('thread_id'))
            # 规范消息段 → Telegram 发送指令（支持图/语音/视频/文件/贴纸）
            from framework.messaging.segments import normalize_message as _canon
            ops = self.to_native(_canon(message))
            last = None
            for op in ops:
                if op.get('method') == 'sendMessage':
                    last = await asyncio.to_thread(
                        self._send_message, chat_id,
                        (op.get('data') or {}).get('text', ''), reply_to,
                        thread_id)
                else:
                    last = await asyncio.to_thread(
                        self._send_media, chat_id, op, reply_to, thread_id)
            return last or {'status': 'failed', 'retcode': -1, 'msg': '发送无内容'}

        if action in ('delete_msg', 'recall', 'delete_message'):
            chat_id, mid = self._target_from(params)
            if chat_id is None or mid is None:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'delete_msg 需要 chat_id(或 group_id/user_id) '
                               '与 message_id'}
            return await asyncio.to_thread(self._delete_message, chat_id, mid)

        if action in ('edit_msg', 'edit_message', 'edit_text'):
            chat_id, mid = self._target_from(params)
            text = params.get('message', params.get('text', ''))
            if chat_id is None or mid is None:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'edit_msg 需要 chat_id 与 message_id'}
            return await asyncio.to_thread(
                self._edit_message, chat_id, mid, str(text))

        if action in ('set_reaction', 'react'):
            chat_id, mid = self._target_from(params)
            emoji = str(params.get('emoji') or params.get('reaction') or '')
            if chat_id is None or mid is None:
                return {'status': 'failed', 'retcode': -1,
                        'msg': 'set_reaction 需要 chat_id 与 message_id'
                               '（emoji 留空表示清除回应）'}
            return await asyncio.to_thread(
                self._set_reaction, chat_id, mid, emoji)

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
        parse_mode=cfg.get('parse_mode') or '',
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
