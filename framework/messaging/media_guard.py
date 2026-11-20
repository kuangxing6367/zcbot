"""媒体内容统一闸门：图片字节校验 + URL/重定向目标校验。

为什么放在框架层
----------------
媒体段在规范层只是引用（URL / base64 / file_id / 本地路径），框架以前不主动
下载，于是「内容是不是图片」「这个 URL 能不能请求」全靠每个插件自觉。实测
`evil.png` 的纯文本 base64 能被当图片落盘，内网地址也能直接下载（CWE-918），
而完整的魔数/像素预算检查只存在于一个渲染器插件里。

本模块把两件事收口成统一入口：

1. `inspect_image_bytes()` —— 解码前的 fail-closed 字节校验；
2. `check_url()` / `check_redirect_chain()` —— 请求前的地址校验与逐跳重定向复查。

所有拒绝都返回**明确的 reason 常量**，调用方可以直接展示或记日志；不会用
一个含糊的 `failed` 把「文件太大」和「这是 SVG 脚本」混为一谈。

约定：只读文件头，不做像素解码；任何解析异常都按拒绝处理。
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import warnings
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger('zcbot')

# ─────────────────────────────────────────────────────────────
# 拒绝原因常量（给调用方展示 / 统计用）
# ─────────────────────────────────────────────────────────────

REJECT_EMPTY = 'empty'
REJECT_TOO_LARGE = 'too_large'
REJECT_UNSUPPORTED_TYPE = 'unsupported_type'
REJECT_ACTIVE_CONTENT = 'active_content'
REJECT_HEADER_PARSE = 'header_parse_failed'
REJECT_INVALID_DIMENSION = 'invalid_dimension'
REJECT_PIXEL_BOMB = 'pixel_bomb'
REJECT_FRAME_LIMIT = 'frame_limit'
REJECT_TRAILING_DATA = 'trailing_data'
REJECT_INVALID_URL = 'invalid_url'
REJECT_USERINFO = 'url_userinfo'
REJECT_BLOCKED_HOST = 'blocked_host'
REJECT_BLOCKED_ADDRESS = 'blocked_address'
REJECT_REDIRECT = 'redirect_blocked'
REJECT_REQUEST = 'request_failed'

# ─────────────────────────────────────────────────────────────
# 预算与白名单
# ─────────────────────────────────────────────────────────────

#: 压缩图片字节上限（解码前）。与 image_renderer 原有预算保持一致。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: 40M 像素约 160MB RGBA 位图。低于 Pillow 自己的警告阈值，留足安全边界。
MAX_PIXELS = 40_000_000
MAX_FRAMES = 200
MAX_URL_CHARS = 2048
DEFAULT_MAX_REDIRECTS = 30

#: 魔数 → (规范 kind, MIME)。只放行这五种静态图片。
_IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b'\x89PNG\r\n\x1a\n', 'png', 'image/png'),
    (b'\xff\xd8\xff', 'jpeg', 'image/jpeg'),
    (b'GIF87a', 'gif', 'image/gif'),
    (b'GIF89a', 'gif', 'image/gif'),
    (b'BM', 'bmp', 'image/bmp'),
)
_ALLOWED_FORMATS = frozenset({'PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'})

#: 头部即能看出的活动内容：SVG / HTML / DOCTYPE/ENTITY / EPS / PostScript / MVG。
_FORBIDDEN_HEAD_MARKERS = (
    b'<svg', b'<!doctype', b'<!entity', b'<script', b'xmlns=',
    b'push graphic-context', b'%!ps', b'<?php',
)
#: 全文扫描的活动载荷标记（EXIF/XMP/注释区/尾随数据里的 polyglot）。
_FORBIDDEN_CONTENT_MARKERS = (
    b'<script', b'<svg', b'<?php', b'<!entity', b'onerror=', b'onload=',
    b'javascript:', b'eval(', b'<html', b'<body',
    b'push graphic-context', b'%!ps',
)

_BLOCKED_HOSTNAMES = frozenset({
    'localhost', 'localhost.localdomain',
    'metadata', 'metadata.google.internal', 'metadata.goog',
    'instance-data', 'instance-data.ec2.internal',
    'metadata.azure.internal', 'kubernetes.default.svc',
})
_BLOCKED_METADATA_IPS = frozenset({
    '169.254.169.254', '100.100.100.200', '192.0.0.192',
})


def _ok(kind: str = '', mime: str = '', **extra) -> dict:
    result = {'ok': True, 'kind': kind, 'mime': mime,
              'rejected_reason': '', 'msg': ''}
    result.update(extra)
    return result


def _reject(reason: str, msg: str, kind: str = '', mime: str = '', **extra) -> dict:
    result = {'ok': False, 'kind': kind, 'mime': mime,
              'rejected_reason': reason, 'msg': msg}
    result.update(extra)
    return result


def detect_image_kind(data: bytes) -> tuple[str, str]:
    """按真实 magic bytes 返回 ``(kind, mime)``；非白名单返回空串。"""
    if not isinstance(data, bytes):
        return '', ''
    for sig, kind, mime in _IMAGE_MAGIC:
        if data.startswith(sig):
            return kind, mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    return '', ''


def inspect_image_bytes(data, *, max_bytes: int = MAX_IMAGE_BYTES,
                        max_pixels: int = MAX_PIXELS,
                        max_frames: int = MAX_FRAMES) -> dict:
    """解码前的 fail-closed 图片校验。

    只读文件头（Pillow 的 `open` 是惰性的），返回统一结构::

        {'ok': bool, 'kind': str, 'mime': str,
         'rejected_reason': str, 'msg': str, ...}

    魔数确定 kind/mime；活动内容、尺寸/帧数预算、格式结束标记全部在解码前
    检查。`DecompressionBombWarning` 当错误处理，任何解析异常都拒绝。
    """
    try:
        raw = data if isinstance(data, bytes) else bytes(data)
    except Exception as e:
        return _reject(REJECT_UNSUPPORTED_TYPE,
                       f'图片数据无法转换为字节: {type(e).__name__}')
    if not raw:
        return _reject(REJECT_EMPTY, '图片为空，已拒绝')
    if len(raw) > max_bytes:
        return _reject(REJECT_TOO_LARGE,
                       f'图片文件超过 {max_bytes} 字节上限，已拒绝')
    head = raw[:4096].lower()
    for marker in _FORBIDDEN_HEAD_MARKERS:
        if marker in head:
            return _reject(REJECT_ACTIVE_CONTENT,
                           '图片内含脚本/外部实体/PostScript/MVG 内容，已拒绝',
                           kind='svg' if b'<svg' in head else '')
    kind, mime = detect_image_kind(raw)
    if not kind:
        return _reject(REJECT_UNSUPPORTED_TYPE,
                       '不是允许的图片格式（仅 PNG/JPEG/GIF/WEBP/BMP），已拒绝')
    low_all = raw.lower()
    for marker in _FORBIDDEN_CONTENT_MARKERS:
        if marker in low_all:
            return _reject(
                REJECT_ACTIVE_CONTENT,
                '图片内嵌脚本/外部实体/活动载荷标记 %r，已拒绝' % marker.decode('latin1'),
                kind=kind, mime=mime)

    try:
        from io import BytesIO

        from PIL import Image as PILImage

        with warnings.catch_warnings():
            warnings.simplefilter('error', PILImage.DecompressionBombWarning)
            with PILImage.open(BytesIO(raw)) as probe:
                fmt = (probe.format or '').upper()
                width, height = probe.size
                frames = int(getattr(probe, 'n_frames', 1) or 1)
    except Exception as e:
        return _reject(
            REJECT_HEADER_PARSE,
            f'图片头解析失败或触发解压炸弹警告，已拒绝: {type(e).__name__}',
            kind=kind, mime=mime)

    if fmt not in _ALLOWED_FORMATS:
        return _reject(REJECT_UNSUPPORTED_TYPE,
                       f'不允许的图片格式 {fmt!r}，已拒绝', kind=kind, mime=mime)
    if width <= 0 or height <= 0:
        return _reject(REJECT_INVALID_DIMENSION, '图片尺寸非法，已拒绝',
                       kind=kind, mime=mime)
    if width * height > max_pixels:
        return _reject(
            REJECT_PIXEL_BOMB,
            f'图片尺寸过大已拒绝：{width}×{height} = {width * height / 1e6:.1f} '
            f'百万像素，上限 {max_pixels / 1e6:.1f} 百万像素',
            kind=kind, mime=mime)
    if frames > max_frames:
        return _reject(REJECT_FRAME_LIMIT,
                       f'动画帧数 {frames} 超过上限 {max_frames}，已拒绝',
                       kind=kind, mime=mime)

    if fmt == 'PNG':
        iend = raw.rfind(b'IEND\xaeB`\x82')
        if iend < 0 or len(raw) - (iend + 8) != 0:
            return _reject(REJECT_TRAILING_DATA,
                           'PNG 缺 IEND 或 IEND 后有尾随数据，已拒绝',
                           kind=kind, mime=mime)
    elif fmt == 'JPEG':
        eoi = raw.rfind(b'\xff\xd9')
        if eoi < 0 or len(raw) - (eoi + 2) != 0:
            return _reject(REJECT_TRAILING_DATA,
                           'JPEG 缺 EOI 或 EOI 后有尾随数据，已拒绝',
                           kind=kind, mime=mime)
    elif fmt == 'GIF' and not raw.endswith(b'\x3b'):
        return _reject(REJECT_TRAILING_DATA,
                       'GIF 缺 trailer 或尾部附加数据，已拒绝',
                       kind=kind, mime=mime)

    return _ok(kind, mime, width=width, height=height, frames=frames,
               format=fmt, size=len(raw))


def _address_blocked(address: str) -> bool:
    if address in _BLOCKED_METADATA_IPS:
        return True
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or not ip.is_global)


def _resolve_host(host: str) -> list[str]:
    """默认解析器：只取地址字符串；解析失败返回空列表，由调用方决定是否拒绝。"""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as e:
        logger.debug('[media_guard] DNS 解析失败 %s: %s', host, e)
        return []
    return sorted({str(info[4][0]) for info in infos})


def check_url(url: str, resolver=None) -> dict:
    """请求前的 URL 静态 + DNS 校验。

    `resolver` 签名为 ``host -> list[str]``，测试可注入；默认走 `getaddrinfo`。
    DNS 解析不出来时不额外拒绝（连接本身也会失败），但解析出的每个地址都要
    再过一次地址黑名单，缩小 DNS rebinding 的窗口。
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return _reject(REJECT_INVALID_URL, 'URL 为空或过长，已拒绝')
    try:
        parts = urlsplit(url)
    except ValueError as e:
        return _reject(REJECT_INVALID_URL, f'URL 解析失败: {e}')
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return _reject(REJECT_INVALID_URL, '只允许 http(s) 且必须带主机名，已拒绝')
    if parts.username or parts.password:
        return _reject(REJECT_USERINFO, 'URL 不允许携带 userinfo，已拒绝')
    host = parts.hostname.lower().strip('[]')
    if host in _BLOCKED_HOSTNAMES:
        return _reject(REJECT_BLOCKED_HOST, f'禁止请求主机 {host}，已拒绝')

    if _looks_like_ip(host) and _address_blocked(host):
        return _reject(REJECT_BLOCKED_ADDRESS,
                       f'禁止请求地址 {host}，已拒绝', host=host)
    resolve = resolver or _resolve_host
    try:
        addresses = list(resolve(host) or [])
    except Exception as e:
        logger.debug('[media_guard] 注入的 DNS 解析器失败 %s: %s', host, e)
        addresses = []
    for address in addresses:
        if _address_blocked(address):
            return _reject(REJECT_BLOCKED_ADDRESS,
                           f'域名 {host} 解析到禁止地址 {address}，已拒绝',
                           host=host, addresses=addresses)
    return _ok(host=host, addresses=addresses)


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _location_of(resp) -> str:
    headers = getattr(resp, 'headers', None) or {}
    try:
        location = headers.get('Location') or headers.get('location') or ''
    except AttributeError:
        return ''
    return str(location).strip()


def _close(resp) -> None:
    close = getattr(resp, 'close', None)
    if callable(close):
        try:
            close()
        except Exception as e:
            logger.debug('[media_guard] 响应关闭失败（不影响结果）: %s', e)


def check_redirect_chain(url: str, *, fetcher=None, resolver=None,
                         timeout: float = 30, max_redirects: int = DEFAULT_MAX_REDIRECTS,
                         headers: dict | None = None) -> dict:
    """逐跳复查 URL 与重定向目标，返回最终仍-open 的响应供调用方流式读取。

    `requests` 默认跟随重定向且不复查目标 host，公网跳内网（CWE-918）就是这么
    进来的。这里显式关掉自动跟随，每一跳都过 `check_url()`，且校验通过前不发
    下一个请求。响应对象放在结果的 `response` 字段；失败路径会关掉已拿到的响应。
    """
    import requests

    get = fetcher or requests.get
    opts = {'stream': True, 'timeout': (5, timeout),
            'headers': headers or {'User-Agent': 'zcbot-media/1.0'},
            'allow_redirects': False}
    current = url
    hops: list[str] = []
    for _hop in range(max_redirects + 1):
        verdict = check_url(current, resolver=resolver)
        if not verdict['ok']:
            return _reject(verdict['rejected_reason'], verdict['msg'],
                           hops=hops)
        try:
            resp = get(current, **opts)
        except Exception as e:
            return _reject(REJECT_REQUEST,
                           f'请求失败: {type(e).__name__}: {e}', hops=hops)
        # 万一注入的 fetcher 忽略了 allow_redirects，仍要逐段复查历史 URL。
        history = getattr(resp, 'history', None) or []
        for item in history:
            hist_url = getattr(item, 'url', '') or ''
            if hist_url:
                checked = check_url(hist_url, resolver=resolver)
                if not checked['ok']:
                    _close(resp)
                    return _reject(checked['rejected_reason'],
                                   f'重定向历史目标被拒绝: {checked["msg"]}',
                                   hops=hops)
        final_url = getattr(resp, 'url', '') or current
        checked = check_url(final_url, resolver=resolver)
        if not checked['ok']:
            _close(resp)
            return _reject(checked['rejected_reason'],
                           f'最终 URL 被拒绝: {checked["msg"]}', hops=hops)
        status = getattr(resp, 'status_code', 200)
        location = _location_of(resp)
        if not (300 <= status < 400 and location):
            return _ok(url=final_url, response=resp, hops=hops,
                       host=checked.get('host', ''))

        target = urljoin(current, location)
        checked = check_url(target, resolver=resolver)
        _close(resp)
        if not checked['ok']:
            return _reject(REJECT_REDIRECT,
                           f'重定向目标被拒绝: {checked["msg"]}',
                           hops=hops + [target])
        hops.append(target)
        current = target
    return _reject(REJECT_REDIRECT,
                   f'重定向次数超过上限 {max_redirects}，已拒绝', hops=hops)


__all__ = [
    'DEFAULT_MAX_REDIRECTS',
    'MAX_FRAMES',
    'MAX_IMAGE_BYTES',
    'MAX_PIXELS',
    'MAX_URL_CHARS',
    'REJECT_ACTIVE_CONTENT',
    'REJECT_BLOCKED_ADDRESS',
    'REJECT_BLOCKED_HOST',
    'REJECT_EMPTY',
    'REJECT_FRAME_LIMIT',
    'REJECT_HEADER_PARSE',
    'REJECT_INVALID_DIMENSION',
    'REJECT_INVALID_URL',
    'REJECT_PIXEL_BOMB',
    'REJECT_REDIRECT',
    'REJECT_REQUEST',
    'REJECT_TOO_LARGE',
    'REJECT_TRAILING_DATA',
    'REJECT_UNSUPPORTED_TYPE',
    'REJECT_USERINFO',
    'check_redirect_chain',
    'check_url',
    'detect_image_kind',
    'inspect_image_bytes',
]
