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
import itertools
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse

import requests
import websockets

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

# msg_seq：QQ 官方要求同一 msg_id 下 seq 不重复；秒级取模会同秒碰撞，用进程内计数器
_msg_seq_counter = itertools.count(1)


def _next_msg_seq() -> int:
    return (next(_msg_seq_counter) - 1) % 9999 + 1

__plugin_meta__ = {
    "name": "qq_official",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "QQ 官方机器人接入端：WSS 网关 + OpenAPI 收发",
    "priority": 5,
    "official": True,
    "process": "core",
}

# 官方接入域名（2026-09-18 指引）：取令牌 bots.qq.com，正式 API api.sgroup.qq.com，
# 沙箱 sandbox.api.sgroup.qq.com（旧域名 api.bot.qq.com 处于迁移窗口期，仅保留作回退）
_API_BASE_FALLBACK = 'https://api.bot.qq.com'
_API_BASE = 'https://api.sgroup.qq.com'
_TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
_GATEWAY_URL = f'{_API_BASE}/gateway/bot'

# GROUP_AND_C2C_EVENT(1<<25)：群 @/全量 + 单聊 + 群/好友生命周期
# INTERACTION(1<<26)：互动事件（按钮回调、快捷菜单、消息反馈等）
# 两者都订阅才能既收消息又收按钮回调；只想收消息就把 intents 改回 33554432
_DEFAULT_INTENTS = (1 << 25) | (1 << 26)

# 需要 ACK 的互动类型：11=消息按钮回调，12=单聊快捷菜单回调。
# 其余（13 消息反馈 / 14 清空会话 / 18-20 授权等）官方明确无需回应，
# 回了反而报"已回应过"，所以不在自动 ACK 范围内。
_ACK_INTERACTION_TYPES = (11, 12)

# ── 出站消息类型（QQ 开放平台 msg_type）────────────────────────
_MSG_TYPE_TEXT = 0        # 纯文本
_MSG_TYPE_MARKDOWN = 2    # markdown（可带 keyboard / 模板）
_MSG_TYPE_ARK = 3         # ark 模板
_MSG_TYPE_EMBED = 4       # embed
_MSG_TYPE_MEDIA = 7       # 富媒体（图片/语音/视频/文件）

# 消息段类型 → 上传 file_type（1图 2视频 3语音 4文件）
_FILE_TYPE = {'image': 1, 'video': 2, 'record': 3, 'voice': 3, 'file': 4}
_MEDIA_SEGS = ('image', 'record', 'voice', 'video', 'file')
# 段类型 → 上传文件后缀（用于 multipart 文件名，服务端按后缀识别）
_MEDIA_EXT = {'image': 'png', 'video': 'mp4', 'record': 'silk',
              'voice': 'silk', 'file': 'bin'}

# ── 入站事件分类 ──────────────────────────────────────────────
_MESSAGE_EVENTS = frozenset({
    'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE',
    'C2C_MESSAGE_CREATE', 'DIRECT_MESSAGE_CREATE', 'MESSAGE_CREATE',
})
_INTERACTION_EVENT = 'INTERACTION_CREATE'
# 生命周期类：机器人进/出群、好友、群成员增减、消息拒绝/接收、订阅状态
_LIFECYCLE_EVENTS = {
    'GROUP_ADD_ROBOT': 'group_add_robot',
    'GROUP_DEL_ROBOT': 'group_del_robot',
    'GROUP_MSG_REJECT': 'group_msg_reject',
    'GROUP_MSG_RECEIVE': 'group_msg_receive',
    'C2C_MSG_REJECT': 'c2c_msg_reject',
    'C2C_MSG_RECEIVE': 'c2c_msg_receive',
    'GROUP_MEMBER_ADD': 'group_increase',
    'GROUP_MEMBER_REMOVE': 'group_decrease',
    'FRIEND_ADD': 'friend_add',
    'FRIEND_DEL': 'friend_del',
    'GROUP_JOIN_REQUEST': 'group_join_request',
    'SUBSCRIBE_MESSAGE_STATUS': 'subscribe_status',
}

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


def _read_file_bytes(path: str) -> Optional[bytes]:
    """线程池内读文件（async 路径避免阻塞事件循环）"""
    try:
        with open(path, 'rb') as f:
            return f.read()
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


_IMG_EXT = {'image/png': 'png', 'image/jpeg': 'jpg',
            'image/gif': 'gif', 'image/webp': 'webp'}


def _image_filename(mime: str) -> str:
    """按嗅探到的 mime 定上传文件名扩展名（群/单聊上传统一用，不再硬编码 .png）"""
    return f'image.{_IMG_EXT.get(mime, "png")}'


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


def _first_media(message):
    """取出第一个富媒体段（图/语音/视频/文件），没有则返回 None。

    返回 (seg_type, data_dict)。文本段之外的段按 _MEDIA_SEGS 顺序优先取第一个。
    """
    if isinstance(message, str):
        message = normalize_message(message)
    if not isinstance(message, list):
        return None
    for seg in message:
        if isinstance(seg, dict) and seg.get('type') in _MEDIA_SEGS:
            return seg.get('type'), dict(seg.get('data') or {})
    return None


def _split_outgoing(message):
    """把出站 message 拆成 (文本, 富媒体段, markdown文本, 按钮行)。

    这一段存在的理由：QQ 官方一条消息只能带一种主体（文本 / markdown / 媒体），
    所以插件塞进来的混合内容必须先拆干净，再决定 msg_type。
    优先级：markdown > 富媒体 > 文本（按钮作为附属字段挂在 markdown/文本上）。
    """
    if isinstance(message, str):
        message = normalize_message(message)
    if not isinstance(message, list):
        message = [{'type': 'text', 'data': {'text': _message_to_text(message)}}] \
            if message is not None else []

    texts, markdown, buttons, media = [], None, None, None
    for seg in message:
        if not isinstance(seg, dict):
            texts.append(str(seg))
            continue
        seg_type = seg.get('type') or 'text'
        data = dict(seg.get('data') or {})
        if seg_type == 'text':
            texts.append(str(data.get('text') or ''))
        elif seg_type == 'at':
            # 官方不支持 CQ:at，退化成 @昵称 文本，避免整条消息被吞
            texts.append('@' + str(data.get('name') or data.get('qq') or ''))
        elif seg_type == 'markdown':
            markdown = str(data.get('content') or data.get('text') or '')
        elif seg_type in ('keyboard', 'buttons'):
            buttons = data.get('buttons') or data.get('rows') or []
        elif seg_type in _MEDIA_SEGS and media is None:
            media = (seg_type, data)
    return ''.join(texts).strip(), media, markdown, buttons


def build_keyboard(buttons) -> Optional[dict]:
    """把简洁按钮定义翻成 QQ InlineKeyboard 结构。

    入参形态（两种都收）：
      1. 扁平列表：[{'text':'点我', 'data':'do_thing'}, ...]      → 一行
      2. 分行列表：[[{...}, {...}], [{...}]]                     → 多行
    按钮字段：text(文案) / data(回调数据) / link(跳转，优先) /
              style(0灰1蓝2红) / show(点击后文案) / id(可选)
    """
    if not buttons:
        return None
    if isinstance(buttons, dict):
        rows = buttons.get('rows') or buttons.get('buttons') or []
    elif isinstance(buttons, list):
        # 元素全是 dict → 扁平列表，视作单行；否则按"每行一个 list"处理
        dicts = [b for b in buttons if isinstance(b, dict)]
        flat = bool(dicts) and len(dicts) == len(buttons)
        rows = [buttons] if flat else buttons
    else:
        return None

    built_rows, next_id = [], 0
    for row in rows:
        if isinstance(row, dict):
            row = row.get('buttons') or row.get('btns') or [row]
        if not isinstance(row, (list, tuple)):
            continue
        items = []
        for btn in row:
            if not isinstance(btn, dict):
                continue
            action = {'type': 2, 'data': str(btn.get('data') or '')}
            if btn.get('link'):
                action = {'type': 0, 'data': str(btn['link'])}
            if btn.get('enter') is True and action['type'] == 2:
                action['type'] = 1        # 回车即发送
            render = {
                'label': str(btn.get('text') or btn.get('label') or '按钮'),
                'style': int(btn.get('style', 1) or 0),
            }
            if btn.get('show'):
                render['visited_label'] = str(btn['show'])
            item = {
                'id': str(btn.get('id') if btn.get('id') not in (None, '')
                          else next_id),
                'render_data': render,
                'action': action,
            }
            if btn.get('reply') is True:
                action['reply'] = True
            if btn.get('group_id') is not None:
                item['group_id'] = btn['group_id']
            next_id += 1
            items.append(item)
        if items:
            built_rows.append({'buttons': items})
    if not built_rows:
        return None
    return {'content': {'rows': built_rows}}


def _mime_for(seg_type: str, raw: bytes, filename: str = '') -> str:
    """按段类型定上传 mime；图片继续按字节嗅探，其它按类型给默认值。"""
    if seg_type == 'image':
        return _sniff_image_mime(raw)
    if seg_type == 'video':
        return 'video/mp4'
    if seg_type in ('record', 'voice'):
        return 'audio/silk'
    ext = (filename.rsplit('.', 1)[-1] if '.' in filename else '').lower()
    if ext in ('mp3', 'wav', 'ogg', 'flac', 'm4a', 'amr'):
        return f'audio/{ext}'
    return 'application/octet-stream'


def _media_name(seg_type: str, mime: str, filename: str = '') -> str:
    """上传时的 multipart 文件名（必须带正确后缀，QQ 侧按后缀判定类型）"""
    if filename:
        return filename
    if seg_type == 'image':
        return _image_filename(mime)
    return f'file.{_MEDIA_EXT.get(seg_type, "bin")}'


def normalize_interaction(raw: dict, bot_name: str) -> Optional[dict]:
    """INTERACTION_CREATE（按钮回调等）→ 内部 notice 事件 dict。

    回调体：d.id 为 interaction_id（同时是被动回复用的 event_id），
    d.data.resolved.button_data 是按钮携带的数据，d.chat_type/scene 区分群与单聊。
    """
    if not isinstance(raw, dict):
        return None
    d = raw.get('d') if isinstance(raw.get('d'), dict) else raw
    data = d.get('data') if isinstance(d.get('data'), dict) else {}
    resolved = data.get('resolved') if isinstance(data.get('resolved'), dict) else {}
    group_openid = d.get('group_openid') or d.get('group_id') or ''
    user_id = (d.get('group_member_openid') or d.get('user_openid')
               or ((d.get('author') or {}) or {}).get('id') or '')
    is_group = bool(group_openid) or str(d.get('chat_type')) == '1' \
        or d.get('scene') == 'group'
    return {
        'type': 'notice',
        'notice_type': 'interaction',
        'bot_name': bot_name,
        'user_id': user_id,
        'group_id': group_openid or None,
        'message_id': d.get('id') or '',
        'interaction_id': d.get('id') or '',
        'interaction_type': int(d.get('type') or 0),
        'button_data': str(resolved.get('button_data') or ''),
        'button_id': str(resolved.get('button_id') or ''),
        'feature_id': str(resolved.get('feature_id') or ''),
        'scene': d.get('scene') or ('group' if is_group else 'c2c'),
        'timestamp': d.get('timestamp') or '',
        'message_type': 'group' if is_group else 'private',
        'adapter': 'qq_official',
        'qq_event': _INTERACTION_EVENT,
        'raw': d,
    }


def normalize_notice(raw: dict, bot_name: str, notice_type: str,
                     event_t: str) -> Optional[dict]:
    """QQ 生命周期事件 → 内部 notice 事件 dict（走框架的 notice.* 事件总线）。"""
    if not isinstance(raw, dict):
        return None
    d = raw.get('d') if isinstance(raw.get('d'), dict) else raw
    group_openid = d.get('group_openid') or d.get('group_id') or ''
    op = d.get('op_member_openid') or d.get('op_user_openid') or ''
    target = (d.get('target_openid') or d.get('user_openid')
              or d.get('member_openid') or d.get('group_member_openid') or '')
    return {
        'type': 'notice',
        'notice_type': notice_type,
        'bot_name': bot_name,
        'user_id': target or op,
        'operator_id': op,
        'group_id': group_openid or None,
        'sub_type': notice_type,
        'timestamp': d.get('timestamp') or '',
        'adapter': 'qq_official',
        'qq_event': event_t,
        'raw': d,
    }


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
        # 附件结构化透传（content 仍是纯文本，插件可另读 attachments）
        'attachments': attachments,
        'raw': d,
    }
    if event_t:
        event['qq_event'] = event_t
    return event


class QQOfficialAdapter(ProtocolAdapter):
    """QQ 官方机器人：WSS 收事件 + OpenAPI 发消息"""

    adapter_id = 'qq_official'

    # 互动回调是否自动 ACK（关掉则由插件自行调用 ack_interaction）
    auto_ack_interaction = True
    # 生命周期事件（进/出群、好友、成员增减）是否转成 notice 分发
    enable_notice_events = True

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
        self._token_url = _TOKEN_URL
        # 旧域名保留作回退（官方迁移窗口期）：新域名连不上/5xx 时自动重试
        self.api_base_fallback = (_API_BASE_FALLBACK
                                  if self.api_base != _API_BASE_FALLBACK else '')

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
        self._pending_reply = {}        # {会话key: (msg_id, ts, kind)} kind=group/private
        self._seen_events = OrderedDict()   # event_id 幂等去重（Resume 重放/服务重发）
        self._boot_waiting = False
        self._token_lock = threading.Lock()  # 双路径刷新共用（async / to_thread 同步）
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

    _TOKEN_REFRESH_MARGIN = 60   # 统一的提前刷新阈值（原 async 60s / sync 30s 不一致）

    def _refresh_token_sync(self) -> str:
        """同步刷新 access_token（线程内执行；带锁防 async/to_thread 双路径并发重复刷）"""
        with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expire_at - self._TOKEN_REFRESH_MARGIN:
                return self._access_token     # 拿锁后复查，已被并发线程刷过
            # 官方 2026-09-18 起取令牌域名迁移到 bots.qq.com；旧地址保留作回退
            token_urls = [self._token_url]
            fallback_token_url = f'{_API_BASE_FALLBACK}/app/getAppAccessToken'
            if fallback_token_url not in token_urls:
                token_urls.append(fallback_token_url)
            body = {}
            resp = None
            for tok_url in token_urls:
                resp = self._http.post(tok_url, json={
                    'appId': self.app_id, 'clientSecret': self.app_secret,
                }, timeout=15)
                body = resp.json() if resp.content else {}
                if body.get('access_token') or body.get('accessToken'):
                    break
            token = body.get('access_token') or body.get('accessToken') or ''
            if not token:
                code = resp.status_code if resp is not None else 'N/A'
                raise RuntimeError(f'获取 access_token 失败: HTTP {code} {body}')
            expires = int(body.get('expires_in') or body.get('expiresIn') or 7200)
            self._access_token = token
            self._token_expire_at = time.time() + max(60, expires)
            logger.info('qq_official access_token 已刷新，有效期 %ss', expires)
            return token

    def _refresh_token_if_stale(self) -> str:
        """已持有未过期 token 直接用，否则同步刷新（供 to_thread 线程内调用）"""
        if self._access_token and time.time() < self._token_expire_at - self._TOKEN_REFRESH_MARGIN:
            return self._access_token
        return self._refresh_token_sync()

    async def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expire_at - self._TOKEN_REFRESH_MARGIN:
            return self._access_token
        return await asyncio.to_thread(self._refresh_token_sync)

    async def _fetch_gateway(self) -> str:
        token = await self._ensure_token()
        headers = {'Authorization': f'QQBot {token}'}
        base = self.api_base
        fb = getattr(self, 'api_base_fallback', '')
        hosts = [base, fb] if fb else [base]
        last_exc = None
        for host in hosts:
            url = f'{host}/gateway/bot'
            try:
                resp = await asyncio.to_thread(
                    self._http.get, url, headers=headers, timeout=15)
            except Exception as e:
                last_exc = e
                continue
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                last_exc = RuntimeError(
                    f'HTTP {resp.status_code} {data}') if data else \
                    RuntimeError(f'HTTP {resp.status_code}')
                continue
            ws_url = data.get('url') or data.get('wss') or ''
            if not ws_url:
                last_exc = RuntimeError(f'响应无 url: {data}')
                continue
            if '://' not in ws_url:
                ws_url = f'wss://{ws_url}'
            if not ws_url.startswith(('ws://', 'wss://')):
                ws_url = 'wss://' + ws_url.lstrip('/')
            # 去掉可能的 http(s) 前缀统一成 wss
            if ws_url.startswith('https://'):
                ws_url = 'wss://' + ws_url[8:]
            elif ws_url.startswith('http://'):
                ws_url = 'ws://' + ws_url[7:]
            return ws_url
        raise RuntimeError(f'获取 gateway 失败: {last_exc}')

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_AT, CAP_FILE, CAP_GROUP_ADMIN, CAP_IMAGE, CAP_KEYBOARD,
            CAP_MARKDOWN, CAP_NOTICE, CAP_RECALL, CAP_REPLY, CAP_REQUEST,
            CAP_TEXT, CAP_VIDEO, CAP_VOICE, Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE,
                     CAP_AT, CAP_REPLY, CAP_NOTICE, CAP_REQUEST],
            outbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE,
                      CAP_AT, CAP_REPLY, CAP_MARKDOWN, CAP_KEYBOARD],
            actions=[CAP_RECALL, CAP_GROUP_ADMIN],
        )

    def normalize_incoming(self, raw_message) -> list:
        """
        QQ 官方事件 → 规范消息段。

        入站是 content 文本 + attachments[]（QQ 官方把图片/语音/视频放在
        attachments 里），这里按 attachment 的 content_type 翻成规范段。
        """
        from framework.messaging.segments import normalize_message
        if isinstance(raw_message, list):
            return normalize_message(raw_message)
        if isinstance(raw_message, str):
            return normalize_message(raw_message)
        if not isinstance(raw_message, dict):
            return []

        segs = []
        content = raw_message.get('content') or raw_message.get('text') or ''
        if content:
            segs.append({'type': 'text', 'data': {'text': content}})
        for att in raw_message.get('attachments') or []:
            if not isinstance(att, dict):
                continue
            ctype = str(att.get('content_type') or '')
            url = att.get('url') or ''
            if ctype.startswith('image/') or att.get('file_type') == 1:
                stype = 'image'
            elif ctype.startswith('video/') or att.get('file_type') == 2:
                stype = 'video'
            elif ctype.startswith('audio/') or att.get('file_type') == 3:
                stype = 'voice'
            else:
                stype = 'file'
            segs.append({'type': stype, 'data': {
                'url': url, 'file_info': att.get('file_info') or '',
                'name': att.get('filename') or '', 'mime': ctype,
                'size': att.get('size'), 'duration': att.get('duration'),
                'width': att.get('width'), 'height': att.get('height'),
            }})
        return segs or [{'type': 'text', 'data': {'text': ''}}]

    def to_native(self, segments: list):
        """
        规范段 → QQ 官方发送结构。

        QQ 官方的 msg_type 互斥：文本=0、markdown=2、富媒体=7；按钮只能挂在
        markdown 上。这里按段组合自动选型，插件不用关心这个限制。
        """
        from framework.messaging.segments import (
            canonical_type, media_ref, segments_to_text,
        )
        text = segments_to_text(segments)
        media = None
        media_type = None
        for s in segments or []:
            if not isinstance(s, dict):
                continue
            stype = canonical_type(s.get('type'))
            if stype in ('image', 'voice', 'video', 'file') and media is None:
                if not media_ref(s):
                    continue
                media = s
                media_type = stype
        keyboard = None
        for s in segments or []:
            if isinstance(s, dict) and canonical_type(s.get('type')) == 'keyboard':
                keyboard = s.get('data') or {}
                break
        if media is not None:
            return {'msg_type': 7, 'media': media, 'media_type': media_type,
                    'content': text, 'keyboard': keyboard}
        if keyboard is not None:
            return {'msg_type': 2, 'markdown': {'content': text},
                    'keyboard': keyboard}
        return {'msg_type': 0, 'content': text, 'keyboard': None}

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        self._schedule_connect()

    def _schedule_connect(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is not None and loop.is_running():
            if self._supervisor is None or self._supervisor.done():
                self._supervisor = loop.create_task(self._supervise())
            return
        # loop 未就绪：起守护线程等就绪后线程安全地调度。旧的
        # get_event_loop().call_soon 路径在 3.10+ 会静默失效，适配器可能永不启动
        if self._boot_waiting:
            return
        self._boot_waiting = True

        def _wait():
            for _ in range(120):
                lp = getattr(self.framework, 'loop', None)
                if lp is not None and lp.is_running():
                    self._boot_waiting = False
                    lp.call_soon_threadsafe(self._schedule_connect)
                    return
                time.sleep(0.5)
            self._boot_waiting = False
            logger.error('qq_official 等待框架事件循环超时（60s），连接未启动')

        threading.Thread(target=_wait, daemon=True,
                         name='qq_official-boot-wait').start()

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
        identify_token = f'QQBot {token}'
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
            # 互动事件（按钮回调等）→ notice 事件并自动 ACK
            if event_t == _INTERACTION_EVENT:
                await self._on_interaction(d)
                continue
            # 生命周期事件（进群/退群/好友/成员增减…）→ notice 事件
            if event_t in _LIFECYCLE_EVENTS:
                await self._on_lifecycle(event_t, d)
                continue
            # 其余非消息事件一律忽略
            if event_t not in _MESSAGE_EVENTS:
                continue
            wrapped = dict(d)
            wrapped['_t'] = event_t
            wrapped['id'] = d.get('id') or payload.get('id') or ''
            # 幂等去重：Resume 重放 / 服务端重发时同一事件只进一次 dispatch
            eid = wrapped['id'] or d.get('event_id') or ''
            if eid:
                if eid in self._seen_events:
                    logger.debug('qq_official 重复事件已去重: %s', eid)
                    continue
                self._seen_events[eid] = 1
                if len(self._seen_events) > 1024:
                    self._seen_events.popitem(last=False)
            # 被动回复缓存（kind 决定各自的被动窗口时长）
            is_group = bool(d.get('group_openid') or d.get('channel_id'))
            key = d.get('group_openid') or d.get('channel_id') \
                or (d.get('author') or {}).get('user_openid') or ''
            if key and d.get('id'):
                self._pending_reply[key] = (d['id'], time.time(),
                                            'group' if is_group else 'private')
                self._last_msg_id = d['id']
            event = normalize_event(wrapped, self.bot_name)
            if event is None:
                continue
            try:
                await self.framework.dispatch_event(event)
            except Exception as e:
                logger.error('qq_official 事件分发失败: %s', e)

    # ── 互动事件与生命周期事件 ─────────────────────────────────

    async def _on_interaction(self, d: dict):
        """按钮回调：去重 → 记录被动回复 id → ACK → 分发 notice 事件。"""
        iid = str(d.get('id') or d.get('event_id') or '')
        if iid:
            if iid in self._seen_events:
                logger.debug('qq_official 重复互动事件已去重: %s', iid)
                return
            self._seen_events[iid] = 1
            if len(self._seen_events) > 1024:
                self._seen_events.popitem(last=False)
            # 互动回调的 event_id 就是被动回复入口，窗口与群一致（5 分钟）
            key = d.get('group_openid') or d.get('user_openid') or ''
            if key:
                self._pending_reply[key] = (
                    iid, time.time(),
                    'group' if d.get('group_openid') else 'private')

        # 只有按钮(11)/菜单(12)回调需要 ACK，其它互动类型回了会报"已回应"
        if self.auto_ack_interaction and int(d.get('type') or 0) in _ACK_INTERACTION_TYPES:
            await self.ack_interaction(iid)

        event = normalize_interaction({'d': d}, self.bot_name)
        if event is None:
            return
        try:
            await self.framework.dispatch_event(event)
        except Exception as e:
            logger.error('qq_official 互动事件分发失败: %s', e)

    async def _on_lifecycle(self, event_t: str, d: dict):
        """机器人进/出群、好友增删、成员增减等 → notice 事件。"""
        if not self.enable_notice_events:
            return
        eid = str(d.get('id') or d.get('event_id') or '')
        if eid:
            if eid in self._seen_events:
                return
            self._seen_events[eid] = 1
            if len(self._seen_events) > 1024:
                self._seen_events.popitem(last=False)
        event = normalize_notice({'d': d}, self.bot_name,
                                 _LIFECYCLE_EVENTS[event_t], event_t)
        if event is None:
            return
        logger.info('qq_official 生命周期事件: %s (%s)',
                    event_t, event.get('group_id') or event.get('user_id'))
        try:
            await self.framework.dispatch_event(event)
        except Exception as e:
            logger.error('qq_official 生命周期事件分发失败: %s', e)

    async def ack_interaction(self, interaction_id: str, code: int = 0) -> dict:
        """确认互动回调（不 ACK 官方会判定回调失败并重试）。"""
        iid = str(interaction_id or '')
        if not iid:
            return self._wrap({'error': '缺少 interaction_id'}, ok=False)
        try:
            await asyncio.to_thread(
                self._api_request, 'PUT', f'/interactions/{iid}',
                json_body={'code': int(code)})
            return self._wrap({'id': iid}, ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning('qq_official 互动回调 ACK 失败: %s', e)
            return self._wrap({'error': str(e)}, ok=False)

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

    _REPLY_WINDOW = {'group': 300, 'private': 3600}   # 群 5 分钟 / 单聊 60 分钟（按各自窗口清理）

    def _reply_msg_id(self, group_id=None, user_id=None) -> str:
        now = time.time()
        # 按会话类型各自的被动窗口清理（原实现统一 5 分钟，单聊窗口被无声缩短）
        for key, (_, ts, kind) in list(self._pending_reply.items()):
            if now - ts > self._REPLY_WINDOW.get(kind, 300):
                self._pending_reply.pop(key, None)
        key = group_id or user_id or ''
        hit = self._pending_reply.get(key)
        if hit:
            return hit[0]
        return self._last_msg_id if not group_id and not user_id else ''

    def _api_request(self, method: str, path: str, *,
                     json_body: dict = None, files=None, data=None,
                     timeout: int = 20) -> dict:
        """同步 HTTP（在 to_thread 里调用）。

        非 2xx 抛 RuntimeError——调用方据此区分成败。旧实现不查状态码、
        错误体被当正常响应返回，再叠一层 'ret' 子串判定，可把失败报成成功。

        域名兼容：默认走正式环境 api.sgroup.qq.com；迁移窗口期内若新域名
        网络异常 / 404 / 5xx，自动用旧域名 api.bot.qq.com 重试一次
        （仅当 api_base 与旧域名不同；4xx 业务错误不换域名直接抛出）。
        """
        token = self._refresh_token_if_stale()
        headers = {'Authorization': f'QQBot {token}'}
        base = self.api_base
        fb = getattr(self, 'api_base_fallback', '')
        hosts = [base, fb] if fb else [base]
        last_exc = None
        last_resp = None
        for host in hosts:
            url = f'{host}{path}'
            try:
                if files is not None:
                    resp = self._http.request(
                        method, url, headers=headers, files=files, data=data,
                        timeout=timeout)
                else:
                    if json_body is not None:
                        headers['Content-Type'] = 'application/json; charset=utf-8'
                    resp = self._http.request(
                        method, url, headers=headers, json=json_body, data=data,
                        timeout=timeout)
            except Exception as e:
                last_exc = e
                continue
            last_resp = resp
            if resp.status_code < 400:
                try:
                    return resp.json() if resp.content else {}
                except Exception:
                    return {}
            # 4xx 业务错误不换域名重试，直接抛
            if resp.status_code < 500 and resp.status_code != 404:
                raise RuntimeError(
                    f'{method} {path} -> HTTP {resp.status_code}: '
                    f'{(resp.text or "")[:300]}')
            # 404/5xx：换域名再试一次
            continue
        if last_resp is not None:
            raise RuntimeError(
                f'{method} {path} -> HTTP {last_resp.status_code}: '
                f'{(last_resp.text or "")[:300]}')
        raise RuntimeError(f'{method} {path} 请求失败: {last_exc}')

    async def _upload_media(self, openid: str, seg_type: str,
                            media_data: dict, is_group: bool = True) -> dict:
        """上传任意富媒体（图 / 语音 / 视频 / 文件）并返回 media dict。

        seg_type 决定 file_type：1 图 / 2 视频 / 3 语音 / 4 文件。
        数据来源三选一：base64://、http(s) 直传 url、本地路径（含 file://）。
        失败一律抛异常，由发送侧感知，不静默降级成"发送成功"。
        """
        file_type = _FILE_TYPE.get(seg_type, 4)
        scope = 'groups' if is_group else 'users'
        path = f'/v2/{scope}/{openid}/files'
        label = {'image': '图片', 'video': '视频', 'record': '语音',
                 'voice': '语音', 'file': '文件'}.get(seg_type, '媒体')
        file = str(media_data.get('file') or media_data.get('url') or '')
        b64 = media_data.get('base64') or _extract_b64(file)
        try:
            if b64:
                raw = _b64_to_bytes(b64)
                if not raw:
                    raise ValueError(f'{label} base64 解码失败: {b64[:64]}...')
                mime = _mime_for(seg_type, raw)
                files = {'file': (_media_name(seg_type, mime), raw, mime)}
                form = {'file_type': str(file_type), 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
            elif file.startswith(('http://', 'https://')):
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    json_body={'file_type': file_type, 'url': file,
                               'srv_send_msg': False})
            else:
                local = file[7:] if file.startswith('file://') else file
                raw = await asyncio.to_thread(_read_file_bytes, local)
                if not raw:
                    raise FileNotFoundError(f'读取本地{label}失败: {local}')
                mime = _mime_for(seg_type, raw, local)
                name = _media_name(seg_type, mime,
                                   local.rsplit('/', 1)[-1].rsplit('\\', 1)[-1])
                files = {'file': (name, raw, mime)}
                form = {'file_type': str(file_type), 'srv_send_msg': 'false'}
                data = await asyncio.to_thread(
                    self._api_request, 'POST', path,
                    files=files, data=form)
        except Exception as e:
            logger.warning('qq_official %s上传失败: %s', label, e)
            raise
        if not isinstance(data, dict) or not data.get('file_info'):
            raise RuntimeError(f'qq_official 上传响应无 file_info: {data}')
        return {'file_info': data['file_info']}

    async def _upload_group_image(self, group_openid: str,
                                  image_data: dict) -> dict:
        """上传图片到群，返回 media dict（_upload_media 的图片特化入口）。"""
        return await self._upload_media(group_openid, 'image', image_data,
                                        is_group=True)

    async def _build_body(self, openid: str, message, reply_msg_id: str,
                          is_group: bool):
        """出站消息 → OpenAPI 请求体。

        官方一条消息只能有一种主体：媒体(msg_type 7) / markdown(2) / 文本(0)，
        keyboard 只能挂在 markdown 上——所以这里做唯一的"选型"决策，
        其余地方不再各自判断。

        :return: (body, err)；err 非空即失败原因（已是可直接回给调用方的中文）
        """
        text, media, markdown, buttons = _split_outgoing(message)
        body: dict = {}
        if reply_msg_id:
            body['msg_id'] = reply_msg_id
            body['msg_seq'] = _next_msg_seq()

        if media is not None:
            seg_type, seg_data = media
            try:
                uploaded = await self._upload_media(
                    openid, seg_type, seg_data, is_group)
            except Exception as e:  # noqa: BLE001
                return None, f'{seg_type} 上传失败: {e}'
            body['msg_type'] = _MSG_TYPE_MEDIA
            body['media'] = uploaded
            if text:
                body['content'] = text
            return body, ''

        keyboard = build_keyboard(buttons)
        if markdown is not None:
            md: dict = {}
            if markdown:
                md['content'] = markdown
            # 模板 markdown：段里带 template_id / params 时优先用模板形态
            tpl_id = ''
            tpl_params = None
            if isinstance(message, list):
                for seg in message:
                    if isinstance(seg, dict) and seg.get('type') == 'markdown':
                        data = seg.get('data') or {}
                        tpl_id = data.get('template_id') or \
                            data.get('custom_template_id') or ''
                        tpl_params = data.get('params')
            if tpl_id:
                md['custom_template_id'] = tpl_id
                if tpl_params:
                    md['params'] = tpl_params
            if not md:
                md['content'] = text
            body['msg_type'] = _MSG_TYPE_MARKDOWN
            body['markdown'] = md
            if keyboard:
                body['keyboard'] = keyboard
            return body, ''

        if keyboard:
            # 按钮必须配 markdown：没有就退化成"文本包一层 markdown"
            body['msg_type'] = _MSG_TYPE_MARKDOWN
            body['markdown'] = {'content': text}
            body['keyboard'] = keyboard
            return body, ''

        body['msg_type'] = _MSG_TYPE_TEXT
        body['content'] = text
        return body, ''

    async def _send_group(self, group_openid: str, message,
                          reply_msg_id: str = '') -> dict:
        body, err = await self._build_body(
            group_openid, message, reply_msg_id, is_group=True)
        if err:
            logger.warning('qq_official 群消息构造失败: %s', err)
            return self._wrap({'error': err}, ok=False)
        path = f'/v2/groups/{group_openid}/messages'
        try:
            data = await asyncio.to_thread(
                self._api_request, 'POST', path, json_body=body)
        except Exception as e:
            logger.warning('qq_official 群消息发送失败: %s', e)
            return self._wrap({'error': str(e)}, ok=False)
        # _api_request 只在 2xx 时返回——到此处即为成功，不再对响应体形状做猜测
        return self._wrap(data, ok=True)

    async def _send_c2c(self, user_openid: str, message,
                        reply_msg_id: str = '') -> dict:
        body, err = await self._build_body(
            user_openid, message, reply_msg_id, is_group=False)
        if err:
            logger.warning('qq_official 单聊消息构造失败: %s', err)
            return self._wrap({'error': err}, ok=False)
        path = f'/v2/users/{user_openid}/messages'
        try:
            data = await asyncio.to_thread(
                self._api_request, 'POST', path, json_body=body)
        except Exception as e:
            logger.warning('qq_official 单聊消息发送失败: %s', e)
            return self._wrap({'error': str(e)}, ok=False)
        return self._wrap(data, ok=True)

    async def _upload_c2c_image(self, user_openid: str,
                                image_data: dict) -> dict:
        """上传单聊图片，返回 media dict（_upload_media 的图片特化入口）。"""
        return await self._upload_media(user_openid, 'image', image_data,
                                        is_group=False)

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
            # 快捷参数：markdown / markdown_template_id / buttons 直接拼进消息段，
            # 插件不用自己拼 dict，走 OneBot 风格的段也能得到一样的效果
            if params.get('markdown') is not None:
                md = {'content': str(params['markdown'])}
                if params.get('markdown_template_id'):
                    md['template_id'] = str(params['markdown_template_id'])
                if params.get('markdown_params'):
                    md['params'] = params['markdown_params']
                seg = [{'type': 'markdown', 'data': md}]
                message = seg + (message if isinstance(message, list)
                                 else [{'type': 'text', 'data': {'text': str(message)}}])
            if params.get('buttons'):
                kb = [{'type': 'keyboard',
                       'data': {'buttons': params['buttons']}}]
                message = (message if isinstance(message, list)
                           else [{'type': 'text', 'data': {'text': str(message)}}]) + kb
            group_id = params.get('group_id')
            user_id = params.get('user_id')
            if params.get('active'):
                # 显式 active=True：不挂 msg_id，按主动消息发送（默认仍自动补被动 id）
                reply = ''
            else:
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

        if action in ('delete_msg', 'recall_msg'):
            # 撤回：群走 /v2/groups/{gid}/messages/{mid}，单聊走 /v2/users/{uid}/…
            group_id = params.get('group_id')
            user_id = params.get('user_id')
            msg_id = params.get('message_id') or params.get('msg_id') or ''
            if not msg_id:
                return {'status': 'failed', 'retcode': -2,
                        'msg': 'delete_msg 需要 message_id'}
            if group_id:
                path = f'/v2/groups/{group_id}/messages/{msg_id}'
            elif user_id:
                path = f'/v2/users/{user_id}/messages/{msg_id}'
            else:
                return {'status': 'failed', 'retcode': -2,
                        'msg': 'delete_msg 需要 group_id 或 user_id'}
            try:
                await asyncio.to_thread(
                    self._api_request, 'DELETE', path,
                    json_body={'hidetip': bool(params.get('hidetip', False))})
                return self._wrap({'message_id': msg_id}, ok=True)
            except Exception as e:  # noqa: BLE001
                logger.warning('qq_official 撤回失败: %s', e)
                return self._wrap({'error': str(e)}, ok=False)

        if action == 'ack_interaction':
            return await self.ack_interaction(
                params.get('interaction_id') or params.get('event_id') or '',
                params.get('code', 0))

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
    # 互动回调默认自动 ACK、生命周期事件默认转 notice；可按需关掉
    _adapter_instance.auto_ack_interaction = \
        cfg.get('auto_ack_interaction', True) is not False
    _adapter_instance.enable_notice_events = \
        cfg.get('enable_notice_events', True) is not False
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    if app_id and app_secret:
        _adapter_instance.start()
        ctx.log(f'QQ 官方接入端已启动 (app_id={app_id})')
    else:
        ctx.log('QQ 官方接入端已注册，待配置 app_id / app_secret 后重启生效')


def unregister():
    global _adapter_instance
    inst, _adapter_instance = _adapter_instance, None
    if inst is None:
        return
    loop = getattr(inst.framework, 'loop', None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.create_task, inst.stop())
    else:
        # loop 不可用（卸载时序早于循环/已停）：同步尽力收尾，防连接与线程残留
        inst._closing = True
        inst._connected = False
        try:
            inst._http.close()
        except Exception:
            pass
