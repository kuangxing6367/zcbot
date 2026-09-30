# -*- coding: utf-8 -*-
"""
多模态输入（图片）

多模态不是一个独立的“功能层”，它就是两件事拼起来：

    1. 从原始消息里把图片抠出来
    2. 把图片按模型认识的格式塞进 user 消息里

原始消息之所以必须经过手：命令层和事件层拿到的是「提取后的纯文本」，图片早就被
剥掉了。只有在 raw handler 里还能看到完整的消息段数组，所以这里必须趁早截一次，
把图片暂存下来，等真正发请求时再拼进内容里。

模型收不收得了是另一回事：OpenAI 视觉输入的格式是 content 不再是字符串，而是一串
内容块——看到不认识内容块的模型会报错，所以「这个 provider 支不支持 vision」必须由
provider 自己声明，不支持就干脆不带图。
"""
import base64
import logging
import mimetypes
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('zcbot.llm_core')

__all__ = ['MediaCache', 'extract_images', 'build_user_content', 'image_part']

# 常见的图片段类型（不同接入端写法略有差异，统一按包含关系判定）
_IMAGE_TYPES = ('image', 'picture', 'photo')
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _guess_mime(path: str, default: str = 'image/png') -> str:
    mime, _ = mimetypes.guess_type(path or '')
    return mime or default


def _read_as_data_uri(path: str) -> Optional[str]:
    """本地文件 → data URI。读不出来返回 None（不让它污染整条消息）。"""
    try:
        if not os.path.isfile(path):
            return None
        if os.path.getsize(path) > _MAX_IMAGE_BYTES:
            logger.warning(f"[llm_core] 图片超过 {_MAX_IMAGE_BYTES // 1024 // 1024}MB，已跳过: {path}")
            return None
        with open(path, 'rb') as f:
            raw = f.read()
        return f"data:{_guess_mime(path)};base64," + base64.b64encode(raw).decode('ascii')
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_core] 读取图片失败: {e}")
        return None


def _normalize_file(value: str) -> str:
    """去掉各种前缀，还原成本地路径"""
    value = (value or '').strip()
    for prefix in ('file:///', 'file://'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value


def _pick_source(data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从图片段里挑出可用来源，返回 {'kind': 'url'|'base64'|'file', 'value': ...}"""
    if not isinstance(data, dict):
        return None
    file_value = str(data.get('file') or '')
    url = str(data.get('url') or '')

    if file_value.startswith('base64://'):
        return {'kind': 'base64', 'value': file_value[len('base64://'):]}
    if url.startswith(('http://', 'https://')):
        return {'kind': 'url', 'value': url}
    if file_value.startswith(('http://', 'https://')):
        return {'kind': 'url', 'value': file_value}
    if file_value:
        return {'kind': 'file', 'value': _normalize_file(file_value)}
    if url:
        return {'kind': 'file', 'value': _normalize_file(url)}
    return None


def extract_images(raw_event: dict) -> List[Dict[str, str]]:
    """从原始消息事件里抠出图片来源列表。

    只看消息段数组（不同接入端字段名略有出入，这里按常见约定取值）。
    """
    found: List[Dict[str, str]] = []
    if not isinstance(raw_event, dict):
        return found
    segments = raw_event.get('message')
    if not isinstance(segments, (list, tuple)):
        return found
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get('type') or '').lower()
        if stype not in _IMAGE_TYPES:
            continue
        src = _pick_source(seg.get('data') or {})
        if src:
            found.append(src)
    return found


def image_part(source: Dict[str, str]) -> Optional[dict]:
    """把一种来源转成 OpenAI 视觉输入的内容块"""
    if not source:
        return None
    kind, value = source.get('kind'), source.get('value') or ''
    if kind == 'url':
        return {'type': 'image_url', 'image_url': {'url': value}}
    if kind == 'base64':
        mime = 'image/png'
        return {'type': 'image_url',
                'image_url': {'url': f"data:{mime};base64,{value}"}}
    if kind == 'file':
        uri = _read_as_data_uri(value)
        if uri:
            return {'type': 'image_url', 'image_url': {'url': uri}}
    return None


def build_user_content(text: str, images: List[Dict[str, str]] = None,
                       vision_supported: bool = False) -> Any:
    """构造 user 消息内容。

    没图、或 provider 不支持视觉时返回纯字符串（兼容最老的模型）；
    有图且支持视觉时返回内容块数组。
    """
    text = text or ''
    if not images or not vision_supported:
        return text
    parts: List[dict] = []
    if text:
        parts.append({'type': 'text', 'text': text})
    for src in images:
        part = image_part(src)
        if part:
            parts.append(part)
    if not parts:
        return text
    if len(parts) == 1 and parts[0].get('type') == 'text':
        return text
    return parts


class MediaCache:
    """图片暂存区。

    raw handler 里拿到图，但命令真正处理时才会用到，中间隔着一次命令匹配
    和一次网络请求，所以需要一个带 TTL 的小缓存把它们对上。

    键用 (来源, 群, 用户) —— 一条消息的生命周期里这三元组足够定位到人。
    """

    def __init__(self, ttl: int = 300, capacity: int = 200):
        self._ttl = ttl
        self._capacity = capacity
        self._data: 'OrderedDict[Tuple[str, str, str], tuple]' = _new_ordered()

    def put(self, source: str, group_id, user_id, images: List[Dict[str, str]]) -> None:
        if not images:
            return
        key = _key(source, group_id, user_id)
        self._gc()
        self._data[key] = (time.time(), list(images))
        self._data.move_to_end(key)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def take(self, source: str, group_id, user_id) -> List[Dict[str, str]]:
        """取出并清空该会话暂存的图片"""
        key = _key(source, group_id, user_id)
        item = self._data.pop(key, None)
        if not item:
            return []
        ts, images = item
        if time.time() - ts > self._ttl:
            return []
        return images

    def clear(self) -> None:
        self._data.clear()

    def _gc(self) -> None:
        now = time.time()
        for key in [k for k, (ts, _v) in self._data.items() if now - ts > self._ttl]:
            self._data.pop(key, None)


def _new_ordered():
    from collections import OrderedDict
    return OrderedDict()


def _key(source: str, group_id, user_id) -> Tuple[str, str, str]:
    return (str(source or '-'), str(group_id or 'private'), str(user_id or 0))
