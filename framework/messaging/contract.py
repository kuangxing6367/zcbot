# -*- coding: utf-8 -*-
"""
接入端契约（Adapter Contract）

框架不认识任何具体协议，但认识"接入端必须交出什么"。这一层把约束写成代码：

1. `normalize_event()` —— 事件进入内核前的**补齐器**。
   接入端只交出"它有把握的部分"（谁、在哪个群、说了什么），剩下由框架补成
   标准形状。这样接入端写起来不死板，插件读起来却是一致的。
2. `Capabilities` —— 接入端能力自述。
   插件可以问"当前接入端能不能收语音 / 能不能发按钮"，而不是 try 一把看报错。
3. `validate_adapter()` —— 契约自检。
   加载时跑一遍，缺胳膊少腿直接在日志里点名，不用等线上出怪事。
"""
import logging
from typing import Any, Dict, Iterable, List, Optional

from .segments import (
    canonical_type, normalize_message, segments_to_text, SEG_TEXT,
)

logger = logging.getLogger('zcbot.contract')

# ─────────────────────────────────────────────────────────────
# 事件类型
# ─────────────────────────────────────────────────────────────

EVENT_MESSAGE = 'message'
EVENT_NOTICE = 'notice'
EVENT_REQUEST = 'request'
EVENT_META = 'meta_event'
EVENT_TYPES = (EVENT_MESSAGE, EVENT_NOTICE, EVENT_REQUEST, EVENT_META)

#: post_type（OneBot 系叫法）→ 规范 type
_POST_TYPE_ALIASES = {
    'message': EVENT_MESSAGE,
    'message_sent': EVENT_MESSAGE,
    'notice': EVENT_NOTICE,
    'request': EVENT_REQUEST,
    'meta_event': EVENT_META,
    'meta': EVENT_META,
    'event': EVENT_NOTICE,
}

#: notice_type 归一化：各家叫法不同，插件只认规范名
NOTICE_ALIASES = {
    # 成员增减
    'group_increase': 'group_member_increase',
    'member_join': 'group_member_increase',
    'group_join': 'group_member_increase',
    'group_add_robot': 'group_member_increase',
    'group_msg_receive': 'group_member_increase',
    'group_decrease': 'group_member_decrease',
    'member_leave': 'group_member_decrease',
    'group_leave': 'group_member_decrease',
    'group_del_robot': 'group_member_decrease',
    'kick': 'group_member_decrease',
    # 撤回
    'recall': 'message_recall',
    'group_recall': 'message_recall',
    'group_msg_recall': 'message_recall',
    'friend_recall': 'message_recall',
    'message_delete': 'message_recall',
    # 其它
    'notify': 'poke',
    'group_admin_change': 'group_admin',
    'admin_change': 'group_admin',
    'group_upload': 'group_upload',
    'friend_added': 'friend_add',
    'friend_del': 'friend_delete',
    'friend_delete': 'friend_delete',
    'callback_query': 'callback_query',
    'interaction': 'interaction',
    'button': 'interaction',
    'essence': 'group_essence',
}

#: 规范通知类型（文档与能力自述用）
NOTICE_TYPES = (
    'group_member_increase', 'group_member_decrease', 'group_ban',
    'group_admin', 'group_upload', 'group_essence', 'group_card',
    'message_recall', 'poke', 'friend_add', 'friend_delete',
    'interaction', 'callback_query',
)

#: 请求类型
REQUEST_TYPES = ('friend', 'group')

# ─────────────────────────────────────────────────────────────
# 能力常量
# ─────────────────────────────────────────────────────────────

CAP_TEXT = 'text'
CAP_IMAGE = 'image'
CAP_VOICE = 'voice'
CAP_VIDEO = 'video'
CAP_FILE = 'file'
CAP_STICKER = 'sticker'
CAP_AT = 'at'
CAP_REPLY = 'reply'
CAP_FACE = 'face'
CAP_SHARE = 'share'
CAP_JSON = 'json'
CAP_LOCATION = 'location'
CAP_POKE = 'poke'
CAP_MARKDOWN = 'markdown'
CAP_KEYBOARD = 'keyboard'
CAP_FORWARD = 'forward'
CAP_RECALL = 'recall'          # 撤回
CAP_REACT = 'react'            # 表情回应
CAP_TYPING = 'typing'          # 输入中
CAP_GROUP_ADMIN = 'group_admin'  # 禁言/踢人/改名片等群管动作
CAP_NOTICE = 'notice'          # 能收到 notice 类事件
CAP_REQUEST = 'request'        # 能收到加好友/加群请求

#: 媒体类能力（用于"这条媒体能不能过"的快速判断）
MEDIA_CAPS = (CAP_IMAGE, CAP_VOICE, CAP_VIDEO, CAP_FILE, CAP_STICKER)

IN = 'in'
OUT = 'out'


class Capabilities:
    """
    接入端能力自述。

        def capabilities(self):
            return Capabilities(
                inbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE, CAP_NOTICE],
                outbound=[CAP_TEXT, CAP_IMAGE, CAP_KEYBOARD],
                actions=[CAP_RECALL, CAP_GROUP_ADMIN],
            )

    - inbound：能收到什么
    - outbound：能发出什么
    - actions：额外动作能力（撤回、群管…）
    """

    __slots__ = ('inbound', 'outbound', 'actions')

    def __init__(self, inbound: Iterable[str] = (), outbound: Iterable[str] = (),
                 actions: Iterable[str] = ()):
        self.inbound = _clean(inbound)
        self.outbound = _clean(outbound)
        self.actions = _clean(actions)

    def supports(self, cap: str, direction: str = OUT) -> bool:
        if direction == IN:
            return cap in self.inbound
        if direction == OUT:
            return cap in self.outbound
        return cap in self.actions

    def supports_media(self, seg_type: str, direction: str = OUT) -> bool:
        """按规范段类型问媒体能力（'voice'/'image'…）"""
        return self.supports(canonical_type(seg_type), direction)

    def to_dict(self) -> dict:
        return {'inbound': list(self.inbound),
                'outbound': list(self.outbound),
                'actions': list(self.actions)}

    def __repr__(self):
        return (f"Capabilities(in={sorted(self.inbound)}, "
                f"out={sorted(self.outbound)}, acts={sorted(self.actions)})")


def _clean(items) -> tuple:
    if not items:
        return ()
    if isinstance(items, str):
        return (items,)
    return tuple(dict.fromkeys(str(i) for i in items if i))


def as_capabilities(raw: Any) -> Capabilities:
    """把接入端返回的 dict / list / Capabilities 统一成 Capabilities"""
    if isinstance(raw, Capabilities):
        return raw
    if isinstance(raw, dict):
        return Capabilities(raw.get('inbound') or raw.get('in') or (),
                            raw.get('outbound') or raw.get('out') or (),
                            raw.get('actions') or ())
    if isinstance(raw, (list, tuple)):
        # 只给一个列表时，视为"收发同能力"
        return Capabilities(inbound=raw, outbound=raw)
    return Capabilities()


# ─────────────────────────────────────────────────────────────
# 事件补齐
# ─────────────────────────────────────────────────────────────

def normalize_event(raw: dict, adapter: Any = None,
                    adapter_id: str = '') -> dict:
    """
    事件补齐：接入端只给有把握的字段，框架补齐成标准形状。

    约定结果（message 类）：
        type / message_type / sub_type / user_id / group_id / message_id /
        bot_name / self_id / sender{nickname,card,role} / segments /
        message(纯文本) / raw_message / adapter / raw

    已有字段**不会被覆盖**（接入端更了解自己），只补缺失项。
    """
    if not isinstance(raw, dict):
        return {}

    out = dict(raw)

    # ── type ──
    etype = out.get('type') or out.get('post_type') or ''
    etype = _POST_TYPE_ALIASES.get(str(etype).lower(), str(etype).lower())
    if not etype:
        # 没有显式类型但带消息内容 → 按消息处理
        etype = EVENT_MESSAGE if ('message' in out or 'segments' in out) else EVENT_NOTICE
    out['type'] = etype
    out['post_type'] = {EVENT_MESSAGE: 'message', EVENT_NOTICE: 'notice',
                        EVENT_REQUEST: 'request', EVENT_META: 'meta_event'
                        }.get(etype, etype)

    # ── 来源 / 接入端标识 ──
    if adapter_id and not out.get('adapter'):
        out['adapter'] = adapter_id
    if not out.get('bot_name'):
        out['bot_name'] = getattr(adapter, 'bot_name', '') or adapter_id or 'default'

    # ── 身份 ──
    if out.get('user_id') is None:
        sender = out.get('sender') if isinstance(out.get('sender'), dict) else {}
        out['user_id'] = sender.get('user_id') or sender.get('id') or 0
    if isinstance(out.get('sender'), dict):
        sender = dict(out['sender'])
        sender.setdefault('user_id', out.get('user_id'))
        sender.setdefault('nickname', sender.get('card') or '')
        sender.setdefault('card', '')
        sender.setdefault('role', 'member')
        out['sender'] = sender
    elif out.get('sender') is None:
        out['sender'] = {'user_id': out.get('user_id', 0), 'nickname': '',
                         'card': '', 'role': 'member'}

    if out.get('group_id') is None and out.get('chat_id') is not None:
        out['group_id'] = out['chat_id']

    # ── 消息 ──
    if etype == EVENT_MESSAGE:
        _normalize_message_fields(out, adapter)

    # ── 通知 / 请求 ──
    if etype == EVENT_NOTICE:
        # 注意：这里**不改写** notice_type。
        # 已有插件订阅的是协议原名（notice.group_increase 等），改写会让它们
        # 集体失效；规范名只作为 `notice_type_canonical` 附加，并在事件总线上
        # 额外广播一次，新插件可以用规范名跨协议订阅。
        ntype = str(out.get('notice_type') or out.get('sub_type') or '').lower()
        out['notice_type'] = ntype or 'unknown'
        out['notice_type_canonical'] = NOTICE_ALIASES.get(ntype, ntype or 'unknown')
        out.setdefault('sub_type', '')
    if etype == EVENT_REQUEST:
        rtype = str(out.get('request_type') or '').lower()
        if rtype in ('invite', 'join', 'group_invite'):
            rtype = 'group'
        out['request_type'] = rtype or 'friend'

    out.setdefault('sub_type', '')
    out.setdefault('self_id', getattr(adapter, '_self_id', 0) or 0)
    return out


def _normalize_message_fields(event: dict, adapter: Any) -> None:
    """补齐 message 类事件的消息段与纯文本"""
    # 消息段：优先用接入端给的 segments，其次从 message 归一化
    segments = event.get('segments')
    if not isinstance(segments, list) or not segments:
        raw_msg = event.get('message')
        if raw_msg is None and isinstance(event.get('raw_message'), (list, tuple)):
            raw_msg = event['raw_message']
        translate = getattr(adapter, 'normalize_incoming', None)
        if callable(translate) and raw_msg is not None:
            try:
                got = translate(raw_msg)
                if isinstance(got, list):
                    segments = got
            except Exception as e:  # noqa: BLE001
                logger.debug("接入端 normalize_incoming 失败，退回通用归一化: %s", e)
        if not isinstance(segments, list) or not segments:
            segments = normalize_message(raw_msg)
    event['segments'] = segments

    # 纯文本：接入端给了就用它的，否则从规范段提取（命令匹配口径统一）
    if not isinstance(event.get('message'), str):
        event['message'] = segments_to_text(segments)
    if not isinstance(event.get('raw_message'), str):
        event['raw_message'] = event['message']

    # 消息类型：有群即群聊
    mt = str(event.get('message_type') or '').lower()
    if mt not in ('group', 'private'):
        mt = 'group' if event.get('group_id') else 'private'
    event['message_type'] = mt

    if event.get('message_id') is None:
        event['message_id'] = 0

    # 只留一个纯文本段时补 text 键，避免插件判空出错
    if not segments:
        event['segments'] = [{'type': SEG_TEXT, 'data': {'text': ''}}]


# ─────────────────────────────────────────────────────────────
# 契约自检
# ─────────────────────────────────────────────────────────────

def validate_adapter(adapter: Any) -> List[str]:
    """
    接入端契约自检，返回问题列表（空列表=合规）。

    检查：
    - 必须实现的方法是否可调用
    - `normalize_incoming` / `to_native` 返回值形状
    - `capabilities()` 能否解析
    - `get_connection_info()` 形状（若提供）
    """
    issues: List[str] = []
    if adapter is None:
        return ['适配器为空']
    name = type(adapter).__name__

    for meth, arity in (('handle_event', 2), ('call_api', 1),
                        ('get_connected_bots', 0), ('start', 0), ('stop', 0)):
        fn = getattr(adapter, meth, None)
        if not callable(fn):
            issues.append(f'{name} 缺少可调用方法 {meth}()')

    # 入站归一化
    tr = getattr(adapter, 'normalize_incoming', None)
    if callable(tr):
        try:
            got = tr('测试')
            if not isinstance(got, list):
                issues.append(f'{name}.normalize_incoming() 应返回 list，实际 '
                              f'{type(got).__name__}')
        except Exception as e:  # noqa: BLE001
            issues.append(f'{name}.normalize_incoming() 抛异常: {e}')

    # 出站翻译
    tn = getattr(adapter, 'to_native', None)
    if callable(tn):
        try:
            tn([{'type': SEG_TEXT, 'data': {'text': 'x'}}])
        except Exception as e:  # noqa: BLE001
            issues.append(f'{name}.to_native() 抛异常: {e}')

    # 能力自述
    caps = getattr(adapter, 'capabilities', None)
    if callable(caps):
        try:
            parsed = as_capabilities(caps())
            if not parsed.outbound:
                issues.append(f'{name}.capabilities() 未声明任何出站能力')
        except Exception as e:  # noqa: BLE001
            issues.append(f'{name}.capabilities() 抛异常: {e}')

    # 连接自描述
    info = getattr(adapter, 'get_connection_info', None)
    if callable(info):
        try:
            got = info()
            if got is not None and (not isinstance(got, dict) or not got.get('id')):
                issues.append(f'{name}.get_connection_info() 缺少 id 字段')
        except Exception as e:  # noqa: BLE001
            issues.append(f'{name}.get_connection_info() 抛异常: {e}')

    return issues


def contract_report(adapter: Any) -> dict:
    """接入端契约摘要（Web 面板 / 排错用）"""
    caps = Capabilities()
    fn = getattr(adapter, 'capabilities', None)
    if callable(fn):
        try:
            caps = as_capabilities(fn())
        except Exception:  # noqa: BLE001
            pass
    info = {}
    fn = getattr(adapter, 'get_connection_info', None)
    if callable(fn):
        try:
            got = fn()
            info = got if isinstance(got, dict) else {}
        except Exception:  # noqa: BLE001
            info = {}
    return {
        'id': info.get('id') or getattr(adapter, 'adapter_id', '') or type(adapter).__name__,
        'name': info.get('name') or '',
        'capabilities': caps.to_dict(),
        'has_inbound_translator': callable(getattr(adapter, 'normalize_incoming', None)),
        'has_outbound_translator': callable(getattr(adapter, 'to_native', None)),
        'issues': validate_adapter(adapter),
    }


__all__ = [
    'EVENT_MESSAGE', 'EVENT_NOTICE', 'EVENT_REQUEST', 'EVENT_META', 'EVENT_TYPES',
    'NOTICE_TYPES', 'NOTICE_ALIASES', 'REQUEST_TYPES',
    'CAP_TEXT', 'CAP_IMAGE', 'CAP_VOICE', 'CAP_VIDEO', 'CAP_FILE', 'CAP_STICKER',
    'CAP_AT', 'CAP_REPLY', 'CAP_FACE', 'CAP_SHARE', 'CAP_JSON', 'CAP_LOCATION',
    'CAP_POKE', 'CAP_MARKDOWN', 'CAP_KEYBOARD', 'CAP_FORWARD',
    'CAP_RECALL', 'CAP_REACT', 'CAP_TYPING', 'CAP_GROUP_ADMIN', 'CAP_NOTICE',
    'CAP_REQUEST', 'MEDIA_CAPS', 'IN', 'OUT',
    'Capabilities', 'as_capabilities', 'normalize_event', 'validate_adapter',
    'contract_report',
]
