"""框架级媒体闸门：图片字节校验 + URL/重定向目标校验。

这里守的是「别人拿图片和链接当通道打进来」的那条线：伪 PNG、活动载荷、
像素炸弹、尾随数据、内网地址、DNS rebinding 与公网跳内网的重定向。
所有用例都离线，不发真实网络请求。
"""
import io
import struct
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.messaging.media_guard import (
    REJECT_ACTIVE_CONTENT,
    REJECT_BLOCKED_ADDRESS,
    REJECT_BLOCKED_HOST,
    REJECT_FRAME_LIMIT,
    REJECT_HEADER_PARSE,
    REJECT_INVALID_URL,
    REJECT_PIXEL_BOMB,
    REJECT_REDIRECT,
    REJECT_TOO_LARGE,
    REJECT_TRAILING_DATA,
    REJECT_UNSUPPORTED_TYPE,
    REJECT_USERINFO,
    check_redirect_chain,
    check_url,
    detect_image_kind,
    inspect_image_bytes,
)

# ─────────────────────────────────────────────────────────────
# 离线样本
# ─────────────────────────────────────────────────────────────

_PNG_SIG = b'\x89PNG\r\n\x1a\n'


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack('>I', len(payload)) + kind + payload
            + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))


def _png_declaring(width: int, height: int, extra: bytes = b'') -> bytes:
    """只把 IHDR 写成目标尺寸，IDAT 仍是 1 像素的量。

    Pillow 的 open() 是惰性的，读 IHDR 就够拿 size；这样能在不真的分配
    几百 MB 位图的情况下测像素预算。
    """
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b'\x00\x00\x00\x00')
    return (_PNG_SIG + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat)
            + _chunk(b'IEND', b'') + extra)


def _encoded(fmt: str, size=(2, 2)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', size, (30, 60, 90)).save(buf, fmt)
    return buf.getvalue()


def _public_resolver(host):
    return ['93.184.216.34']


def _private_resolver(host):
    return ['93.184.216.34', '10.0.0.7']


class _Resp:
    def __init__(self, status=200, location='', url='', history=None):
        self.status_code = status
        self.headers = {'Location': location} if location else {}
        self.url = url
        self.history = history or []
        self.closed = False

    def iter_content(self, size):
        yield b''

    def close(self):
        self.closed = True


# ─────────────────────────────────────────────────────────────
# 图片字节
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,kind,mime', [
    (_PNG_SIG + b'x', 'png', 'image/png'),
    (b'\xff\xd8\xff\xe0x', 'jpeg', 'image/jpeg'),
    (b'GIF87a' + b'x', 'gif', 'image/gif'),
    (b'GIF89a' + b'x', 'gif', 'image/gif'),
    (b'BM' + b'x', 'bmp', 'image/bmp'),
    (b'RIFF\x1a\x00\x00\x00WEBPVP8 x', 'webp', 'image/webp'),
    (b'<svg/>', '', ''),
])
def test_detect_image_kind(raw, kind, mime):
    assert detect_image_kind(raw) == (kind, mime)


@pytest.mark.parametrize('fmt,kind', [
    ('PNG', 'png'), ('JPEG', 'jpeg'), ('GIF', 'gif'),
    ('WEBP', 'webp'), ('BMP', 'bmp'),
])
def test_inspect_accepts_real_images(fmt, kind):
    verdict = inspect_image_bytes(_encoded(fmt))
    assert verdict['ok'] is True
    assert verdict['kind'] == kind
    assert verdict['mime'] == f'image/{kind}'
    assert verdict['rejected_reason'] == ''
    assert verdict['width'] > 0 and verdict['height'] > 0


def test_inspect_rejects_fake_png():
    """实测过的那种 evil.png：魔数对，后面全是纯文本。"""
    verdict = inspect_image_bytes(_PNG_SIG + b'not really a png')
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_HEADER_PARSE
    assert verdict['kind'] == 'png' and verdict['mime'] == 'image/png'


@pytest.mark.parametrize('payload', [
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">',
    b'<html><body><script>alert(1)</script></body></html>',
    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    b'%!PS-Adobe-3.0',
    b'push graphic-context',
    b'GIF89a' + b'<script>alert(1)</script>',
])
def test_inspect_rejects_active_content(payload):
    verdict = inspect_image_bytes(payload)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_ACTIVE_CONTENT


def test_inspect_rejects_pixel_budget():
    """7000×7000=4900 万像素：高于本模块预算，低于 Pillow 自己的警告阈值。"""
    verdict = inspect_image_bytes(_png_declaring(7000, 7000))
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_PIXEL_BOMB


def test_inspect_treats_decompression_bomb_warning_as_error():
    """1 亿像素会触发 Pillow 的警告；必须当错误，不能只打日志放行。"""
    verdict = inspect_image_bytes(_png_declaring(10000, 10000))
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_HEADER_PARSE


def test_inspect_rejects_trailing_data_after_png_iend():
    verdict = inspect_image_bytes(_encoded('PNG') + b'EVIL-PAYLOAD')
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_TRAILING_DATA


def test_inspect_rejects_over_limit():
    verdict = inspect_image_bytes(_encoded('PNG'), max_bytes=10)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_TOO_LARGE


def test_inspect_rejects_empty_and_non_image():
    assert inspect_image_bytes(b'')['rejected_reason'] == 'empty'
    assert inspect_image_bytes(b'hello')['rejected_reason'] == REJECT_UNSUPPORTED_TYPE


def test_inspect_rejects_too_many_frames():
    # 每帧颜色不同，避免 Pillow 把重复帧合并后 n_frames 低于 200。
    frames = [Image.new('RGB', (2, 2), (i % 256, 0, 0)) for i in range(201)]
    buf = io.BytesIO()
    frames[0].save(buf, 'GIF', save_all=True, append_images=frames[1:],
                   duration=10, loop=0)
    verdict = inspect_image_bytes(buf.getvalue())
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_FRAME_LIMIT


# ─────────────────────────────────────────────────────────────
# URL 与重定向
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('url', [
    'http://127.0.0.1/inner.png',
    'http://localhost/inner.png',
    'http://192.168.1.10/inner.png',
    'http://10.0.0.5/inner.png',
    'http://169.254.169.254/latest/meta-data/',
    'http://[::1]/inner.png',
    'http://224.0.0.1/inner.png',
    'http://0.0.0.0/inner.png',
])
def test_check_url_rejects_internal_targets(url):
    verdict = check_url(url, resolver=_public_resolver)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] in (REJECT_BLOCKED_ADDRESS, REJECT_BLOCKED_HOST)


@pytest.mark.parametrize('host', [
    'metadata', 'metadata.google.internal', 'instance-data',
    'localhost.localdomain',
])
def test_check_url_rejects_metadata_domains(host):
    verdict = check_url(f'http://{host}/latest/', resolver=_public_resolver)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_BLOCKED_HOST


@pytest.mark.parametrize('url', [
    'file:///etc/passwd',
    'ftp://example.com/a.png',
    'https://user@example.com/a.png',
    'https://user:pass@example.com/a.png',
    'https://',
    'x' * 3000,
])
def test_check_url_rejects_invalid_shape(url):
    verdict = check_url(url, resolver=_public_resolver)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] in (REJECT_INVALID_URL, REJECT_USERINFO)


def test_check_url_rechecks_dns_result():
    """域名字面量是公网，但解析结果混进私网地址：拒绝，缩小 rebinding 窗口。"""
    verdict = check_url('https://cdn.example.com/a.png', resolver=_private_resolver)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_BLOCKED_ADDRESS
    assert '10.0.0.7' in verdict['msg']


def test_check_url_allows_public_target():
    verdict = check_url('https://cdn.example.com/a.png', resolver=_public_resolver)
    assert verdict['ok'] is True
    assert verdict['rejected_reason'] == ''
    assert verdict['host'] == 'cdn.example.com'


def test_check_redirect_chain_rejects_public_to_private():
    seen = []

    def _get(url, **kw):
        seen.append(url)
        return _Resp(status=302, location='http://127.0.0.1/inner.png',
                     url=url)

    verdict = check_redirect_chain('https://cdn.example.com/a.png',
                                   fetcher=_get, resolver=_public_resolver)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_REDIRECT
    assert seen == ['https://cdn.example.com/a.png'], '重定向目标不应被请求'
    assert 'response' not in verdict


def test_check_redirect_chain_rechecks_each_hop():
    """合法跳转逐跳复查，最终响应交给调用方；中间响应必须关闭。"""
    responses = [
        _Resp(status=302, location='/second.png', url='https://cdn.example.com/a.png'),
        _Resp(status=302, location='https://img.example.com/final.png',
              url='https://cdn.example.com/second.png'),
        _Resp(status=200, url='https://img.example.com/final.png'),
    ]
    seen = []

    def _get(url, **kw):
        seen.append(url)
        return responses[len(seen) - 1]

    verdict = check_redirect_chain('https://cdn.example.com/a.png',
                                   fetcher=_get, resolver=_public_resolver)
    assert verdict['ok'] is True
    assert verdict['url'] == 'https://img.example.com/final.png'
    assert verdict['response'] is responses[2]
    assert verdict['hops'] == ['https://cdn.example.com/second.png',
                               'https://img.example.com/final.png']
    assert seen == ['https://cdn.example.com/a.png',
                    'https://cdn.example.com/second.png',
                    'https://img.example.com/final.png']
    assert responses[0].closed and responses[1].closed
    assert not responses[2].closed


def test_check_redirect_chain_rejects_history_target():
    """注入的 fetcher 若自动跟了重定向，历史 URL 也要补查。"""
    hist = _Resp(status=302, url='http://127.0.0.1/inner.png')
    final = _Resp(status=200, url='https://cdn.example.com/a.png',
                  history=[hist])

    verdict = check_redirect_chain('https://cdn.example.com/a.png',
                                   fetcher=lambda url, **kw: final,
                                   resolver=_public_resolver)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_BLOCKED_ADDRESS
    assert final.closed is True


def test_check_redirect_chain_caps_hops():
    def _get(url, **kw):
        return _Resp(status=302, location=f'/hop?{url}', url=url)

    verdict = check_redirect_chain('https://cdn.example.com/a.png',
                                   fetcher=_get, resolver=_public_resolver,
                                   max_redirects=3)
    assert verdict['ok'] is False
    assert verdict['rejected_reason'] == REJECT_REDIRECT
