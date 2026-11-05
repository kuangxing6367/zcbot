# -*- coding: utf-8 -*-
"""接入端出站错误码契约回归测试。

契约来源（本测试只读，不修改）：
  - docs/指南/对接IM平台.md 的 retcode 表（含 -10「该端不支持此动作」）
  - docs/进阶/可靠性与限流.md 的「只重试确定没发出去的失败」
  - framework/messaging/outbound_guard.py 的 RETRYABLE_RETCODES

统一映射：
  参数缺失 / 前置条件不满足 / 平台业务错误 / 结果不确定 → -1（不可重试）
  真正超时                                          → -2（可能已送达，绝不重试）
  网络/HTTP 异常、连接断开、请求失败                → -3（可重试）
  429 / 平台限流                                    → -4 + throttled: True
  该端不支持此动作                                  → -10（文档定义，保留）

所有路径由 __file__ / tmp_path 推导，不依赖 cwd，不写死时区；
适配器网络调用一律走内存桩，不发真实请求；状态目录重定向到 tmp_path。
"""
import asyncio
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


qq = _load('zcbot_retcode_qq', 'qq_official')
tg = _load('zcbot_retcode_tg', 'telegram')
dc = _load('zcbot_retcode_dc', 'discord')
kk = _load('zcbot_retcode_kk', 'kook')
mx = _load('zcbot_retcode_mx', 'matrix')
ws = _load('zcbot_retcode_ws', 'ws_client')
ob = _load('zcbot_retcode_ob', 'onebot_adapter')
ra = _load('zcbot_retcode_ra', 'rust_accel')


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    """接入端状态文件一律落到临时目录，绝不写进真实项目 data/。"""
    monkeypatch.setenv('ZCBOT_HOME', str(tmp_path))
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)


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
    """纯内存 HTTP 桩：返回预设响应，或抛出预设异常（模拟连接断开）。"""

    def __init__(self, resp=None, error=None):
        self.resp = resp
        self.error = error
        self.calls = []

    def _once(self, method, url):
        self.calls.append((method, url))
        if self.error is not None:
            raise self.error
        return self.resp

    def post(self, url, **kw):
        return self._once('POST', url)

    def get(self, url, **kw):
        return self._once('GET', url)

    def request(self, method, url, **kw):
        return self._once(method, url)

    def close(self):
        pass


def _qq():
    a = qq.QQOfficialAdapter(framework=None, app_id='1', app_secret='2')
    a._access_token = 'tok'
    a._token_expire_at = 9e18
    return a


def _tg():
    return tg.TelegramAdapter(framework=None, token='1:A', bot_name='t')


def _dc():
    return dc.DiscordAdapter(framework=None, token='x', bot_name='d')


def _kk():
    return kk.KookAdapter(framework=None, token='x')


def _mx():
    return mx.MatrixAdapter(
        framework=None, homeserver='https://m.example',
        access_token='t', user_id='@b:m.example')


def _ws():
    return ws.WsClientAdapter(framework=None, url='ws://example.invalid')


_CALL_API_ADAPTERS = [
    ('qq_official', _qq),
    ('telegram', _tg),
    ('kook', _kk),
    ('matrix', _mx),
    ('discord', _dc),
    ('ws_client', _ws),
]
_CALL_API_IDS = [name for name, _ in _CALL_API_ADAPTERS]


# ── 0. 契约文档与闸门（只读校验，本轮不改框架） ─────────────────

def test_retcode_contract_is_documented():
    doc = (ROOT / 'docs' / '指南' / '对接IM平台.md').read_text(encoding='utf-8')
    for code in ('0', '-1', '-2', '-3', '-4', '-10'):
        assert f'| `{code}` |' in doc, f'契约表缺少 {code}'
    assert '该端不支持此动作' in doc, '-10 的语义必须有文档定义'


def test_outbound_guard_retry_contract_is_untouched():
    src = (ROOT / 'framework' / 'messaging' / 'outbound_guard.py').read_text(
        encoding='utf-8')
    assert 'RETRYABLE_RETCODES = frozenset({-3})' in src
    assert 'except (TimeoutError, asyncio.TimeoutError)' in src


# ── 1. 参数缺失 / 前置条件不满足 → -1 ───────────────────────────

QQ_PARAM_CASES = [
    ('send_msg', {}),                              # 缺 group_id/user_id
    ('delete_msg', {'group_id': 'G'}),             # 缺 message_id
    ('delete_msg', {'message_id': 'M'}),           # 缺 group_id/user_id
    ('generate_url_link', {}),                     # 缺 callback_data
    ('set_global_menu', {'menu': 'not-a-dict'}),   # menu 类型不对
    ('get_panel', {}),                            # 缺 panel_id
    ('create_panel', {'panel': {}}),               # scope 非法
    ('update_panel', {'panel_id': 'P'}),           # 缺 panel
    ('delete_panel', {}),                          # 缺 panel_id
    ('update_panel_targets', {'panel_id': 'P'}),   # op 非法
    ('get_group_member', {'group_id': 'G'}),       # 缺 member_id
    ('operate_group_member_blacklist', {'group_id': 'G'}),  # op 非法
    ('review_group_join_request', {'group_id': 'G'}),
    ('set_group_member_mute', {'group_id': 'G'}),  # 缺 members 列表
    ('get_group_info', {}),                        # _group_request 缺 group_id
    ('send_stream', {}),                           # 缺 user_id
]


@pytest.mark.parametrize('action,params', QQ_PARAM_CASES)
def test_qq_official_param_errors_are_minus1(action, params):
    res = asyncio.run(_qq().call_api(action, **params))
    assert res['status'] == 'failed'
    assert res['retcode'] == -1, (action, res)


def test_telegram_param_errors_are_minus1():
    a = _tg()
    assert asyncio.run(a.call_api('send_msg'))['retcode'] == -1
    missing = a._send_media(
        123, {'method': 'sendDocument', 'field': 'document', 'data': {}})
    assert missing['retcode'] == -1, '缺文件引用是参数问题，不是超时'
    # 坏的 base64 属于数据不合法：重试毫无意义，不能标成可重试的 -3
    a._http = _FakeHttp(_Resp(200, {'ok': True, 'result': {}}))
    bad_b64 = a._send_media(
        123, {'method': 'sendDocument', 'field': 'document', 'type': 'file',
              'data': {'base64': 'AAAAA'}})
    assert bad_b64['retcode'] == -1


def test_kook_param_errors_are_minus1():
    a = _kk()
    assert asyncio.run(a.call_api('send_msg'))['retcode'] == -1
    assert asyncio.run(a.call_api('delete_msg', group_id='C'))['retcode'] == -1


def test_matrix_param_error_is_minus1():
    assert asyncio.run(_mx().call_api('send_msg'))['retcode'] == -1


def test_discord_param_error_is_minus1():
    assert asyncio.run(_dc().call_api('send_msg'))['retcode'] == -1


def test_onebot_not_connected_is_minus1():
    """文档明确 -1 含「接入端未连接」，且未连接重试无意义 → 保留 -1。"""
    conn = ob.BotConnection('x')
    assert asyncio.run(conn.acall('send_msg'))['retcode'] == -1


def test_rust_accel_not_running_is_minus1():
    a = ra.RustAccelService(framework=None)
    assert asyncio.run(a.acall('send_msg'))['retcode'] == -1


# ── 2. 网络 / HTTP 异常、连接断开、请求失败 → -3 ─────────────────

def test_telegram_http_exception_is_minus3():
    a = _tg()
    a._http = _FakeHttp(error=RuntimeError('connection reset'))
    res = a._send_media(
        123, {'method': 'sendDocument', 'field': 'document', 'type': 'file',
              'data': {'file': 'http://example.invalid/a.bin'}})
    assert res['retcode'] == -3, 'HTTP 异常不是限流，可被闸门重试'


def test_kook_request_exception_is_minus3():
    a = _kk()
    a._http = _FakeHttp(error=RuntimeError('connection reset'))
    res = a._api('POST', '/api/v3/message/create', json_body={})
    assert res['retcode'] == -3


def test_matrix_network_error_is_minus3():
    a = _mx()
    a._http = _FakeHttp(error=RuntimeError('connection reset'))
    res = a.send_message('!r:m.example', 'hi')
    assert res['retcode'] == -3


def test_onebot_send_failure_is_minus3():
    class _WsServer:
        async def asend(self, name, payload):
            return False

    conn = ob.BotConnection('x')
    conn._ws = object()
    conn._ws_server = _WsServer()
    assert asyncio.run(conn.acall('send_msg'))['retcode'] == -3


@pytest.mark.parametrize('res_value,expected', [('failed', -3), ('queued', -1)])
def test_ws_client_send_result_mapping(res_value, expected):
    """连接断开/发送失败 → -3；已入队待重连 → -1（不可重试，防重复投递）。"""
    async def run():
        a = _ws()

        async def fake_send(obj):
            return res_value

        a._send_json = fake_send
        return await a.call_api('send_msg', message='hi')

    res = asyncio.run(run())
    assert res['retcode'] == expected, res
    assert res['queued'] is (res_value == 'queued')


def test_rust_accel_writer_failure_is_minus3():
    class _Proc:
        def poll(self):
            return None

    class _Writer:
        def write(self, data):
            raise RuntimeError('broken pipe')

    async def run():
        a = ra.RustAccelService(framework=None)
        a.proc = _Proc()
        a._writer = _Writer()
        a._loop = asyncio.get_running_loop()
        return await a.acall('send_msg')

    assert asyncio.run(run())['retcode'] == -3


def test_rust_accel_stop_pending_is_minus3():
    """stop() 清理 pending 是通道断开，不是限流 → -3。"""
    async def run():
        a = ra.RustAccelService(framework=None)
        fut = asyncio.get_running_loop().create_future()
        a._pending = {'1': fut}
        await a.stop()
        return fut.result()

    assert asyncio.run(run())['retcode'] == -3


# ── 3. 429 / 平台限流 → -4 + throttled: True ────────────────────

def test_telegram_429_is_minus4_throttled():
    a = _tg()
    a._http = _FakeHttp(_Resp(429, {
        'ok': False, 'error_code': 429,
        'description': 'Too Many Requests: retry after 5',
        'parameters': {'retry_after': 5}}))
    res = a._send_message(123, 'hi')
    assert res['retcode'] == -4 and res['throttled'] is True
    assert res['retry_after'] == 5


def test_kook_429_is_minus4_throttled():
    a = _kk()
    a._http = _FakeHttp(_Resp(429, {'code': 429},
                              headers={'Retry-After': '12'}))
    res = a._api('POST', '/api/v3/message/create', json_body={})
    assert res['retcode'] == -4 and res['throttled'] is True
    assert res['retry_after'] == 12


def test_matrix_limit_exceeded_is_minus4_throttled():
    a = _mx()
    a._http = _FakeHttp(_Resp(429, {
        'errcode': 'M_LIMIT_EXCEEDED', 'error': 'Too many requests',
        'retry_after_ms': 4000}))
    res = a.send_message('!r:m.example', 'hi')
    assert res['retcode'] == -4 and res['throttled'] is True
    assert res['retry_after'] == 4


def test_discord_429_is_minus4_throttled():
    a = _dc()
    a._http = _FakeHttp(_Resp(429, {'retry_after': 4.7, 'global': False}))
    res = a._rest_post('/channels/1/messages', json_body={'content': 'x'})
    assert res['retcode'] == -4 and res['throttled'] is True
    assert res['retry_after'] == 4


# ── 4. 真正超时 → -2（保持不动） ────────────────────────────────

def test_onebot_real_timeout_is_minus2(monkeypatch):
    class _FakeEvent:
        def set(self):
            pass

        async def wait(self):
            raise asyncio.TimeoutError

    class _FakeAsyncio:
        TimeoutError = asyncio.TimeoutError

        @staticmethod
        def Event():
            return _FakeEvent()

        @staticmethod
        async def wait_for(aw, timeout):
            return await aw

    class _WsServer:
        async def asend(self, name, payload):
            return True

    monkeypatch.setattr(ob, 'asyncio', _FakeAsyncio)
    conn = ob.BotConnection('x')
    conn._ws = object()
    conn._ws_server = _WsServer()
    assert asyncio.run(conn.acall('send_msg'))['retcode'] == -2


def test_rust_accel_real_timeout_is_minus2(monkeypatch):
    monkeypatch.setattr(ra, '_CALL_PY_TIMEOUT', 0.01)

    class _Proc:
        def poll(self):
            return None

    class _Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

    async def run():
        a = ra.RustAccelService(framework=None)
        a.proc = _Proc()
        a._writer = _Writer()
        a._loop = asyncio.get_running_loop()
        return await a.acall('send_msg')

    assert asyncio.run(run())['retcode'] == -2


# ── 5. 未知 / 不支持动作 → -10（文档定义，固定语义） ─────────────

@pytest.mark.parametrize('name,factory', _CALL_API_ADAPTERS,
                         ids=_CALL_API_IDS)
def test_unknown_action_is_minus10(name, factory):
    res = asyncio.run(factory().call_api('definitely_not_supported_action'))
    assert res['retcode'] == -10, (name, res)


def test_kook_active_private_msg_is_minus10():
    """文档里 -10 的例子：KOOK 主动私聊缺 chat_code。"""
    res = asyncio.run(_kk().call_api('send_private_msg',
                                     user_id='u1', message='x'))
    assert res['retcode'] == -10


@pytest.mark.parametrize('name,factory', _CALL_API_ADAPTERS,
                         ids=_CALL_API_IDS)
def test_status_ok_is_zero(name, factory):
    res = asyncio.run(factory().call_api('get_status'))
    assert res['retcode'] == 0, (name, res)


# ── 6. 出站闸门：只重试 -3（改错误码影响的重试路径） ─────────────

def _fresh_gate():
    from framework.messaging.outbound_guard import OutboundGate
    g = OutboundGate()
    g.configure({'rate_limit': {'enabled': False},
                 'retry': {'enabled': True, 'attempts': 1, 'backoff': 0}})
    return g


def test_gate_retries_minus3():
    g = _fresh_gate()
    seq = [{'status': 'failed', 'retcode': -3, 'msg': '连接断开'},
           {'status': 'ok', 'retcode': 0}]
    calls = []

    def call():
        calls.append(1)
        return seq.pop(0)

    res = g.execute_sync('send_msg', 'bot', {'group_id': 1}, call)
    assert res['retcode'] == 0 and len(calls) == 2


@pytest.mark.parametrize('code', [-1, -2, -4, -10])
def test_gate_does_not_retry_other_codes(code):
    g = _fresh_gate()
    calls = []

    def call():
        calls.append(1)
        return {'status': 'failed', 'retcode': code, 'msg': 'x'}

    res = g.execute_sync('send_msg', 'bot', {'group_id': 1}, call)
    assert res['retcode'] == code and len(calls) == 1


# ── 7. QQ 官方主动消息防重复投递语义（上一轮修复，必须保护） ──────

def test_qq_official_active_message_no_internal_retry():
    """主动消息（无 msg_seq/msg_id）网络异常时不内部重试，防重复投递。"""
    a = _qq()
    fake = _FakeHttp(error=RuntimeError('connection timeout'))
    a._http = fake
    with pytest.raises(Exception):
        a._api_request('POST', '/v2/groups/G/messages',
                       json_body={'content': 'x'})
    assert len(fake.calls) == 1


def test_qq_official_passive_message_with_msg_seq_may_retry(monkeypatch):
    """对照：带 msg_seq 的被动回复平台可去重，允许内部重试最多 2 次。"""
    monkeypatch.setattr(qq.time, 'sleep', lambda *_: None)
    a = _qq()
    fake = _FakeHttp(error=RuntimeError('connection timeout'))
    a._http = fake
    with pytest.raises(Exception):
        a._api_request('POST', '/v2/groups/G/messages',
                       json_body={'content': 'x', 'msg_seq': 1})
    assert len(fake.calls) == 3
