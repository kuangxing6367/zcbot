"""出站路径的行为钉桩：长文本切分、媒体大小上限、parse_mode 降级。

这些用例针对的都是「不会报错、但结果不对」的地方：切分丢字符、
不设上限把大文件读进内存、配置项在某条路径上静默失效。
全部用假 transport，不需要真实凭证。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, sub):
    path = ROOT / 'core_plugins' / sub / 'main.py'
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dc = _load('zcbot_test_discord_out', 'discord')
tg = _load('zcbot_test_telegram_out', 'telegram')
kk = _load('zcbot_test_kook_out', 'kook')
mx = _load('zcbot_test_matrix_out', 'matrix')
qq = _load('zcbot_test_qq_out', 'qq_official')


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('ZCBOT_HOME', str(tmp_path))
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------- 文本切分
#
# 两个接入端各有一份切分实现（discord 2000 / telegram 4096）。判定标准一样：
# 拼回去必须与原文完全相同（不丢字），且每段按 UTF-16 计都不超限。

_SPLIT_CASES = [
    ('ascii', 'a' * 9999),
    ('cjk', '中' * 5000),
    ('emoji', '\U0001F600' * 3000),      # 非 BMP，每个占 2 个 UTF-16 码元
    ('newlines', ('一行内容\n' * 800)),
    ('no-break', 'c' * 4097),
    ('empty', ''),
]


@pytest.mark.parametrize('name,text', _SPLIT_CASES)
def test_discord_split_never_loses_or_overflows(name, text):
    chunks = dc._split_content(text)
    assert ''.join(chunks) == text, f'{name}: 切分丢了字符'
    assert all(dc._utf16_len(c) <= dc._MAX_CONTENT_LEN for c in chunks), \
        f'{name}: 有分段超过 2000'


@pytest.mark.parametrize('name,text', _SPLIT_CASES)
def test_telegram_split_never_loses_or_overflows(name, text):
    chunks = tg._split_text(text, tg._MAX_TEXT_LEN)
    assert ''.join(chunks) == text, f'{name}: 切分丢了字符'
    assert all(tg._utf16_len(c) <= tg._MAX_TEXT_LEN for c in chunks), \
        f'{name}: 有分段超过 4096'


def test_utf16_len_counts_astral_as_two():
    """emoji 按 UTF-16 计是 2，用 len() 会低估一半，切分就会超限。"""
    assert dc._utf16_len('a') == 1
    assert dc._utf16_len('中') == 1
    assert dc._utf16_len('\U0001F600') == 2
    assert tg._utf16_len('\U0001F600') == 2


def test_discord_split_prefers_newline_boundary():
    text = 'x' * 1500 + '\n' + 'y' * 600
    head = dc._split_content(text)[0]
    assert head.endswith('\n'), '断点应落在换行处，而不是把一行劈开'


# ---------------------------------------------------------------- 媒体上限

def test_kook_base64_rejects_oversized_before_decoding():
    """解码前先按 base64 长度估算：解完才发现超限，内存已经吃满了。"""
    a = kk.KookAdapter(framework=None, token='t', media_max_bytes=1024)
    seg = {'data': {'name': 'big.bin'}}
    payload = 'A' * (4096 * 4 // 3 + 16)
    raw, name, _mime = a._read_media_ref(f'base64://{payload}', seg)
    assert raw == b'', '超限必须返回空，交给上层报错'
    assert name == 'big.bin'


def test_kook_local_file_over_limit_is_refused(tmp_path):
    a = kk.KookAdapter(framework=None, token='t', media_max_bytes=16)
    p = tmp_path / 'big.bin'
    p.write_bytes(b'x' * 64)
    raw, _name, _mime = a._read_media_ref(str(p), {'data': {}})
    assert raw == b''


def test_kook_local_file_within_limit_is_read(tmp_path):
    a = kk.KookAdapter(framework=None, token='t', media_max_bytes=1024)
    p = tmp_path / 'ok.bin'
    p.write_bytes(b'hello')
    raw, name, _mime = a._read_media_ref(str(p), {'data': {}})
    assert raw == b'hello'
    assert name == 'ok.bin'


def test_matrix_base64_rejects_oversized(tmp_path):
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         media_max_bytes=1024)
    payload = 'A' * (4096 * 4 // 3 + 16)
    raw, _name, _mime = a._read_media_ref(f'base64://{payload}', {'data': {}})
    assert raw == b''


def test_matrix_local_file_over_limit_is_refused(tmp_path):
    # 构造函数把 media_max_bytes 下限钳到 1024，所以这里用超过 1KB 的文件
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         media_max_bytes=1024)
    p = tmp_path / 'big.bin'
    p.write_bytes(b'x' * 4096)
    raw, _name, _mime = a._read_media_ref(str(p), {'data': {}})
    assert raw == b''


# ---------------------------------------------------------------- parse_mode

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.content = b'{}' if payload else b''
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeHttp:
    """按顺序返回预设响应，并记录每次请求体。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def post(self, url, **kw):
        body = kw.get('json') or kw.get('data') or {}
        self.calls.append({'url': url, 'body': dict(body)})
        return _FakeResp(self._payloads.pop(0) if self._payloads else {'ok': True})


def _tg_media_adapter(home, payloads, parse_mode='HTML'):
    a = tg.TelegramAdapter(framework=None, token='1:A', bot_name='t',
                           parse_mode=parse_mode)
    a._http = _FakeHttp(payloads)
    return a


def _photo_op(tmp_path, caption):
    p = tmp_path / 'p.png'
    p.write_bytes(b'\x89PNG')
    return {'method': 'sendPhoto', 'field': 'photo', 'type': 'image',
            'caption': caption, 'data': {'path': str(p), 'name': 'p.png'}}


def test_caption_uses_parse_mode(home, tmp_path):
    """开 HTML 却只对正文生效，是让配置项在媒体上静默失效。"""
    a = _tg_media_adapter(home, [{'ok': True, 'result': {'message_id': 1}}])
    a._send_media('c1', _photo_op(tmp_path, '<b>hi</b>'))
    assert a._http.calls[0]['body']['parse_mode'] == 'HTML'


def test_caption_parse_failure_degrades_to_plain_text(home, tmp_path):
    """实体解析失败是整条 400（媒体一起发不出去），必须降级重发一次。"""
    a = _tg_media_adapter(home, [
        {'ok': False, 'error_code': 400,
         'description': "Bad Request: can't parse entities: unsupported"},
        {'ok': True, 'result': {'message_id': 2}},
    ])
    res = a._send_media('c1', _photo_op(tmp_path, 'a_b_c'))
    assert res['status'] == 'ok', res
    assert len(a._http.calls) == 2, '降级应该重发一次'
    assert 'parse_mode' not in a._http.calls[1]['body'], '重发不能还带 parse_mode'
    assert a._http.calls[1]['body']['caption'] == 'a_b_c', '重发必须保留原文'


def test_text_parse_failure_degrades_to_plain_text(home):
    a = _tg_media_adapter(home, [
        {'ok': False, 'error_code': 400,
         'description': "Bad Request: can't parse entities: unmatched"},
        {'ok': True, 'result': {'message_id': 3}},
    ])
    res = a._send_text_once('c1', 'x_y')
    assert res['status'] == 'ok', res
    assert len(a._http.calls) == 2


def test_caption_overflow_moves_to_followup_message(home, tmp_path):
    """caption 上限 1024，超了整条 400；正文改由随后一条文本承载。"""
    a = _tg_media_adapter(home, [
        {'ok': True, 'result': {'message_id': 4}},
        {'ok': True, 'result': {'message_id': 5}},
    ], parse_mode='')
    long_caption = '字' * 1500
    a._send_media('c1', _photo_op(tmp_path, long_caption))
    assert 'caption' not in a._http.calls[0]['body'], '超长 caption 不能硬发'
    assert a._http.calls[1]['body']['text'] == long_caption


# ---------------------------------------------------------------- QQ 图文卡片

def _card_err(content):
    return qq._check_tuwen_card(content)


def test_tuwen_card_accepts_complete_content():
    assert _card_err({'title': '标题', 'description': '描述',
                      'url': 'https://e.com/a',
                      'pic_url': 'https://e.com/a.png'}) == ''


@pytest.mark.parametrize('missing', ['title', 'description', 'url', 'pic_url'])
def test_tuwen_card_names_the_missing_field(missing):
    """官方只回 304061 不说是哪个字段；本地报错必须指名道姓。"""
    content = {'title': '标题', 'description': '描述',
               'url': 'https://e.com/a', 'pic_url': 'https://e.com/a.png'}
    content.pop(missing)
    err = _card_err(content)
    assert missing in err, err


def test_tuwen_card_reports_length_and_limit():
    err = _card_err({'title': '标' * 51, 'description': '描述',
                     'url': 'https://e.com/a', 'pic_url': 'https://e.com/a.png'})
    assert '50' in err and '51' in err, err


@pytest.mark.parametrize('key', ['url', 'pic_url'])
def test_tuwen_card_requires_https(key):
    content = {'title': '标题', 'description': '描述',
               'url': 'https://e.com/a', 'pic_url': 'https://e.com/a.png'}
    content[key] = 'http://e.com/a'
    assert 'https' in _card_err(content)


def test_tuwen_card_rejects_non_object_content():
    assert _card_err('not-a-dict')


def test_qq_human_size_never_prints_zero_mb():
    """回归：曾用 n // 1024 // 1024，上限小于 1MB 时报「超过 0MB 硬限制」。"""
    assert '0MB' not in qq._human_size(32)
    assert qq._human_size(32) == '32 字节'


# ---------------------------------------------------------------- 分发

def test_kook_send_msg_splits_text_and_media(home):
    """KOOK 一条消息只能有一种 type：文本先发，媒体各自成条。"""
    import asyncio
    a = kk.KookAdapter(framework=None, token='t')
    sent = []

    def fake_api(method, path, **kw):
        sent.append(kw.get('json_body'))
        return {'status': 'ok', 'retcode': 0}

    a._api = fake_api
    a._read_media_ref = lambda ref, seg: (b'img', 'a.png', 'image/png')
    a.upload_asset = lambda raw, name, mime: {
        'status': 'ok', 'data': {'url': 'https://cdn/a.png'}}
    asyncio.run(a.call_api('send_msg', group_id='c1', message=[
        {'type': 'text', 'data': {'text': '文字'}},
        {'type': 'image', 'data': {'file': 'a.png'}},
    ]))
    types = [b['type'] for b in sent]
    assert kk._MSG_TEXT == 1 and kk._MSG_KMARKDOWN == 9
    assert types[0] == kk._MSG_KMARKDOWN
    assert types[1] == kk._MSG_IMAGE
