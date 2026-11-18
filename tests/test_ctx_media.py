"""ctx 媒体动作与 OneBot 出站：撤回 / 落盘 / 段转换。

两个动作面必须分开验：`ctx.actions` 可能是类型化的 OneBot API 封装，
也可能是协议中立的转发代理。两者的调用签名不同，混着用会**静默**把参数
塞错位置——撤回把 `message_id` 当 `bot` 发出去，谁都不会报错，只是消息没撤回。
"""
import base64
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from framework.ctx.messaging import PluginMessagingMixin
from framework.messaging.event import Event
from framework.messaging.segments import at, file_seg, image, text, voice

# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────

class _Ctx(PluginMessagingMixin):
    """只提供 actions 的最小 ctx。mixin 的其它字段都是即时消息才用得到的。"""

    def __init__(self, act):
        self._act = act

    @property
    def actions(self):
        return self._act


class _OneBotApi:
    """模拟 onebot_api：delete_msg 是类上的显式方法（可判定真伪）。"""

    def __init__(self, file_ref=''):
        self.calls = []
        self._file_ref = file_ref

    def delete_msg(self, message_id, bot=None):
        self.calls.append(('delete_msg', message_id, bot))
        return {'status': 'ok', 'retcode': 0}

    def get_image(self, file, bot=None):
        self.calls.append(('get_image', file, bot))
        if not self._file_ref:
            return {'status': 'failed', 'retcode': -1}
        return {'status': 'ok', 'data': {'file': self._file_ref}}

    def get_record(self, file, out_format='mp3', bot=None):
        self.calls.append(('get_record', file, out_format, bot))
        return {'status': 'failed', 'retcode': -1}


class _ProxyApi:
    """模拟 ActionProxy：只有 call/acall，其余动作靠 __getattr__ 动态转发。"""

    def __init__(self, file_ref=''):
        self.calls = []
        self._file_ref = file_ref

    def call(self, action, bot=None, **params):
        self.calls.append(('call', action, bot, params))
        if action == 'get_image' and self._file_ref:
            return {'status': 'ok', 'data': {'file': self._file_ref}}
        return {'status': 'failed', 'retcode': -10}

    async def acall(self, action, bot=None, **params):
        self.calls.append(('acall', action, bot, params))
        if action == 'get_image' and self._file_ref:
            return {'status': 'ok', 'data': {'file': self._file_ref}}
        return {'status': 'failed', 'retcode': -10}

    def __getattr__(self, action):
        def _method(bot=None, **params):
            return self.call(action, bot=bot, **params)
        return _method


# ─────────────────────────────────────────────────────────────
# 撤回
# ─────────────────────────────────────────────────────────────

def test_recall_calls_typed_api_directly():
    api = _OneBotApi()
    res = _Ctx(api).recall(42)
    assert res['status'] == 'ok'
    assert api.calls == [('delete_msg', 42, None)]


def test_recall_passes_bot_through():
    api = _OneBotApi()
    _Ctx(api).recall(42, bot='bot_a')
    assert api.calls == [('delete_msg', 42, 'bot_a')]


def test_recall_via_proxy_keeps_message_id_in_params():
    """代理的动态方法签名是 (bot=None, **params)。

    位置传参会让 message_id 落进 bot，撤回请求发往一个叫 "42" 的实例——
    没有异常、没有日志，就是没撤回。这里锁死必须走关键字。
    """
    api = _ProxyApi()
    _Ctx(api).recall(42)
    assert api.calls == [('call', 'delete_msg', None, {'message_id': 42})]


async def test_arecall_awaits_acall():
    api = _ProxyApi()
    await _Ctx(api).arecall(7, bot='b1')
    assert api.calls == [('acall', 'delete_msg', 'b1', {'message_id': 7})]


# ─────────────────────────────────────────────────────────────
# download_media
# ─────────────────────────────────────────────────────────────

def test_download_media_base64(tmp_path):
    payload = base64.b64encode(b'abc').decode()
    res = _Ctx(_OneBotApi()).download_media(
        {'type': 'image', 'data': {'base64': payload}}, dest_dir=str(tmp_path))
    assert res['status'] == 'ok'
    assert res['size'] == 3


def test_download_media_file_id_resolved_by_adapter(tmp_path):
    """file_id 只有接入端能换地址：先 get_image，拿到真实路径再下载。"""
    src = tmp_path / 'real.png'
    src.write_bytes(b'png!')
    api = _OneBotApi(file_ref=str(src))
    res = _Ctx(api).download_media(
        {'type': 'image', 'data': {'file_id': 'FI-1'}},
        dest_dir=str(tmp_path / 'out'))
    assert res['status'] == 'ok'
    assert res['size'] == 4
    assert ('get_image', 'FI-1', None) in api.calls


def test_download_media_file_id_falls_back_to_record(tmp_path):
    """get_image 没有结果时试 get_record（语音就是这条路径）。"""
    src = tmp_path / 'real.amr'
    src.write_bytes(b'amr')
    api = _OneBotApi(file_ref='')
    calls = []
    api.get_record = lambda file, out_format='mp3', bot=None: (
        calls.append(('get_record', file, out_format)),
        {'status': 'ok', 'data': {'file': str(src)}})[1]
    res = _Ctx(api).download_media(
        {'type': 'voice', 'data': {'file_id': 'FI-2'}},
        dest_dir=str(tmp_path / 'out'))
    assert res['status'] == 'ok'
    assert calls and calls[0][1] == 'FI-2'


def test_download_media_file_id_unsupported(tmp_path):
    """换不出地址时明确报 unsupported，而不是留下一堆空结果。"""
    api = _OneBotApi(file_ref='')
    res = _Ctx(api).download_media(
        {'type': 'image', 'data': {'file_id': 'FI-3'}},
        dest_dir=str(tmp_path))
    assert res['status'] == 'unsupported'
    assert 'file_id' in res['msg']


def test_download_media_file_info_without_id(tmp_path):
    """file_info 里连 uuid 都没有时，不要拿空串去发请求。"""
    res = _Ctx(_OneBotApi()).download_media(
        {'type': 'file', 'data': {'file_info': {'nope': 1}}},
        dest_dir=str(tmp_path))
    assert res['status'] in ('unsupported', 'failed')


def test_download_media_rejects_bad_max_bytes(tmp_path):
    res = _Ctx(_OneBotApi()).download_media(
        'base64://' + base64.b64encode(b'x').decode(),
        dest_dir=str(tmp_path), max_bytes='abc')
    assert res['status'] == 'failed'
    assert 'max_bytes' in res['msg']


def test_download_media_over_limit(tmp_path):
    res = _Ctx(_OneBotApi()).download_media(
        {'type': 'image', 'data': {'base64': 'A' * 4096}},
        dest_dir=str(tmp_path), max_bytes=16)
    assert res['status'] == 'failed'


async def test_adownload_media_runs_off_loop(tmp_path):
    payload = base64.b64encode(b'zz').decode()
    res = await _Ctx(_OneBotApi()).adownload_media(
        {'type': 'image', 'data': {'base64': payload}}, dest_dir=str(tmp_path))
    assert res['status'] == 'ok'
    assert res['size'] == 2


# ─────────────────────────────────────────────────────────────
# 事件媒体属性
# ─────────────────────────────────────────────────────────────

def _event(message, **extra):
    raw = {'post_type': 'message', 'message_type': 'group',
           'message_id': 1, 'user_id': 2, 'group_id': 3,
           'message': message, 'adapter': 'telegram'}
    raw.update(extra)
    return Event(raw)


def test_event_video_note_properties():
    ev = _event([{'type': 'video_note', 'data': {'file': 'v.mp4'}}])
    assert ev.has_video_note is True
    assert ev.video_notes == [{'file': 'v.mp4'}]
    assert ev.first_video_note == {'file': 'v.mp4'}


def test_event_no_video_note():
    ev = _event([{'type': 'text', 'data': {'text': 'hi'}}])
    assert ev.has_video_note is False
    assert ev.video_notes == []
    assert ev.first_video_note == {}


def test_event_audio_folds_into_voices():
    """`audio` 是保留名，归一后进 voices——文档里写的就是这个行为，
    别哪天改了别名表而文档没跟上。"""
    ev = _event([{'type': 'audio', 'data': {'file': 'a.mp3'}}])
    assert ev.voices == [{'file': 'a.mp3'}]
    assert ev.has_voice is True


def test_event_animation_folds_into_videos():
    ev = _event([{'type': 'animation', 'data': {'file': 'a.gif'}}])
    assert ev.videos == [{'file': 'a.gif'}]


def test_event_media_url_from_data_dict():
    ev = _event([{'type': 'image', 'data': {'url': 'https://x/a.png'}}])
    assert ev.media_url(ev.first_image) == 'https://x/a.png'


def test_event_media_url_base64_prefixed():
    ev = _event([{'type': 'image', 'data': {'base64': 'AAA'}}])
    assert ev.media_url(ev.first_image) == 'base64://AAA'


def test_event_malformed_segments_do_not_crash():
    """段数组里混进垃圾时属性要能返回空值，不能把整条事件搞挂。"""
    ev = _event([None, 'plain', 123, {'type': 'image', 'data': None},
                 {'type': 'image', 'data': {'file': 'ok.png'}}])
    assert ev.images == [{}, {'file': 'ok.png'}]
    assert ev.first_image == {}
    assert ev.has_image is True


# ─────────────────────────────────────────────────────────────
# OneBot 出站段转换
# ─────────────────────────────────────────────────────────────

def _adapter_class():
    path = ROOT / 'core_plugins' / 'onebot_adapter' / 'main.py'
    spec = importlib.util.spec_from_file_location('_ob_main_for_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.OneBotAdapter


@pytest.fixture(scope='module')
def adapter():
    return _adapter_class().__new__(_adapter_class())


def test_native_moves_url_into_file(adapter):
    """OneBot 发送只认 file；url 是入站只读字段，不挪过去就发不出图。"""
    out = adapter.to_native([image(url='https://x/a.png')])
    assert out[0]['type'] == 'image'
    assert out[0]['data']['file'] == 'https://x/a.png'


def test_native_moves_base64_into_file(adapter):
    out = adapter.to_native([image(base64='AAA')])
    assert out[0]['data']['file'] == 'base64://AAA'


def test_native_moves_path_into_file(adapter):
    out = adapter.to_native([file_seg(path='/tmp/a.pdf', name='a.pdf')])
    assert out[0]['type'] == 'file'
    assert out[0]['data']['file'] == '/tmp/a.pdf'


def test_native_voice_back_to_record(adapter):
    """规范名是 voice，OneBot 的段名是 record。发 voice 出去会被当未知段。"""
    out = adapter.to_native([voice(url='https://x/a.amr')])
    assert out[0]['type'] == 'record'


def test_native_keeps_existing_file(adapter):
    seg = {'type': 'image', 'data': {'file': 'file:///c.png'}}
    assert adapter.to_native([seg])[0]['data']['file'] == 'file:///c.png'


def test_native_text_and_at_untouched(adapter):
    out = adapter.to_native([text('hi'), at(123)])
    assert out[0] == {'type': 'text', 'data': {'text': 'hi'}}
    assert out[1]['type'] == 'at'
    assert out[1]['data']['qq'] == '123'


def test_native_drops_empty_text(adapter):
    """空文本段发出去只会让 OneBot 返回业务错误，直接丢掉。"""
    assert adapter.to_native([text('')]) == []


@pytest.mark.parametrize('segments', [None, [], [None], ['str'], [123],
                                      [{}], [{'type': 'image', 'data': None}]])
def test_native_malformed_input(adapter, segments):
    out = adapter.to_native(segments)
    assert isinstance(out, list)
    for seg in out:
        assert isinstance(seg, dict)
        assert 'type' in seg and isinstance(seg['data'], dict)


def test_native_unknown_type_passes_through(adapter):
    """认不出的类型保持原名，不能悄悄变成 text（会丢失原意）。"""
    out = adapter.to_native([{'type': 'share', 'data': {'url': 'u'}}])
    assert out[0]['type'] == 'share'


async def test_call_api_skips_conversion_for_send(adapter):
    """发送类动作要过一遍 to_native。"""
    captured = {}

    class _Caller:
        async def acall(self, action, bot=None, **params):
            captured['action'] = action
            captured['params'] = params
            return {'status': 'ok'}

    adapter.api_caller = _Caller()
    await adapter.call_api('send_group_msg', group_id=1,
                           message=[image(url='https://x/a.png')])
    sent = captured['params']['message']
    assert sent[0]['data']['file'] == 'https://x/a.png'


async def test_call_api_respects_auto_escape(adapter):
    """auto_escape=True 表示当纯文本，不能顺手解析成段。"""
    captured = {}

    class _Caller:
        async def acall(self, action, bot=None, **params):
            captured['params'] = params
            return {'status': 'ok'}

    adapter.api_caller = _Caller()
    await adapter.call_api('send_group_msg', group_id=1,
                           message='[CQ:image,file=x]', auto_escape=True)
    assert captured['params']['message'] == '[CQ:image,file=x]'


async def test_call_api_non_send_action_untouched(adapter):
    captured = {}

    class _Caller:
        async def acall(self, action, bot=None, **params):
            captured['params'] = params
            return {'status': 'ok'}

    adapter.api_caller = _Caller()
    await adapter.call_api('get_group_list', message=[image(url='u')])
    assert captured['params']['message'] == [image(url='u')]
