# -*- coding: utf-8 -*-
"""
规范消息段（Canonical Message Segment）

为什么要有这一层
----------------
框架要同时挂 OneBot（QQ 非官方）、QQ 官方机器人、Telegram、Discord、Webhook
等接入端。各协议对"一条消息"的表达完全不一样：

- OneBot 11：数组段 `[{'type':'image','data':{'file':...}}]`，语音叫 `record`
- QQ 官方：`msg_type` + `content` + `media`，语音/视频是独立 file_type
- Telegram：`photo[] / voice / video / document / sticker` 挂在 message 对象上
- Discord：`content` 文本 + `attachments[]` 数组

如果放任各接入端自己决定 `event['message']` 长什么样，插件就只能去翻
`event['raw']` 摸底层协议——换个接入端就崩。所以框架在这里定一套**规范消息段**：

1. 入站：接入端把协议原生结构翻译成规范段（或交给框架默认翻译器兜底）
2. 内核：`ev.segments` 恒为规范段，`ev.images / ev.voices / ev.videos` 恒可用
3. 出站：框架把插件给的任意形态（字符串/CQ 码/段数组）规范化，再交给接入端
   的 `to_native()` 翻译回协议原生

规范段形状固定为 `{'type': str, 'data': dict}`。`data` 里能放什么由下面
`MEDIA_DATA_KEYS` 约定，未列出的键允许透传（适配器可塞协议私有字段）。
"""
import re
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────
# 规范段类型
# ─────────────────────────────────────────────────────────────

SEG_TEXT = 'text'
SEG_AT = 'at'
SEG_FACE = 'face'
SEG_IMAGE = 'image'
SEG_VOICE = 'voice'
SEG_VIDEO = 'video'
SEG_FILE = 'file'
SEG_STICKER = 'sticker'
SEG_REPLY = 'reply'
SEG_SHARE = 'share'
SEG_JSON = 'json'
SEG_XML = 'xml'
SEG_LOCATION = 'location'
SEG_POKE = 'poke'
SEG_MUSIC = 'music'
SEG_DICE = 'dice'
SEG_RPS = 'rps'
SEG_FORWARD = 'forward'
SEG_MARKDOWN = 'markdown'
SEG_KEYBOARD = 'keyboard'

#: 承载二进制/文件的内容型段（有"文件引用"语义）
MEDIA_TYPES = frozenset({SEG_IMAGE, SEG_VOICE, SEG_VIDEO, SEG_FILE, SEG_STICKER})
#: 纯文本语义
TEXT_TYPES = frozenset({SEG_TEXT, SEG_MARKDOWN})
#: 交互/结构化语义（不是给人直接读的）
STRUCT_TYPES = frozenset({SEG_REPLY, SEG_JSON, SEG_XML, SEG_KEYBOARD, SEG_FORWARD})

ALL_TYPES = frozenset({
    SEG_TEXT, SEG_AT, SEG_FACE, SEG_IMAGE, SEG_VOICE, SEG_VIDEO, SEG_FILE,
    SEG_STICKER, SEG_REPLY, SEG_SHARE, SEG_JSON, SEG_XML, SEG_LOCATION,
    SEG_POKE, SEG_MUSIC, SEG_DICE, SEG_RPS, SEG_FORWARD, SEG_MARKDOWN,
    SEG_KEYBOARD,
})

#: 各协议叫法 → 规范类型。接入端只要产出规范名，别名表只是为了兼容历史写法
#: 与第三方实现（比如老插件仍在用 OneBot 的 `record` 表示语音）。
_TYPE_ALIASES: Dict[str, str] = {
    # 语音：OneBot 叫 record，QQ 官方/Telegram 叫 audio/voice
    'record': SEG_VOICE, 'audio': SEG_VOICE, 'voice': SEG_VOICE,
    'sound': SEG_VOICE, 'ptt': SEG_VOICE,
    # 图片
    'img': SEG_IMAGE, 'image': SEG_IMAGE, 'pic': SEG_IMAGE, 'photo': SEG_IMAGE,
    'picture': SEG_IMAGE,
    # 文件
    'file': SEG_FILE, 'document': SEG_FILE, 'doc': SEG_FILE, 'attachment': SEG_FILE,
    # 视频
    'video': SEG_VIDEO, 'movie': SEG_VIDEO, 'animation': SEG_VIDEO,
    # 提及
    'at': SEG_AT, 'mention': SEG_AT, 'at_all': SEG_AT,
    # 回复
    'reply': SEG_REPLY, 'quote': SEG_REPLY,
    # 表情
    'face': SEG_FACE, 'emoji': SEG_FACE, 'sticker': SEG_STICKER,
    # 结构
    'json': SEG_JSON, 'xml': SEG_XML, 'card': SEG_JSON,
    'share': SEG_SHARE, 'link': SEG_SHARE,
    'location': SEG_LOCATION, 'geo': SEG_LOCATION,
    'poke': SEG_POKE, 'music': SEG_MUSIC,
    'dice': SEG_DICE, 'rps': SEG_RPS,
    'forward': SEG_FORWARD, 'markdown': SEG_MARKDOWN, 'keyboard': SEG_KEYBOARD,
    'text': SEG_TEXT, 'plain': SEG_TEXT,
}

#: 内容型段 data 的约定键。除这些之外允许携带协议私有字段。
MEDIA_DATA_KEYS = (
    'file',        # 统一文件引用：URL / 本地路径 / file_id / base64://...
    'url',         # 可直接下载的 http(s) 地址
    'path',        # 本地磁盘路径
    'base64',      # 裸 base64 内容（不带 data: 前缀）
    'file_id',     # 协议侧文件标识（Telegram / QQ 官方）
    'file_info',   # QQ 官方上传后返回的文件信息
    'name',        # 文件名
    'mime',        # MIME 类型
    'size',        # 字节大小
    'duration',    # 音视频时长（秒）
    'width', 'height', 'thumb',
)

#: 媒体段里"文件引用"的取值优先级
_REF_KEYS = ('file', 'url', 'file_id', 'file_info', 'path', 'base64')

_CQ_RE = re.compile(r'\[CQ:([A-Za-z0-9_\-]+)((?:,[^\]]*)?)\]')
_CQ_UNESCAPE = (
    ('&#44;', ','), ('&#91;', '['), ('&#93;', ']'), ('&amp;', '&'),
)


def canonical_type(type_name: Any) -> str:
    """把任意协议的段类型名映射到规范类型（无法识别时按原名小写返回）。"""
    if not isinstance(type_name, str):
        return SEG_TEXT
    key = type_name.strip().lower()
    return _TYPE_ALIASES.get(key, key)


def is_media(segment: dict) -> bool:
    """该段是否承载媒体内容"""
    return bool(segment) and canonical_type(segment.get('type')) in MEDIA_TYPES


def seg(type_name: str, data: Optional[dict] = None) -> dict:
    """构造一个规范段（自动做类型别名归一）"""
    return {'type': canonical_type(type_name), 'data': dict(data or {})}


# ── 便捷构造（给插件与接入端用，省得手拼 dict）──────────────────

def text(content: str) -> dict:
    return seg(SEG_TEXT, {'text': '' if content is None else str(content)})


def at(qq, name: str = '') -> dict:
    """@某人；qq='all' 即 @全体"""
    return seg(SEG_AT, {'qq': str(qq), 'name': name})


def at_all() -> dict:
    return at('all')


def image(file=None, url: str = '', base64: str = '', **extra) -> dict:
    return seg(SEG_IMAGE, _media_data(file, url, base64, extra))


def voice(file=None, url: str = '', base64: str = '', duration: int = 0, **extra) -> dict:
    data = _media_data(file, url, base64, extra)
    if duration:
        data['duration'] = int(duration)
    return seg(SEG_VOICE, data)


def video(file=None, url: str = '', base64: str = '', **extra) -> dict:
    return seg(SEG_VIDEO, _media_data(file, url, base64, extra))


def file_seg(file=None, url: str = '', name: str = '', **extra) -> dict:
    data = _media_data(file, url, '', extra)
    if name:
        data['name'] = name
    return seg(SEG_FILE, data)


def reply(message_id) -> dict:
    return seg(SEG_REPLY, {'id': str(message_id)})


def face(face_id) -> dict:
    return seg(SEG_FACE, {'id': str(face_id)})


def _media_data(file, url: str, base64: str, extra: dict) -> dict:
    data: Dict[str, Any] = {}
    if file:
        data['file'] = str(file)
    if url:
        data['url'] = str(url)
    if base64:
        data['base64'] = str(base64)
    data.update(extra or {})
    return data


def media_ref(segment: dict) -> str:
    """
    取媒体段的"文件引用"：URL / 路径 / file_id / base64://...

    各协议把文件放在不同字段（OneBot 的 `file`、Telegram 的 `file_id`、
    Discord 的 `url`、QQ 官方的 `file_info`），插件不应该关心这些差异，
    统一用本函数取值。取不到返回空串。
    """
    if not isinstance(segment, dict):
        return ''
    data = segment.get('data')
    if not isinstance(data, dict):
        # 允许直接传 data 字典（ev.first_voice 返回的就是 data），
        # 省得调用方还得回头去 segments 里捞整个段
        if 'type' in segment:
            return ''
        data = segment
    for key in _REF_KEYS:
        val = data.get(key)
        if val is None or val == '':
            continue
        if key == 'base64':
            return f'base64://{val}'
        return str(val)
    return ''


def media_ref_is_local(segment: dict) -> bool:
    """引用是否是本地磁盘路径（接入端多半需要先上传再发送）"""
    ref = media_ref(segment)
    if not ref:
        return False
    if ref.startswith(('http://', 'https://', 'base64://', 'file_id:')):
        return False
    return True


# ─────────────────────────────────────────────────────────────
# 归一化
# ─────────────────────────────────────────────────────────────

def _unescape_cq(value: str) -> str:
    for src, dst in _CQ_UNESCAPE:
        value = value.replace(src, dst)
    return value


def parse_cq(raw: str) -> List[dict]:
    """
    解析 OneBot CQ 码字符串为规范段（用于兼容字符串格式的历史消息）。

    「你好[CQ:at,qq=123]」→ [text, at]
    """
    if not isinstance(raw, str) or not raw:
        return []
    out: List[dict] = []
    last = 0
    for m in _CQ_RE.finditer(raw):
        head = raw[last:m.start()]
        if head:
            out.append(text(head))
        last = m.end()
        cq_type = canonical_type(m.group(1))
        data: Dict[str, Any] = {}
        for pair in (m.group(2) or '').split(','):
            if not pair or '=' not in pair:
                continue
            k, _, v = pair.partition('=')
            data[k.strip()] = _unescape_cq(v)
        if cq_type == SEG_TEXT:
            out.append(text(data.get('text', '')))
        else:
            out.append(seg(cq_type, data))
    tail = raw[last:]
    if tail:
        out.append(text(tail))
    return out


def normalize_segment(segment: Any) -> Optional[dict]:
    """单个段的规范化；无法识别返回 None。"""
    if isinstance(segment, str):
        if not segment:
            return None
        return text(segment)
    if not isinstance(segment, dict):
        return None
    stype = canonical_type(segment.get('type') or segment.get('seg') or SEG_TEXT)
    data = segment.get('data')
    if not isinstance(data, dict):
        # 有些实现把字段平铺在段上（如 {'type':'at','qq':123}）
        data = {k: v for k, v in segment.items() if k not in ('type', 'seg', 'data')}
    data = dict(data)
    if stype == SEG_TEXT and 'text' not in data:
        data['text'] = ''
    # at 段的 qq 统一成字符串，避免 int/str 混用导致 == 判断失效
    if stype == SEG_AT and data.get('qq') is not None:
        data['qq'] = str(data['qq'])
    if stype == SEG_REPLY and data.get('id') is not None:
        data['id'] = str(data['id'])
    return {'type': stype, 'data': data}


def normalize_message(message: Any, parse_string_cq: bool = True) -> List[dict]:
    """
    把任意形态的消息归一化成规范段列表。

    支持输入：
    - 规范/OneBot 段数组  [{'type':'image','data':{...}}]
    - 单个段 dict
    - 纯字符串（默认按 CQ 码解析；关掉则整段视为 text）
    - None / 空 → []
    """
    if message is None:
        return []
    if isinstance(message, dict):
        one = normalize_segment(message)
        return [one] if one else []
    if isinstance(message, (list, tuple)):
        out = []
        for item in message:
            one = normalize_segment(item)
            if one is not None:
                out.append(one)
        return out
    if isinstance(message, str):
        if parse_string_cq and '[CQ:' in message:
            return parse_cq(message)
        return [text(message)] if message else []
    return [text(str(message))]


def segments_to_text(segments: List[dict], at_as_text: bool = True,
                     media_hint: bool = False) -> str:
    """
    规范段 → 纯文本（命令匹配 / 关键词 / 日志用）。

    - at 默认渲染成 `[@qq]`，与历史行为一致；
    - share / json 段提取其中的链接；
    - media_hint=True 时给媒体段补 `[image:xxx]` 之类的占位（默认关闭，
      保证"纯图片无文字"的消息文本仍为空串，不破坏既有命令匹配）。
    """
    if isinstance(segments, str):
        return segments
    parts: List[str] = []
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        stype = canonical_type(s.get('type'))
        data = s.get('data') or {}
        if stype == SEG_TEXT:
            parts.append(str(data.get('text', '')))
        elif stype == SEG_MARKDOWN:
            parts.append(str(data.get('content') or data.get('text') or ''))
        elif stype == SEG_AT and at_as_text:
            parts.append(f'[@{data.get("qq", "")}]')
        elif stype == SEG_SHARE:
            if data.get('url'):
                parts.append(str(data['url']))
        elif stype in (SEG_JSON, SEG_XML):
            raw = str(data.get('data') or data.get('content') or '')
            for m in re.finditer(r'https?://[^\s"\'<]+', raw):
                parts.append(m.group(0))
        elif stype == SEG_REPLY:
            continue
        elif media_hint and stype in MEDIA_TYPES:
            ref = media_ref(s)
            parts.append(f'[{stype}:{ref}]' if ref else f'[{stype}]')
    return ''.join(parts)


def pick(segments: List[dict], *types: str) -> List[dict]:
    """按规范类型挑段（类型名会先做别名归一）"""
    wanted = {canonical_type(t) for t in types}
    return [s for s in segments or []
            if isinstance(s, dict) and canonical_type(s.get('type')) in wanted]


def data_list(segments: List[dict], type_name: str) -> List[dict]:
    """取某类型的所有 data（如 `data_list(ev.segments,'voice')`）"""
    return [s.get('data') or {} for s in pick(segments, type_name)]


def first(segments: List[dict], type_name: str) -> dict:
    """取某类型的第一个 data；没有返回空 dict"""
    got = pick(segments, type_name)
    return (got[0].get('data') or {}) if got else {}


def has(segments: List[dict], *types: str) -> bool:
    return bool(pick(segments, *types))


def media_segments(segments: List[dict]) -> List[dict]:
    """所有内容型段（图片/语音/视频/文件/贴纸）"""
    return [s for s in segments or [] if is_media(s)]


def is_media_only(segments: List[dict]) -> bool:
    """是否"只有媒体没有文字"（纯图片/纯语音消息常见）"""
    segs = [s for s in segments or [] if isinstance(s, dict)]
    if not segs:
        return False
    if not media_segments(segs):
        return False
    return not segments_to_text(segs).strip()


def to_outgoing(message: Any, adapter=None) -> Any:
    """
    出站转换：插件给的任意形态 → 规范段 → 接入端原生结构。

    - 接入端实现了 `to_native(segments)` 时按它的结果走；
    - 未实现时原样返回规范段（OneBot 系接入端可直接吃）。
    """
    segments = normalize_message(message)
    if adapter is None:
        return segments
    to_native = getattr(adapter, 'to_native', None)
    if not callable(to_native):
        return segments
    try:
        native = to_native(segments)
    except Exception:  # noqa: BLE001 - 接入端翻译失败不应炸掉发送链路
        return segments
    return native if native is not None else segments


__all__ = [
    'SEG_TEXT', 'SEG_AT', 'SEG_FACE', 'SEG_IMAGE', 'SEG_VOICE', 'SEG_VIDEO',
    'SEG_FILE', 'SEG_STICKER', 'SEG_REPLY', 'SEG_SHARE', 'SEG_JSON', 'SEG_XML',
    'SEG_LOCATION', 'SEG_POKE', 'SEG_MUSIC', 'SEG_DICE', 'SEG_RPS',
    'SEG_FORWARD', 'SEG_MARKDOWN', 'SEG_KEYBOARD',
    'MEDIA_TYPES', 'TEXT_TYPES', 'STRUCT_TYPES', 'ALL_TYPES', 'MEDIA_DATA_KEYS',
    'canonical_type', 'is_media', 'seg', 'text', 'at', 'at_all', 'image',
    'voice', 'video', 'file_seg', 'reply', 'face',
    'media_ref', 'media_ref_is_local', 'parse_cq', 'normalize_segment',
    'normalize_message', 'segments_to_text', 'pick', 'data_list', 'first',
    'has', 'media_segments', 'is_media_only', 'to_outgoing',
]
