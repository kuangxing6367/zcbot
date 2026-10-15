# -*- coding: utf-8 -*-
"""IIPC 插件侧 Event 对象：对齐 framework/messaging/event.py 的公开面。

原 zcbot 插件拿到的 event 是框架 Event 实例（属性 + 惰性权限 + 富媒体快捷属性），
IIPC 为了让真插件零改动跑起来，在这里 1:1 复刻同一张面：

- 属性：post_type / message_type / sub_type / self_id / message / raw_message /
  segments / user_id / sender / group_id / message_id / font / adapter / bot_name
- 身份：sender_role / role / is_admin / is_superuser / is_group_owner /
  is_group_admin / is_blacklisted
- 富媒体：has_image / has_reply / has_voice / has_video / has_file / has_sticker /
  has_face / has_share / share / has_at / has_at_bot / at_list / reply_id /
  images / voices / videos / files / stickers / first_image / ...
- 传播控制：stop_event / is_stopped / continue_route / is_continue_route
- dict 式访问：ev.get('user_id') / ev['group_id']（原框架事件热路径也常被当 dict 用）

权限查询惰性化：role 首次访问时经 IIPC 向内核查一次（带本进程 TTL 缓存），
与原实现"首次查询后缓存"的语义一致。
"""
import re
import time


def _extract_text(message):
    """从 OneBot 11 消息中提取纯文本（对齐框架 _extract_text）。"""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for seg in message:
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {})
                if seg_type == 'text':
                    parts.append(seg_data.get('text', ''))
                elif seg_type == 'at':
                    qq = seg_data.get('qq', '')
                    parts.append('[@%s]' % qq)
                elif seg_type == 'reply':
                    pass
                elif seg_type == 'share':
                    url = seg_data.get('url', '')
                    if url:
                        parts.append(url)
                elif seg_type == 'json':
                    raw = seg_data.get('data', '') or ''
                    for m in re.finditer(r'https?://[^\s"\'<]+', raw):
                        parts.append(m.group(0))
        return ''.join(parts)
    return str(message)


def _normalize_message(msg):
    """消息段归一（对齐框架 normalize_message 的最小可用面）。

    框架把各协议差异（voice/record 等）统一成规范名；IIPC 侧事件来自 OneBot，
    保持原样即可，仅做 dict 化兜底。
    """
    if isinstance(msg, list):
        out = []
        for seg in msg:
            if isinstance(seg, dict):
                out.append({'type': seg.get('type', ''),
                            'data': seg.get('data', {}) or {}})
            else:
                out.append({'type': 'text', 'data': {'text': str(seg)}})
        return out
    return []


class Event:
    """消息事件对象（IIPC 版，1:1 对齐框架 Event 公开面）"""

    def __init__(self, raw, bot_name='default', perm_bridge=None, has_perm_fn=None):
        self._raw = raw or {}
        self.bot_name = bot_name
        self._perm_bridge = perm_bridge  # callable(user_id, group_id) -> dict，惰性查角色
        self._has_perm_fn = has_perm_fn  # callable(user_id, node, group_id) -> bool

        self.post_type = self._raw.get('post_type', '')
        self.message_type = self._raw.get('message_type', '')
        self.sub_type = self._raw.get('sub_type', '')
        self.self_id = self._raw.get('self_id', 0)

        _raw_msg = self._raw.get('message', '')
        if isinstance(self._raw.get('_msg_text'), str):
            _msg_text = self._raw['_msg_text']
        else:
            _msg_text = _extract_text(_raw_msg)
        self.message = _msg_text
        # @机器人 前缀剥离（与框架一致）：命令前缀匹配不被 [@self_id] 污染
        if isinstance(_raw_msg, list) and _raw_msg and \
                _raw_msg[0].get('type') == 'at' and \
                str(_raw_msg[0].get('data', {}).get('qq', '')) == str(self.self_id):
            _bot_at_prefix = '[@%s]' % self.self_id
            if self.message.startswith(_bot_at_prefix):
                self.message = self.message[len(_bot_at_prefix):].lstrip()
        self.message_id = self._raw.get('message_id', 0)
        _orig_raw = self._raw.get('raw_message')
        self.raw_message = _msg_text if _orig_raw is None else _extract_text(_orig_raw)

        seg_src = self._raw.get('segments') if isinstance(self._raw.get('segments'), list) \
            else _raw_msg
        self.segments = _normalize_message(seg_src)

        self.adapter = self._raw.get('adapter', '') or ''
        self.user_id = self._raw.get('user_id', 0)
        self.sender = self._raw.get('sender', {}) or {}
        self.group_id = self._raw.get('group_id', 0)
        self.font = self._raw.get('font', 0)

        self._role_cache = None
        self._stopped = False
        self._continue_route = False
        self._ctx = None  # SDK 装配时反挂 PluginContext（便捷访问用）

    # ── dict 式访问（原框架事件在热路径常被当 dict 用）──
    def get(self, key, default=None):
        # 先查原始事件字段，再回落到属性（兼容 ev.get('user_id') 与 ev.get('message') 两种写法）
        if key in self._raw:
            return self._raw[key]
        return getattr(self, key, default)

    def __getitem__(self, key):
        if key in self._raw:
            return self._raw[key]
        return getattr(self, key)

    def __contains__(self, key):
        return key in self._raw

    def keys(self):
        return self._raw.keys()

    # ── 权限属性（惰性查询 + 本进程缓存，对齐框架语义）──
    @property
    def sender_role(self):
        return self.sender.get('role', 'member')

    def _role_row(self):
        if self._role_cache is not None:
            return self._role_cache
        row = None
        if self._perm_bridge is not None:
            try:
                row = self._perm_bridge(self.user_id, self.group_id)
            except Exception:
                row = None
        self._role_cache = row or {}
        return self._role_cache

    @property
    def role(self):
        """完整身份：super > owner > admin > member > blacklist"""
        cached = self._role_cache
        if isinstance(cached, dict) and cached.get('role'):
            return cached['role']
        row = self._role_row()
        if row.get('role'):
            return row['role']
        return self.sender_role

    @property
    def is_admin(self):
        return self.role in ('admin', 'owner', 'super')

    @property
    def is_superuser(self):
        return self.role == 'super'

    @property
    def is_group_owner(self):
        return self.role == 'owner' or self.sender_role == 'owner'

    @property
    def is_group_admin(self):
        return self.sender_role in ('admin', 'owner')

    @property
    def is_blacklisted(self):
        return self.role == 'blacklist'

    # ── 权限节点（对齐框架 ev.has_perm / ev.check_perm）──
    # 原框架：perm.resolve(db, self.user_id, context_from_event(self), self.role)
    # —— 上下文是 dict（bot/group/msgtype），且**注入事件自身的 role**。
    # 此前误把 group_id 当 context 位置参数传（int 上 .items() 抛错被 except 吞掉，
    # 表现为 has_perm 恒 False），且漏了 role，导致 ev.has_perm('zcbot.role.super')
    # 对超管也返回 False。
    def _perm_context(self):
        c = {}
        if getattr(self, 'bot_name', None):
            c['bot'] = str(self.bot_name)
        if getattr(self, 'group_id', 0):
            c['group'] = str(self.group_id)
        if getattr(self, 'message_type', ''):
            c['msgtype'] = str(self.message_type)
        return c

    def has_perm(self, node):
        if self._has_perm_fn is None:
            return False
        try:
            return bool(self._has_perm_fn(self.user_id, node,
                                          context=self._perm_context(),
                                          role=self.role))
        except Exception:
            return False

    def check_perm(self, node):
        # 内核侧为二值判定；显式否决与未定义都返回 False（三态语义的保守近似）
        return self.has_perm(node)

    # ── 文本 / 媒体 ──
    @property
    def text(self):
        return self.message

    @property
    def media(self):
        return [s for s in self.segments
                if s.get('type') in ('image', 'record', 'voice', 'video', 'file')]

    @property
    def is_media_only(self):
        return bool(self.segments) and not any(
            s.get('type') == 'text' and (s.get('data', {}).get('text') or '').strip()
            for s in self.segments)

    def _has(self, t):
        return any(s.get('type') == t for s in self.segments)

    @property
    def has_image(self):
        return self._has('image')

    @property
    def has_reply(self):
        return self._has('reply')

    @property
    def has_voice(self):
        return self._has('record') or self._has('voice')

    @property
    def has_video(self):
        return self._has('video')

    @property
    def has_file(self):
        return self._has('file')

    @property
    def has_sticker(self):
        return self._has('face') or self._has('sticker')

    @property
    def has_face(self):
        return self._has('face')

    @property
    def has_share(self):
        return self._has('share')

    @property
    def share(self):
        for s in self.segments:
            if s.get('type') == 'share':
                return s.get('data', {}) or {}
        return None

    @property
    def has_at(self):
        return any(s.get('type') == 'at' for s in self.segments)

    @property
    def has_at_bot(self):
        return any(s.get('type') == 'at' and
                   str(s.get('data', {}).get('qq', '')) == str(self.self_id)
                   for s in self.segments)

    @property
    def at_list(self):
        return [s.get('data', {}).get('qq', '') for s in self.segments
                if s.get('type') == 'at']

    @property
    def reply_id(self):
        for s in self.segments:
            if s.get('type') == 'reply':
                return s.get('data', {}).get('id') or s.get('data', {}).get('message_id')
        return None

    def _collect(self, t):
        return [s.get('data', {}) or {} for s in self.segments if s.get('type') == t]

    @property
    def images(self):
        return self._collect('image')

    @property
    def voices(self):
        return self._collect('record') + self._collect('voice')

    @property
    def videos(self):
        return self._collect('video')

    @property
    def files(self):
        return self._collect('file')

    @property
    def stickers(self):
        return self._collect('face')

    @property
    def first_image(self):
        # 对齐 framework/messaging/event.py：无内容返回空 dict（非 None），
        # 官方插件常见写法 first_image.get('data') 在 None 上会 AttributeError。
        return self.images[0] if self.images else {}

    @property
    def first_voice(self):
        return self.voices[0] if self.voices else {}

    @property
    def first_video(self):
        return self.videos[0] if self.videos else {}

    @property
    def first_file(self):
        return self.files[0] if self.files else {}

    # ── 消息形态 / 发送者（对齐 framework/messaging/event.py:530-551）──
    # 官方示例（docs/writing-plugins.md 签到插件）直接用 event.is_group；
    # 官方插件 llm_blacklist 用 getattr(event, "is_group", False) 兜底，
    # 缺该属性会让前者直接 AttributeError —— 故必须 1:1 提供。
    @property
    def is_group(self) -> bool:
        """是否群消息"""
        return self.message_type == 'group'

    @property
    def is_private(self) -> bool:
        """是否私聊消息"""
        return self.message_type == 'private'

    @property
    def sender_nickname(self) -> str:
        """发送者昵称（群名片为空时的回落）"""
        return self.sender.get('nickname', '')

    @property
    def sender_card(self) -> str:
        """发送者群名片（私聊为空串）"""
        return self.sender.get('card', '')

    @property
    def at_all(self) -> bool:
        """消息是否 @全体成员"""
        return any(
            s.get('type') == 'at' and s.get('data', {}).get('qq') == 'all'
            for s in self.segments
        )

    # ── 传播控制 ──
    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    def continue_route(self):
        self._continue_route = True

    def is_continue_route(self):
        return self._continue_route


_ROLE_CACHE_TTL = 60.0


class _RoleCache:
    """插件进程内的角色查询缓存（user, group) -> (row, ts)，60s TTL。

    对齐框架 _ROLE_CACHE_TTL；上限 5000 条惰性清理，防长期运行内存增长。
    """

    def __init__(self, ttl=_ROLE_CACHE_TTL, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._cache = {}
        self._checks = 0

    def get_or_fetch(self, key, fetcher):
        now = time.time()
        hit = self._cache.get(key)
        if hit is not None and now - hit[1] <= self.ttl:
            return hit[0]
        self._checks += 1
        if self._checks % 256 == 0 and len(self._cache) > self.max_entries:
            self._cache = {k: v for k, v in self._cache.items()
                           if now - v[1] <= self.ttl}
        row = fetcher()
        self._cache[key] = (row or {}, now)
        return row or {}
