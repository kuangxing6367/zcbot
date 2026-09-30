# -*- coding: utf-8 -*-
"""qq_official 适配器增量能力测试（富媒体 / markdown / 按钮 / 互动回调 /
生命周期事件 / 撤回）。全部离线，不触网。"""
import asyncio
import pytest

from core_plugins.qq_official.main import (
    QQOfficialAdapter, _split_outgoing, _first_media, build_keyboard,
    normalize_interaction, normalize_notice, _LIFECYCLE_EVENTS, _MSG_TYPE_MEDIA,
    _MSG_TYPE_MARKDOWN, _MSG_TYPE_TEXT,
)


def _adapter(**kw):
    return QQOfficialAdapter(None, app_id='appid', app_secret='secret', **kw)


class _Recorder:
    """假 framework：只记录被分发的事件"""

    def __init__(self):
        self.events = []

    async def dispatch_event(self, event):
        self.events.append(event)


# ── 出站拆分 ─────────────────────────────────────────────────

def test_split_plain_text():
    text, media, md, buttons = _split_outgoing('你好')
    assert text == '你好' and media is None and md is None and buttons is None


def test_split_media_pick_first():
    msg = [
        {'type': 'text', 'data': {'text': '看图'}},
        {'type': 'image', 'data': {'file': 'base64://aGk='}},
        {'type': 'record', 'data': {'file': 'https://x/a.silk'}},
    ]
    _, media, _, _ = _split_outgoing(msg)
    assert media is not None and media[0] == 'image', "应取第一个媒体段"
    assert _first_media(msg)[0] == 'image'


def test_split_at_degrades_to_text():
    text, media, _, _ = _split_outgoing(
        [{'type': 'at', 'data': {'qq': '10001', 'name': '张三'}}])
    assert text == '@张三' and media is None


def test_split_markdown_and_buttons():
    msg = [
        {'type': 'markdown', 'data': {'content': '# 标题'}},
        {'type': 'keyboard', 'data': {'buttons': [{'text': '点我', 'data': 'go'}]}},
    ]
    _, _, md, buttons = _split_outgoing(msg)
    assert md == '# 标题' and len(buttons) == 1


# ── 按钮结构 ─────────────────────────────────────────────────

def test_build_keyboard_flat_list_becomes_one_row():
    kb = build_keyboard([{'text': 'A', 'data': 'a'}, {'text': 'B', 'data': 'b'}])
    assert kb is not None
    rows = kb['content']['rows']
    assert len(rows) == 1 and len(rows[0]['buttons']) == 2
    first = rows[0]['buttons'][0]
    assert first['render_data']['label'] == 'A'
    assert first['action']['type'] == 2 and first['action']['data'] == 'a'


def test_build_keyboard_rows_and_link():
    kb = build_keyboard([[{'text': 'A', 'data': 'a'}],
                         [{'text': '跳转', 'link': 'https://x'}]])
    rows = kb['content']['rows']
    assert len(rows) == 2
    assert rows[1]['buttons'][0]['action']['type'] == 0, "link 应为跳转类型"


def test_build_keyboard_empty():
    assert build_keyboard([]) is None and build_keyboard(None) is None


# ── 请求体构造（上传打桩）───────────────────────────────────

def test_build_body_media_uses_msg_type_7():
    ad = _adapter()
    seen = {}

    async def fake_upload(openid, seg_type, data, is_group):
        seen['seg_type'] = seg_type
        seen['is_group'] = is_group
        return {'file_info': 'FILE'}

    ad._upload_media = fake_upload
    body, err = asyncio.run(ad._build_body(
        'G1', [{'type': 'record', 'data': {'file': 'https://x/a.silk'}}],
        'MSG1', True))
    assert err == '' and body['msg_type'] == _MSG_TYPE_MEDIA
    assert body['media']['file_info'] == 'FILE'
    assert seen['seg_type'] == 'record' and seen['is_group'] is True
    assert body['msg_id'] == 'MSG1' and 'msg_seq' in body


def test_build_body_upload_failure_returns_error():
    ad = _adapter()

    async def boom(*_a, **_kw):
        raise RuntimeError('上传炸了')

    ad._upload_media = boom
    body, err = asyncio.run(ad._build_body(
        'G1', [{'type': 'image', 'data': {'file': 'base64://aGk='}}], '', True))
    assert body is None and '上传失败' in err


def test_build_body_markdown_with_keyboard():
    ad = _adapter()
    body, err = asyncio.run(ad._build_body('G1', [
        {'type': 'markdown', 'data': {'content': '# hi'}},
        {'type': 'keyboard', 'data': {'buttons': [{'text': 'go', 'data': 'g'}]}},
    ], '', True))
    assert err == '' and body['msg_type'] == _MSG_TYPE_MARKDOWN
    assert body['markdown']['content'] == '# hi'
    assert body['keyboard']['content']['rows'][0]['buttons'][0]['action']['data'] == 'g'


def test_build_body_markdown_template():
    ad = _adapter()
    body, _ = asyncio.run(ad._build_body('G1', [
        {'type': 'markdown',
         'data': {'template_id': 'TPL1', 'params': [{'key': 'k', 'values': ['v']}]}},
    ], '', True))
    assert body['markdown']['custom_template_id'] == 'TPL1'
    assert body['markdown']['params'][0]['key'] == 'k'


def test_build_body_buttons_only_degrades_to_markdown():
    ad = _adapter()
    body, _ = asyncio.run(ad._build_body('G1', [
        {'type': 'text', 'data': {'text': '选一个'}},
        {'type': 'keyboard', 'data': {'buttons': [{'text': 'go', 'data': 'g'}]}},
    ], '', True))
    assert body['msg_type'] == _MSG_TYPE_MARKDOWN, "按钮必须挂 markdown"
    assert body['markdown']['content'] == '选一个'


def test_build_body_plain_text():
    ad = _adapter()
    body, _ = asyncio.run(ad._build_body('G1', 'hi', '', True))
    assert body['msg_type'] == _MSG_TYPE_TEXT and body['content'] == 'hi'


# ── 上传 file_type 映射 ──────────────────────────────────────

@pytest.mark.parametrize('seg_type,expect', [
    ('image', '1'), ('video', '2'), ('record', '3'), ('voice', '3'), ('file', '4'),
])
def test_upload_media_file_type_and_endpoint(seg_type, expect):
    ad = _adapter()
    calls = {}

    def fake_api(method, path, json_body=None, files=None, data=None, timeout=20):
        calls['method'] = method
        calls['path'] = path
        calls['form'] = data
        calls['json'] = json_body
        return {'file_info': 'F'}

    ad._api_request = fake_api
    out = asyncio.run(ad._upload_media(
        'X1', seg_type, {'file': 'https://x/y'}, is_group=False))
    assert out['file_info'] == 'F'
    assert calls['path'] == '/v2/users/X1/files', "单聊上传端点应为 /v2/users/"
    assert calls['json']['file_type'] == int(expect)


# ── 互动事件与生命周期 ───────────────────────────────────────

def test_normalize_interaction_group_button():
    d = {'id': 'I1', 'chat_type': 1, 'scene': 'group', 'group_openid': 'G1',
         'group_member_openid': 'U1', 'timestamp': '2026-01-01T00:00:00+08:00',
         'data': {'resolved': {'button_data': 'do_thing', 'button_id': 'b1'}}}
    ev = normalize_interaction({'d': d}, 'bot')
    assert ev['type'] == 'notice' and ev['notice_type'] == 'interaction'
    assert ev['group_id'] == 'G1' and ev['user_id'] == 'U1'
    assert ev['button_data'] == 'do_thing' and ev['interaction_id'] == 'I1'


def test_on_interaction_dispatches_and_acks():
    ad = _adapter()
    ad.auto_ack_interaction = False          # ACK 单独测，这里不触网
    ad.framework = _Recorder()
    d = {'id': 'I1', 'group_openid': 'G1', 'user_openid': 'U1',
         'data': {'resolved': {'button_data': 'x'}}}
    asyncio.run(ad._on_interaction(d))
    assert len(ad.framework.events) == 1
    ev = ad.framework.events[0]
    assert ev['notice_type'] == 'interaction'
    # 互动 event_id 要进被动回复缓存，插件才能直接回这条回调
    assert ad._pending_reply['G1'][0] == 'I1'


def test_ack_only_for_button_and_menu_types():
    """官方规定只有 type=11(按钮)/12(菜单) 需要 ACK，其余回了会报"已回应" """
    ad = _adapter()
    calls = []

    async def fake_ack(iid, code=0):
        calls.append(iid)
        return {'status': 'ok', 'retcode': 0, 'data': {}}

    ad.ack_interaction = fake_ack
    ad.framework = _Recorder()
    asyncio.run(ad._on_interaction({'id': 'I1', 'type': 11, 'data': {}}))
    asyncio.run(ad._on_interaction({'id': 'I2', 'type': 12, 'data': {}}))
    asyncio.run(ad._on_interaction({'id': 'I3', 'type': 13, 'data': {}}))
    assert calls == ['I1', 'I2']


def test_default_intents_covers_message_and_interaction():
    from core_plugins.qq_official.main import _DEFAULT_INTENTS
    assert _DEFAULT_INTENTS & (1 << 25), "缺消息/生命周期位"
    assert _DEFAULT_INTENTS & (1 << 26), "缺互动事件位"


def test_on_interaction_dedup():
    ad = _adapter()
    ad.auto_ack_interaction = False
    ad.framework = _Recorder()
    d = {'id': 'SAME', 'group_openid': 'G1', 'data': {'resolved': {}}}
    asyncio.run(ad._on_interaction(d))
    asyncio.run(ad._on_interaction(d))
    assert len(ad.framework.events) == 1, "同一 interaction_id 只应分发一次"


@pytest.mark.parametrize('qq_event,notice_type', list(_LIFECYCLE_EVENTS.items())[:6])
def test_normalize_notice_lifecycle(qq_event, notice_type):
    ev = normalize_notice({'d': {'group_openid': 'G1',
                                 'op_member_openid': 'U2',
                                 'timestamp': 't'}}, 'bot',
                          notice_type, qq_event)
    assert ev['notice_type'] == notice_type and ev['group_id'] == 'G1'
    assert ev['operator_id'] == 'U2' and ev['qq_event'] == qq_event


def test_on_lifecycle_disabled_by_config():
    ad = _adapter()
    ad.enable_notice_events = False
    ad.framework = _Recorder()
    asyncio.run(ad._on_lifecycle('GROUP_ADD_ROBOT', {'group_openid': 'G1'}))
    assert ad.framework.events == [], "开关关闭时不应分发"


# ── call_api 新动作 ──────────────────────────────────────────

def test_call_api_delete_msg_group_path():
    ad = _adapter()
    calls = {}

    def fake_api(method, path, json_body=None, files=None, data=None, timeout=20):
        calls['method'] = method
        calls['path'] = path
        return {}

    ad._api_request = fake_api
    out = asyncio.run(ad.call_api('delete_msg', group_id='G1', message_id='M1'))
    assert out['status'] == 'ok'
    assert calls['method'] == 'DELETE'
    assert calls['path'] == '/v2/groups/G1/messages/M1'


def test_call_api_delete_msg_requires_id():
    ad = _adapter()
    out = asyncio.run(ad.call_api('delete_msg', group_id='G1'))
    assert out['status'] == 'failed' and 'message_id' in out['msg']


def test_call_api_ack_interaction():
    ad = _adapter()
    calls = {}

    def fake_api(method, path, json_body=None, files=None, data=None, timeout=20):
        calls['path'] = path
        calls['body'] = json_body
        return {}

    ad._api_request = fake_api
    out = asyncio.run(ad.call_api('ack_interaction', interaction_id='I9', code=0))
    assert out['status'] == 'ok'
    assert calls['path'] == '/interactions/I9' and calls['body'] == {'code': 0}


def test_call_api_markdown_and_buttons_shortcuts():
    ad = _adapter()
    captured = {}

    async def fake_send(openid, message, reply_msg_id=''):
        captured['message'] = message
        return {'status': 'ok', 'retcode': 0, 'data': {}}

    ad._send_group = fake_send
    asyncio.run(ad.call_api(
        'send_msg', group_id='G1', message='hi',
        markdown='# 标题', buttons=[{'text': 'go', 'data': 'g'}]))
    msg = captured['message']
    types = [s.get('type') for s in msg if isinstance(s, dict)]
    assert 'markdown' in types and 'keyboard' in types


def test_call_api_send_voice_segment():
    """语音段应走富媒体通道（msg_type 7），而不是被压成文本"""
    ad = _adapter()
    captured = {}

    async def fake_upload(openid, seg_type, data, is_group):
        captured['seg_type'] = seg_type
        return {'file_info': 'F'}

    def fake_api(method, path, json_body=None, files=None, data=None, timeout=20):
        captured['path'] = path
        captured['body'] = json_body
        return {}

    ad._upload_media = fake_upload
    ad._api_request = fake_api
    out = asyncio.run(ad.call_api(
        'send_msg', group_id='G1',
        message=[{'type': 'record', 'data': {'file': 'https://x/a.silk'}}]))
    assert out['status'] == 'ok'
    assert captured['seg_type'] == 'record'
    assert captured['path'] == '/v2/groups/G1/messages'
    assert captured['body']['msg_type'] == 7
