"""media_io：媒体引用落盘。

这批用例守的是**磁盘安全**这条底线。这里的代码写错不会抛异常给人看，
而是把盘写满、把文件写到目标目录外面、或者留下一个看起来正常的残文件。
所以畸形输入的权重要大于正常路径——真出事的都是畸形输入。
"""
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.messaging.media_io import (
    DEFAULT_MAX_BYTES,
    default_media_dir,
    download,
    safe_filename,
    segment_file_id,
)

# ─────────────────────────────────────────────────────────────
# 测试替身：不发真网络请求
# ─────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, chunks, headers=None, status=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status
        self.closed = False

    def iter_content(self, size):
        yield from self._chunks

    def close(self):
        self.closed = True


def _fetcher(resp=None, exc=None):
    def _get(url, **kw):
        if exc is not None:
            raise exc
        return resp
    return _get


# ─────────────────────────────────────────────────────────────
# 文件名净化
# ─────────────────────────────────────────────────────────────

_TRAVERSAL_INPUTS = [
    '../../etc/passwd',
    '..\\..\\windows\\win.ini',
    '/etc/shadow',
    'C:\\Windows\\System32\\drivers\\etc\\hosts',
    'a/b/../../c.png',
    './hidden',
]


@pytest.mark.parametrize('name', _TRAVERSAL_INPUTS)
def test_safe_filename_never_escapes(name):
    """路径分隔符必须被切干净——这是防目录穿越的第一道闸。"""
    got = safe_filename(name)
    assert got
    assert '/' not in got
    assert '\\' not in got
    assert '..' != got


def test_safe_filename_strips_directories():
    assert safe_filename('../../etc/passwd') == 'passwd'
    assert safe_filename('a/b/c.png') == 'c.png'
    assert safe_filename('..\\..\\win.ini') == 'win.ini'


@pytest.mark.parametrize('name,expect', [
    ('CON', '_CON'),
    ('con.txt', '_con.txt'),
    ('NUL', '_NUL'),
    ('com1.log', '_com1.log'),
    ('LPT9', '_LPT9'),
])
def test_safe_filename_windows_reserved(name, expect):
    """Windows 保留设备名带扩展名同样非法，必须改写。"""
    assert safe_filename(name) == expect


def test_safe_filename_control_chars():
    """NUL 会让 open() 直接抛 ValueError，控制字符会污染日志。"""
    assert safe_filename('a\x00b.png') == 'a_b.png'
    assert safe_filename('a\nb.png') == 'a_b.png'
    assert safe_filename('a\x1fb.png') == 'a_b.png'


@pytest.mark.parametrize('name', ['', '   ', '.', '..', '...', None, 123, [], {}])
def test_safe_filename_garbage_falls_back(name):
    """非字符串与纯点号都要落到默认名，不能返回空串给下游拼路径。"""
    got = safe_filename(name)
    assert got == 'media.bin'


def test_safe_filename_keeps_extension():
    assert safe_filename('photo.PNG') == 'photo.PNG'
    # 超长"扩展名"多半是伪装成文件名的路径残留，不该保留
    long_ext = 'a.' + 'x' * 40
    assert '.' not in safe_filename(long_ext) or len(safe_filename(long_ext)) < 140


def test_safe_filename_truncates_long_names():
    got = safe_filename('a' * 500 + '.png')
    assert len(got) <= 130
    assert got.endswith('.png')


# ─────────────────────────────────────────────────────────────
# file_id 提取
# ─────────────────────────────────────────────────────────────

def test_segment_file_id_direct():
    assert segment_file_id({'type': 'image', 'data': {'file_id': 'AgAC'}}) == 'AgAC'


def test_segment_file_id_from_file_info():
    """QQ 官方的标识藏在 file_info.file_uuid 里。"""
    seg = {'type': 'file', 'data': {'file_info': {'file_uuid': 'UU-1'}}}
    assert segment_file_id(seg) == 'UU-1'


@pytest.mark.parametrize('seg', [
    None, 'str', 123, [], {},
    {'type': 'image', 'data': {}},
    {'type': 'image', 'data': None},
    {'type': 'image', 'data': {'file_id': ''}},
    {'type': 'image', 'data': {'file_id': 123}},
    {'type': 'image', 'data': {'file_info': 'not-a-dict'}},
])
def test_segment_file_id_malformed(seg):
    assert segment_file_id(seg) == ''


# ─────────────────────────────────────────────────────────────
# 正常路径
# ─────────────────────────────────────────────────────────────

def test_download_base64(tmp_path):
    payload = base64.b64encode(b'hello').decode()
    res = download({'type': 'image', 'data': {'base64': payload}},
                   dest_dir=str(tmp_path))
    assert res['status'] == 'ok'
    assert res['size'] == 5
    assert open(res['path'], 'rb').read() == b'hello'


def test_download_base64_data_url(tmp_path):
    """data URL 的头部不参与解码。"""
    payload = 'data:image/png;base64,' + base64.b64encode(b'xx').decode()
    res = download({'type': 'image', 'data': {'base64': payload}},
                   dest_dir=str(tmp_path))
    assert res['status'] == 'ok'
    assert res['size'] == 2


def test_download_base64_with_newlines(tmp_path):
    """有些实现会在 base64 里塞换行，必须先去空白再解码。"""
    payload = base64.encodebytes(b'y' * 100).decode()
    res = download({'type': 'image', 'data': {'base64': payload}},
                   dest_dir=str(tmp_path))
    assert res['status'] == 'ok'
    assert res['size'] == 100


def test_download_local_file(tmp_path):
    src = tmp_path / 'src.bin'
    src.write_bytes(b'abc')
    dest = tmp_path / 'out'
    res = download(str(src), dest_dir=str(dest))
    assert res['status'] == 'ok'
    assert res['size'] == 3


def test_download_file_url(tmp_path):
    src = tmp_path / 'src.bin'
    src.write_bytes(b'abcd')
    dest = tmp_path / 'out'
    res = download('file://' + src.as_posix(), dest_dir=str(dest))
    assert res['status'] == 'ok'
    assert res['size'] == 4


def test_download_http_ok(tmp_path):
    resp = _Resp([b'12345'])
    res = download('https://x/a.png', dest_dir=str(tmp_path),
                   fetcher=_fetcher(resp))
    assert res['status'] == 'ok'
    assert res['size'] == 5
    assert resp.closed, '响应必须关闭，否则连接池会泄漏'


def test_download_same_name_does_not_overwrite(tmp_path):
    payload = base64.b64encode(b'1').decode()
    seg = {'type': 'image', 'data': {'base64': payload, 'name': 'a.png'}}
    first = download(seg, dest_dir=str(tmp_path))
    second = download(seg, dest_dir=str(tmp_path))
    assert first['path'] != second['path']
    assert os.path.basename(second['path']) == 'a-1.png'


# ─────────────────────────────────────────────────────────────
# 畸形输入
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('seg', [None, '', 123, [], {}, {'type': 'image'},
                                 {'type': 'image', 'data': {}},
                                 {'type': 'image', 'data': {'url': ''}},
                                 {'type': 'image', 'data': {'base64': ''}}])
def test_download_no_reference(tmp_path, seg):
    res = download(seg, dest_dir=str(tmp_path))
    assert res['status'] == 'failed'
    assert res['path'] == ''
    assert '引用' in res['msg'] or '为空' in res['msg']


def test_download_invalid_base64(tmp_path):
    res = download('base64://!!!!not-base64', dest_dir=str(tmp_path))
    assert res['status'] == 'failed'
    assert not os.listdir(tmp_path), '失败后不能留下文件'


def test_download_truncated_base64(tmp_path):
    """少一个字符的 base64 是畸形输入，必须报错而不是解出半截内容。"""
    payload = base64.b64encode(b'0123456789').decode()
    res = download({'type': 'image', 'data': {'base64': payload[:-1]}},
                   dest_dir=str(tmp_path))
    assert res['status'] == 'failed'
    assert not os.listdir(tmp_path)


def test_download_base64_rejected_before_decode(tmp_path):
    """超限要在解码前拦——先 decode 再判长度，内存峰值会翻倍。"""
    big = 'A' * (200 * 1024)
    res = download({'type': 'image', 'data': {'base64': big}},
                   dest_dir=str(tmp_path), max_bytes=1024)
    assert res['status'] == 'failed'
    assert '超过上限' in res['msg']
    assert not os.listdir(tmp_path)


def test_download_local_over_limit(tmp_path):
    src = tmp_path / 'big.bin'
    src.write_bytes(b'x' * 4096)
    dest = tmp_path / 'out'
    res = download(str(src), dest_dir=str(dest), max_bytes=1024)
    assert res['status'] == 'failed'
    assert '超过上限' in res['msg']
    assert not os.listdir(dest)


def test_download_local_missing(tmp_path):
    res = download(str(tmp_path / 'nope.bin'), dest_dir=str(tmp_path))
    assert res['status'] == 'failed'
    assert '不存在' in res['msg']


def test_download_http_declared_too_large(tmp_path):
    resp = _Resp([b'x' * 100], headers={'Content-Length': '999999'})
    res = download('https://x/a.png', dest_dir=str(tmp_path),
                   max_bytes=1024, fetcher=_fetcher(resp))
    assert res['status'] == 'failed'
    assert '超过上限' in res['msg']
    assert not os.listdir(tmp_path), '预检拒绝后不能落任何文件'


def test_download_http_stream_over_limit(tmp_path):
    """服务端不给 Content-Length 时，靠边写边累计拦住。"""
    resp = _Resp([b'x' * 1024] * 10)   # 共 10KB，无 Content-Length
    res = download('https://x/a.png', dest_dir=str(tmp_path),
                   max_bytes=4096, fetcher=_fetcher(resp))
    assert res['status'] == 'failed'
    assert '超过上限' in res['msg']
    # 半成品必须删掉，否则后续读到的是一个截断文件
    assert not os.listdir(tmp_path)


def test_download_http_non_2xx(tmp_path):
    resp = _Resp([b'{"err": 1}'], status=404)
    res = download('https://x/a.png', dest_dir=str(tmp_path),
                   fetcher=_fetcher(resp))
    assert res['status'] == 'failed'
    assert '404' in res['msg']


def test_download_http_empty_body(tmp_path):
    resp = _Resp([])
    res = download('https://x/a.png', dest_dir=str(tmp_path),
                   fetcher=_fetcher(resp))
    assert res['status'] == 'failed'
    assert not os.listdir(tmp_path)


def test_download_http_connection_error(tmp_path):
    res = download('https://x/a.png', dest_dir=str(tmp_path),
                   fetcher=_fetcher(exc=OSError('boom')))
    assert res['status'] == 'failed'
    assert '请求失败' in res['msg']


def test_download_path_traversal_name(tmp_path):
    """发送方完全控制 data['name']，必须只能落在目标目录内。"""
    payload = base64.b64encode(b'x').decode()
    seg = {'type': 'image',
           'data': {'base64': payload, 'name': '../../evil.sh'}}
    res = download(seg, dest_dir=str(tmp_path))
    assert res['status'] == 'ok'
    assert os.path.dirname(os.path.abspath(res['path'])) == \
        os.path.abspath(str(tmp_path))
    assert os.path.basename(res['path']) == 'evil.sh'


def test_download_malformed_name_field(tmp_path):
    """data['name'] 是 dict / None 时退回默认名，不能抛异常。"""
    payload = base64.b64encode(b'x').decode()
    names = []
    for i, bad in enumerate(({'a': 1}, None, 123, ['x'])):
        seg = {'type': 'image', 'data': {'base64': payload, 'name': bad}}
        res = download(seg, dest_dir=str(tmp_path), filename=None)
        assert res['status'] == 'ok'
        # 同名不覆盖会加 -1/-2 后缀，所以只看主干是 media
        assert os.path.basename(res['path']).startswith('media')
        names.append(res['path'])
    assert len(set(names)) == len(names), '四次落盘不能互相覆盖'


@pytest.mark.parametrize('limit', [0, -1, -1024])
def test_download_invalid_max_bytes(tmp_path, limit):
    payload = base64.b64encode(b'x').decode()
    res = download({'type': 'image', 'data': {'base64': payload}},
                   dest_dir=str(tmp_path), max_bytes=limit)
    assert res['status'] == 'failed'
    assert 'must' in res['msg'] or '正数' in res['msg']


def test_download_dest_dir_is_a_file(tmp_path):
    blocker = tmp_path / 'iamfile'
    blocker.write_bytes(b'')
    res = download('base64://' + base64.b64encode(b'x').decode(),
                   dest_dir=str(blocker))
    assert res['status'] == 'failed'
    assert '目录不可用' in res['msg']


def test_download_file_id_needs_adapter(tmp_path):
    res = download({'type': 'image', 'data': {'file_id': 'AgAC'}},
                   dest_dir=str(tmp_path))
    assert res['status'] == 'needs_adapter'
    assert res['ref'] == 'file_id:AgAC'


def test_download_file_info_needs_adapter(tmp_path):
    seg = {'type': 'file', 'data': {'file_info': {'file_uuid': 'UU'}}}
    res = download(seg, dest_dir=str(tmp_path))
    assert res['status'] == 'needs_adapter'
    assert res['ref'] == 'file_id:UU'


def test_download_no_part_left_after_failure(tmp_path):
    """所有失败分支都不得留下 .part 半成品。"""
    download('base64://!!!!', dest_dir=str(tmp_path))
    download(str(tmp_path / 'missing.bin'), dest_dir=str(tmp_path))
    resp = _Resp([b'x' * 9000])
    download('https://x/a', dest_dir=str(tmp_path), max_bytes=100,
             fetcher=_fetcher(resp))
    leftovers = [n for n in os.listdir(tmp_path) if '.part' in n]
    assert leftovers == []


def test_download_empty_segment_list_rejected(tmp_path):
    """段是 list 而不是 dict 时，不能把 repr 当路径去读。"""
    res = download([{'type': 'image'}], dest_dir=str(tmp_path))
    assert res['status'] == 'failed'


def test_default_media_dir_under_state_dir():
    from framework.paths import state_dir
    assert default_media_dir() == os.path.join(state_dir(), 'media')


def test_default_max_bytes_is_positive():
    assert DEFAULT_MAX_BYTES > 0
