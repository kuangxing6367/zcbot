"""适配器错误响应处理。

要钉住的是「平台怎么说失败」这件事在各端是否一致：

  - 平台用 HTTP 200 + 业务码报错（QQ 官方），只看 status_code 会把失败当成功；
  - 平台用 429 + retry_after 限流（Telegram / Discord / Matrix / KOOK），
    自己拍一个退避间隔去重试，会被继续限，越试越久；
  - 平台用关闭码表示「配置错了」（Discord 4013/4014），继续重连会烧光配额；
  - 返回给插件的 retcode 必须是框架约定值，不能直接透 HTTP 状态码。

这些都是「日志里看不出异常、消息就是不回」的典型成因。
"""
import importlib.util
import json
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


tg = _load('zcbot_test_tg', 'telegram')
dc = _load('zcbot_test_dc', 'discord')
qq = _load('zcbot_test_qq', 'qq_official')
kk = _load('zcbot_test_kk', 'kook')
mx = _load('zcbot_test_mx', 'matrix')


class _Resp:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body, ensure_ascii=False).encode()
        self.text = text if text is not None else json.dumps(
            self._body, ensure_ascii=False)
        self.headers = headers or {}

    def json(self):
        return self._body


class _FakeHttp:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(('POST', url, kw))
        return self._resp

    def get(self, url, **kw):
        self.calls.append(('GET', url, kw))
        return self._resp

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._resp

    def close(self):
        pass


# ── Telegram ──────────────────────────────────────────────────

def test_telegram_429_carries_official_retry_after():
    a = tg.TelegramAdapter(framework=None, token='1:A', bot_name='t')
    a._http = _FakeHttp(_Resp(429, {
        'ok': False, 'error_code': 429,
        'description': 'Too Many Requests: retry after 27',
        'parameters': {'retry_after': 27},
    }))
    with pytest.raises(tg.TelegramApiError) as ei:
        a._get_updates()
    assert ei.value.error_code == 429
    assert ei.value.retry_after == 27, '必须读 parameters.retry_after'


def test_telegram_429_falls_back_to_description():
    a = tg.TelegramAdapter(framework=None, token='1:A', bot_name='t')
    a._http = _FakeHttp(_Resp(429, {
        'ok': False, 'error_code': 429,
        'description': 'Too Many Requests: retry after 9',
    }))
    with pytest.raises(tg.TelegramApiError) as ei:
        a._get_updates()
    assert ei.value.retry_after == 9


def test_telegram_outbound_uses_conventional_retcode():
    a = tg.TelegramAdapter(framework=None, token='1:A', bot_name='t')
    a._http = _FakeHttp(_Resp(429, {
        'ok': False, 'error_code': 429,
        'description': 'Too Many Requests: retry after 5',
        'parameters': {'retry_after': 5},
    }))
    res = a._send_message(123, 'hi')
    assert res['retcode'] == -4, '限流必须用 -4，不能用 HTTP 429'
    assert res['throttled'] is True
    assert res['retry_after'] == 5


def test_telegram_409_is_identifiable():
    a = tg.TelegramAdapter(framework=None, token='1:A', bot_name='t')
    a._http = _FakeHttp(_Resp(409, {
        'ok': False, 'error_code': 409,
        'description': 'Conflict: terminated by other getUpdates request',
    }))
    with pytest.raises(tg.TelegramApiError) as ei:
        a._get_updates()
    assert ei.value.error_code == 409


def test_telegram_plain_error_keeps_description():
    a = tg.TelegramAdapter(framework=None, token='1:A', bot_name='t')
    a._http = _FakeHttp(_Resp(400, {
        'ok': False, 'error_code': 400,
        'description': 'Bad Request: chat not found',
    }))
    res = a._send_message(999, 'hi')
    assert res['retcode'] == -1
    assert 'chat not found' in res['msg']


# ── Discord ───────────────────────────────────────────────────

def test_discord_rest_429_uses_retry_after():
    a = dc.DiscordAdapter(framework=None, token='x', bot_name='d')
    a._http = _FakeHttp(_Resp(429, {
        'message': 'You are being rate limited.',
        'retry_after': 4.7, 'global': False,
    }))
    res = a._rest_post('/channels/1/messages', json_body={'content': 'x'})
    assert res['retcode'] == -4
    assert res['throttled'] is True
    assert res['retry_after'] == 4


def test_discord_rest_forbidden_is_explained():
    a = dc.DiscordAdapter(framework=None, token='x', bot_name='d')
    a._http = _FakeHttp(_Resp(403, {'code': 50013, 'message': 'Missing Permissions'}))
    res = a._rest_post('/channels/1/messages', json_body={'content': 'x'})
    assert res['retcode'] == -1
    assert '403' in res['msg']


@pytest.mark.parametrize('code', [4004, 4010, 4011, 4012, 4013, 4014])
def test_discord_fatal_close_codes_stop_reconnect(code):
    a = dc.DiscordAdapter(framework=None, token='x', bot_name='d')
    a._note_close_code(code)
    assert a._fatal_reason, f'{code} 必须标记为致命，否则会无限重连'


def test_discord_normal_close_resets_session():
    a = dc.DiscordAdapter(framework=None, token='x', bot_name='d')
    a._session_id = 'sess'
    a._seq = 42
    a._resume_gateway = 'wss://resume'
    a._note_close_code(1000)
    assert a._session_id == '', '1000/1001 后 session 作废，必须重新 IDENTIFY'
    assert a._seq is None
    assert not a._fatal_reason


def test_discord_missing_message_content_intent_is_warned(caplog):
    import logging
    sink = []
    lg = logging.getLogger('zcbot')
    h = logging.Handler()
    h.emit = lambda r: sink.append(r.getMessage())
    lg.addHandler(h)
    try:
        a = dc.DiscordAdapter(framework=None, token='x', bot_name='d',
                              intents=(1 << 0) | (1 << 9))
        a._warn_missing_intents()
    finally:
        lg.removeHandler(h)
    assert any('MESSAGE_CONTENT' in m for m in sink), \
        '缺特权 intent 会导致 content 全空，必须提前告警'


# ── QQ 官方 ───────────────────────────────────────────────────

def test_qq_business_error_without_message_still_fails():
    """官方说 message 内容可能随时调整，判据只能是业务码。"""
    a = qq.QQOfficialAdapter(framework=None, app_id='1', app_secret='2')
    a._access_token = 'tok'
    a._token_expire_at = 9e18
    body = {'code': 11244, 'err_msg': 'token expired'}
    a._http = _FakeHttp(_Resp(200, body))
    with pytest.raises(Exception):
        a._api_request('POST', '/v2/groups/g/messages', json_body={})


def test_qq_success_body_returns_data():
    a = qq.QQOfficialAdapter(framework=None, app_id='1', app_secret='2')
    a._access_token = 'tok'
    a._token_expire_at = 9e18
    a._http = _FakeHttp(_Resp(200, {'id': 'm1', 'timestamp': 123}))
    data = a._api_request('POST', '/v2/groups/g/messages', json_body={})
    assert data.get('id') == 'm1'


def test_qq_default_api_base_is_unified_domain():
    assert qq._API_BASE == 'https://api.bot.qq.com'
    a = qq.QQOfficialAdapter(framework=None, app_id='1', app_secret='2')
    assert a.api_base == 'https://api.bot.qq.com'
    assert a.api_base_fallback == '', \
        '默认单域名，不应再为已下线的 sgroup 白跑一次'


# ── KOOK ──────────────────────────────────────────────────────

def test_kook_401_is_explained():
    a = kk.KookAdapter(framework=None, token='x')
    a._http = _FakeHttp(_Resp(401, {'code': 40101, 'message': 'token invalid'}))
    res = a._api('POST', '/api/v3/message/create', json_body={})
    assert res['retcode'] == -1
    assert '401' in res['msg']


def test_kook_429_reads_retry_after_header():
    a = kk.KookAdapter(framework=None, token='x')
    a._http = _FakeHttp(_Resp(429, {'code': 429}, headers={'Retry-After': '12'}))
    res = a._api('POST', '/api/v3/message/create', json_body={})
    assert res['retcode'] == -4
    assert res['retry_after'] == 12


def test_kook_business_code_error():
    a = kk.KookAdapter(framework=None, token='x')
    a._http = _FakeHttp(_Resp(200, {'code': 40000, 'message': '缺少参数'}))
    res = a._api('POST', '/api/v3/message/create', json_body={})
    assert res['retcode'] == -1
    assert res['error_code'] == 40000


def test_kook_success_returns_data():
    a = kk.KookAdapter(framework=None, token='x')
    a._http = _FakeHttp(_Resp(200, {'code': 0, 'data': {'msg_id': 'm1'}}))
    res = a._api('POST', '/api/v3/message/create', json_body={})
    assert res['status'] == 'ok'
    assert res['data']['msg_id'] == 'm1'


# ── Matrix ────────────────────────────────────────────────────

def test_matrix_limit_exceeded_maps_to_throttle():
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         access_token='t', user_id='@b:m.example')
    a._http = _FakeHttp(_Resp(429, {
        'errcode': 'M_LIMIT_EXCEEDED', 'error': 'Too many requests',
        'retry_after_ms': 4000,
    }))
    res = a.send_message('!r:m.example', 'hi')
    assert res['retcode'] == -4
    assert res['retry_after'] == 4


def test_matrix_unknown_token_is_fatal():
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         access_token='t', user_id='@b:m.example')
    a._http = _FakeHttp(_Resp(401, {
        'errcode': 'M_UNKNOWN_TOKEN', 'error': 'Invalid access token'}))
    with pytest.raises(mx.MatrixApiError) as ei:
        a._sync()
    assert ei.value.fatal is True


def test_matrix_send_uses_single_txn_id():
    """幂等键：同一次发送的重试必须复用一个 txnId，换掉就会真的重发。"""
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         access_token='t', user_id='@b:m.example')
    seen = []

    def fake_request(method, path, **kw):
        seen.append(path)
        return {'event_id': '$e1'}

    a._request = fake_request
    a.send_message('!r:m.example', 'hi')
    assert len(seen) == 1
    assert '/send/m.room.message/' in seen[0]
    txn = seen[0].rsplit('/', 1)[-1]
    assert txn.startswith('zcbot'), txn
