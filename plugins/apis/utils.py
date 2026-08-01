"""
工具函数
适配 zgric 框架的 Event 对象
"""
from __future__ import annotations

from framework.event import Event


def get_nickname(event: Event, target_id: str | int) -> str:
    """获取用户昵称"""
    return event.sender_nickname or str(target_id)


def get_reply_text(event: Event) -> str:
    """从回复消息中提取纯文本"""
    if not event.has_reply:
        return ""
    # 从 event.segments 中查找回复消息后跟随的文本
    # 在 zgric 框架中，回复消息的文本是 event.message_str
    # 但回复消息本身的内容需要从 event.segments 中提取
    text_parts = []
    capture = False
    for seg in event.segments:
        if seg.get('type') == 'reply':
            capture = True
            continue
        if capture and seg.get('type') == 'text':
            text_parts.append(seg.get('data', {}).get('text', ''))
    return ''.join(text_parts).strip()