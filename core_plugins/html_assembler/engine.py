# -*- coding: utf-8 -*-
"""
html_assembler 装配引擎 —— 单文件 HTML 装配（纯标准库，可脱离 zcbot 独立 import 使用）

职责（仅文本层面装配）：
    接收数据 → 替换占位符 → 图片转 base64 内嵌 → 输出完整 HTML
不启动浏览器、不执行 JS、不解析 CSS、不画像素 —— 全部交给下游浏览器。

占位符语法（写在 HTML 模板里）：
    {{ key }}      变量替换，自动 HTML 转义；支持嵌套 a.b.c 与列表下标 items.0.name
    {{ img:key }}  图片内嵌，替换为 data:image/xxx;base64,...（key 查 images 表）
    {{ raw:key }}  原文插入，不转义（内嵌 HTML/CSS/JS 片段用）

内存可控：模板 / 单图 / 输出均有字节上限，单次装配有图片数上限。
"""
import base64
import html as _htmlmod
import os
import re

__all__ = [
    'HtmlAssembler', 'load_template', 'render_html', 'render_to_file',
    'embed_image', 'make_assembler', 'configure', 'get_defaults',
    'TemplateTooLarge', 'ImageTooLarge', 'OutputTooLarge', 'TooManyImages',
]

# 占位符：{{ 前缀:key }} / {{ key }}，key 为「点分段」，段 = 字母数字下划线
_PLACEHOLDER_RE = re.compile(
    r'\{\{\s*(?:(img|raw):)?([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\s*\}\}')

# 可配置项与默认值（与插件 _conf_schema.json 保持一致；
# 官方插件 register 时用 core_plugins.yaml 的 html_assembler 段覆盖）
_DEFAULTS = {
    'max_template_bytes': 4 * 1024 * 1024,    # 模板最大字节数
    'max_image_bytes': 4 * 1024 * 1024,       # 单张图片最大字节数
    'max_output_bytes': 16 * 1024 * 1024,     # 输出 HTML 最大字节数
    'max_images': 64,                         # 单次装配最多内嵌图片数
    'escape_html': True,                      # 变量替换是否 HTML 转义
    'on_missing': 'keep',                     # keep / empty / raise
}
_ALLOWED_KEYS = frozenset(_DEFAULTS)


# ── 异常 ────────────────────────────────────────────────────
class TemplateTooLarge(ValueError):
    pass


class ImageTooLarge(ValueError):
    pass


class OutputTooLarge(ValueError):
    pass


class TooManyImages(ValueError):
    pass


def configure(opts: dict) -> dict:
    """用插件配置覆盖引擎默认值（仅接受已知键，None 跳过），返回当前生效配置"""
    for k, v in dict(opts or {}).items():
        if k in _ALLOWED_KEYS and v is not None:
            _DEFAULTS[k] = v
    return get_defaults()


def get_defaults() -> dict:
    """返回当前生效的默认配置副本"""
    return dict(_DEFAULTS)


def _opts(**overrides) -> dict:
    """默认值 + 单次调用覆盖项（忽略未知键交给上层校验）"""
    d = dict(_DEFAULTS)
    for k, v in overrides.items():
        if k in _ALLOWED_KEYS and v is not None:
            d[k] = v
    return d


# ── 模板 / 图片读取 ──────────────────────────────────────────

def _read_limited(path: str, limit: int, err) -> bytes:
    """读文件前先查大小上限，防止超限内容被整读进内存"""
    size = os.path.getsize(path)
    if size > limit:
        raise err(f"{path} 大小 {size} 字节，超过上限 {limit} 字节")
    with open(path, 'rb') as f:
        return f.read()


def _decode_template(raw: bytes, limit: int) -> str:
    """模板字节 → 文本（UTF-8 优先，GBK 自适应回退）"""
    if len(raw) > limit:
        raise TemplateTooLarge(f"模板大小 {len(raw)} 字节，超过上限 {limit} 字节")
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('gbk')


def load_template(path: str, **overrides) -> str:
    """读取模板文件（UTF-8/GBK 自适应），超上限抛 TemplateTooLarge"""
    o = _opts(**overrides)
    return _decode_template(_read_limited(path, o['max_template_bytes'],
                                         TemplateTooLarge), o['max_template_bytes'])


_MIME_BY_EXT = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
}


def _sniff_mime(path, head: bytes) -> str:
    """先按扩展名，再按魔数猜 MIME；都不中按 image/png 兜底提示性最差，故用 octet-stream"""
    ext = os.path.splitext(path or '')[1].lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    if head.startswith(b'\x89PNG'):
        return 'image/png'
    if head.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if head.startswith(b'BM'):
        return 'image/bmp'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.lstrip().startswith((b'<?xml', b'<svg')):
        return 'image/svg+xml'
    return 'application/octet-stream'


def _embed(source, mime=None, o: dict = None) -> str:
    o = o or _opts()
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        if mime is None:
            mime = _sniff_mime(None, raw[:16])
    elif isinstance(source, str):
        s = source.strip()
        if s[:5].lower() == 'data:':
            return s                       # 已是 data URI，原样使用
        raw = _read_limited(s, o['max_image_bytes'], ImageTooLarge)
        if mime is None:
            mime = _sniff_mime(s, raw[:16])
    else:
        raise TypeError(f"图片来源仅支持 路径/bytes/data URI，得到 {type(source).__name__}")
    if len(raw) > o['max_image_bytes']:
        raise ImageTooLarge(f"图片大小 {len(raw)} 字节，超过上限 {o['max_image_bytes']} 字节")
    if not mime.startswith('image/'):
        raise ValueError(f"非图片 MIME: {mime}")
    return f"data:{mime};base64," + base64.b64encode(raw).decode('ascii')


def embed_image(source, mime=None) -> str:
    """图片（路径 / bytes / data URI）→ data:image/xxx;base64,... 字符串"""
    return _embed(source, mime)


# ── 占位符解析 ───────────────────────────────────────────────

class _Missing:
    """占位符解析失败的哨兵（与「值恰好是 None」区分开）"""
    __slots__ = ()


_MISSING = _Missing()


def _resolve(data, key: str):
    """按点分路径取值：dict 键 + 列表数字下标；取不到返回 _MISSING"""
    cur = data
    for seg in key.split('.'):
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
                continue
            return _MISSING
        if isinstance(cur, (list, tuple)) and seg.isdigit():
            i = int(seg)
            if i < len(cur):
                cur = cur[i]
                continue
        return _MISSING
    return cur


# ── 装配器 ──────────────────────────────────────────────────

class HtmlAssembler:
    """链式装配器：set/set_many 赋值、add_image(s) 塞图，render/render_to_file 出结果"""

    def __init__(self, template_path: str = None, template: str = None, **opts):
        unknown = set(opts) - _ALLOWED_KEYS
        if unknown:
            raise TypeError(f"未知装配选项: {', '.join(sorted(unknown))}")
        self._opts = _opts(**opts)
        self._template = None
        self._data = {}
        self._images = {}
        self.last_stats = {'replaced': 0, 'missing': 0, 'images': 0,
                           'output_bytes': 0}
        if template_path is not None:
            self.set_template_path(template_path)
        elif template is not None:
            self.set_template(template)

    # -- 模板 --

    def set_template(self, text: str) -> 'HtmlAssembler':
        """直接给模板文本"""
        raw = text.encode('utf-8')
        if len(raw) > self._opts['max_template_bytes']:
            raise TemplateTooLarge(
                f"模板大小 {len(raw)} 字节，超过上限 {self._opts['max_template_bytes']} 字节")
        self._template = text
        return self

    def set_template_path(self, path: str) -> 'HtmlAssembler':
        """从文件读模板（UTF-8/GBK 自适应）"""
        self._template = load_template(path, **self._opts)
        return self

    # -- 数据 / 图片 --

    def set(self, key: str, value) -> 'HtmlAssembler':
        self._data[key] = value
        return self

    def set_many(self, values: dict) -> 'HtmlAssembler':
        self._data.update(values or {})
        return self

    def add_image(self, key: str, source) -> 'HtmlAssembler':
        """塞一张图：路径 / bytes / data URI，渲染时才转 base64"""
        self._images[key] = source
        self._check_images()
        return self

    def add_images(self, sources: dict) -> 'HtmlAssembler':
        self._images.update(sources or {})
        self._check_images()
        return self

    def _check_images(self):
        limit = self._opts['max_images']
        if len(self._images) > limit:
            raise TooManyImages(
                f"单次装配图片数 {len(self._images)} 超过上限 {limit}")

    # -- 渲染 --

    def _missing(self, matched: str, key: str) -> str:
        policy = self._opts['on_missing']
        if policy == 'raise':
            raise KeyError(key)
        self._stats['missing'] += 1
        return '' if policy == 'empty' else matched   # keep：保留原占位符

    def render(self) -> str:
        """装配并返回完整 HTML 字符串"""
        if self._template is None:
            raise ValueError("未设置模板（构造时给 template/template_path，或 set_template）")
        o = self._opts
        self._stats = {'replaced': 0, 'missing': 0, 'images': 0}
        self._check_images()

        def _sub(m: 're.Match') -> str:
            prefix, key = m.group(1), m.group(2)
            if prefix == 'raw':
                v = _resolve(self._data, key)
                if v is _MISSING:
                    return self._missing(m.group(0), key)
                self._stats['replaced'] += 1
                return str(v)
            if prefix == 'img':
                if key not in self._images:
                    return self._missing(m.group(0), key)
                self._stats['images'] += 1
                return _embed(self._images[key], None, o)
            v = _resolve(self._data, key)
            if v is _MISSING:
                return self._missing(m.group(0), key)
            self._stats['replaced'] += 1
            if v is None:
                v = ''
            if o['escape_html']:
                return _htmlmod.escape(str(v), quote=True)
            return str(v)

        out = _PLACEHOLDER_RE.sub(_sub, self._template)
        nbytes = len(out.encode('utf-8'))
        if nbytes > o['max_output_bytes']:
            raise OutputTooLarge(
                f"输出 {nbytes} 字节，超过上限 {o['max_output_bytes']} 字节")
        self.last_stats = {'replaced': self._stats['replaced'],
                           'missing': self._stats['missing'],
                           'images': self._stats['images'],
                           'output_bytes': nbytes}
        return out

    def render_to_file(self, output_path: str) -> str:
        """装配并写盘（UTF-8，自动建目录），返回输出路径"""
        text = self.render()
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)
        self.last_stats['output_path'] = output_path
        return output_path


# ── 一键门面 ────────────────────────────────────────────────

def _coerce_template(template, o: dict) -> str:
    """template 参数既可为模板文本，也可为模板文件路径（存在即按路径读）"""
    if isinstance(template, str) and os.path.isfile(template):
        return load_template(template, **o)
    if not isinstance(template, str):
        raise TypeError("template 须为 HTML 文本或模板文件路径")
    if len(template.encode('utf-8')) > o['max_template_bytes']:
        raise TemplateTooLarge(
            f"模板大小 {len(template.encode('utf-8'))} 字节，超过上限 {o['max_template_bytes']} 字节")
    return template


def render_html(template, data: dict = None, images: dict = None,
                output_path: str = None, **opts) -> str:
    """一键装配。template 可为文本或文件路径；不传 output_path 返回 HTML 字符串，
    传则写盘并返回输出路径"""
    o = _opts(**opts)
    a = HtmlAssembler(**opts)
    a.set_template(_coerce_template(template, o))
    a.set_many(data or {})
    a.add_images(images or {})
    return a.render_to_file(output_path) if output_path else a.render()


def render_to_file(template, output_path: str, data: dict = None,
                   images: dict = None, **opts) -> str:
    """装配并写盘，返回输出路径"""
    return render_html(template, data=data, images=images,
                       output_path=output_path, **opts)


def make_assembler(**opts) -> HtmlAssembler:
    """创建独立装配器实例"""
    return HtmlAssembler(**opts)
