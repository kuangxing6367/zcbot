"""新增接入端（KOOK / Matrix）与 OneBot 自身消息过滤。

这些用例只覆盖「不需要真实凭证也能判定对错」的部分：事件归一化、
自消息过滤、since 持久化、幂等键。真实收发仍需要凭证，测试里不假装覆盖。
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


kk = _load('zcbot_test_kook2', 'kook')
mx = _load('zcbot_test_matrix2', 'matrix')
ob = _load('zcbot_test_onebot2', 'onebot_adapter')


# ── KOOK 事件归一化 ───────────────────────────────────────────

def _kook_msg(**over):
    d = {
        'channel_type': 'GROUP', 'type': 1, 'target_id': 'chan1',
        'author_id': 'u1', 'content': 'hello', 'msg_id': 'm1',
        'msg_timestamp': 1, 'extra': {
            'type': 1, 'guild_id': 'g1', 'author': {
                'id': 'u1', 'username': 'alice', 'nickname': '小A', 'bot': False},
        },
    }
    d.update(over)
    return d


def test_kook_text_message():
    ev = kk.normalize_event(_kook_msg(), 'kookbot')
    assert ev['message'] == 'hello'
    assert ev['group_id'] == 'chan1'
    assert ev['user_id'] == 'u1'
    assert ev['sender']['nickname'] == '小A'
    assert ev['segments'][0]['type'] == 'text'


def test_kook_filters_bot_messages():
    """官方文档专门警告：不过滤 bot 消息会形成两个机器人互相回复的死循环。"""
    d = _kook_msg()
    d['extra']['author']['bot'] = True
    assert kk.normalize_event(d, 'kookbot') is None


def test_kook_filters_self_messages():
    assert kk.normalize_event(_kook_msg(author_id='self'), 'kookbot',
                              self_id='self') is None


def test_kook_image_message_builds_image_segment():
    d = _kook_msg(type=2, content='', extra={
        'type': 2, 'guild_id': 'g1', 'author': {'id': 'u1', 'bot': False},
        'attachments': {'type': 'image', 'name': 'a.jpg',
                        'url': 'https://img.example/a.jpg'},
    })
    ev = kk.normalize_event(d, 'kookbot')
    assert ev['segments'][0]['type'] == 'image'
    assert ev['segments'][0]['data']['url'] == 'https://img.example/a.jpg'


def test_kook_ignores_non_message_events():
    assert kk.normalize_event({'type': 255, 'extra': {}}, 'kookbot') is None


def test_kook_dedupes_old_sn():
    """官方要求：收到已处理过的 sn 直接丢弃，否则同一条命令会执行两次。"""
    a = kk.KookAdapter(framework=None, token='t', bot_name='k')
    assert a._accept_sn(5) is True
    assert a._sn == 5
    assert a._accept_sn(5) is False, '重复 sn 必须丢弃'
    assert a._accept_sn(4) is False, '更旧的 sn 必须丢弃'
    assert a._accept_sn(6) is True
    assert a._sn == 6


def test_kook_private_send_is_explicitly_unsupported():
    import asyncio
    a = kk.KookAdapter(framework=None, token='t', bot_name='k')
    res = asyncio.run(a.call_api('send_private_msg', user_id='u1', message='x'))
    assert res['status'] == 'failed'
    assert 'chat_code' in res['msg'], '不能假装私聊发送成功'


# ── Matrix ────────────────────────────────────────────────────

def _matrix_ev(**over):
    ev = {
        'type': 'm.room.message', 'event_id': '$e1', 'sender': '@u:m.example',
        'origin_server_ts': 1, 'content': {'msgtype': 'm.text', 'body': 'hi'},
    }
    ev.update(over)
    return ev


def test_matrix_text_message():
    ev = mx.normalize_event(_matrix_ev(), '!room:m.example', 'mx')
    assert ev['message'] == 'hi'
    assert ev['group_id'] == '!room:m.example'
    assert ev['user_id'] == '@u:m.example'


def test_matrix_filters_self():
    assert mx.normalize_event(_matrix_ev(), '!r:m.example', 'mx',
                              self_id='@u:m.example') is None


def test_matrix_skips_non_message_events():
    assert mx.normalize_event(_matrix_ev(type='m.room.member'),
                              '!r:m.example', 'mx') is None


def test_matrix_image_segment_and_mxc_convert():
    ev = mx.normalize_event(_matrix_ev(content={
        'msgtype': 'm.image', 'body': 'pic.png',
        'url': 'mxc://example/media1', 'mimetype': 'image/png',
    }), '!r:m.example', 'mx')
    assert ev['segments'][0]['type'] == 'image'
    url = mx._mxc_to_http('mxc://example/media1', 'https://m.example')
    # spec v1.11 起 /_matrix/media/v3/download 已 deprecated，
    # 统一走需认证的 Client-Server v1 media 端点
    assert url == (
        'https://m.example/_matrix/client/v1/media/download'
        '/example/media1')


def test_matrix_since_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv('ZCBOT_HOME', str(tmp_path))
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         access_token='t', user_id='@b:m.example')
    a._since = 'batch-1'
    a._save_since()

    state = json.loads(
        (tmp_path / 'data' / 'matrix_sync.json').read_text(encoding='utf-8'))
    assert state['since'] == 'batch-1'
    assert 't' != state.get('access_token'), '状态文件不得存 token'

    b = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         access_token='t', user_id='@b:m.example')
    assert b._load_since() == 'batch-1'


def test_matrix_since_ignored_for_other_account(tmp_path, monkeypatch):
    monkeypatch.setenv('ZCBOT_HOME', str(tmp_path))
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)
    a = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                         access_token='t', user_id='@bot:m.example')
    a._since = 'batch-1'
    a._save_since()

    other = mx.MatrixAdapter(framework=None, homeserver='https://m.example',
                             access_token='t', user_id='@other:m.example')
    assert other._load_since() == '', '换账号不能沿用旧 since，否则会静默跳过消息'


# ── OneBot 自身消息回显 ───────────────────────────────────────

def test_onebot_filters_message_sent():
    raw = {'post_type': 'message_sent', 'message_type': 'group',
           'user_id': 1, 'group_id': 2, 'message': 'x', 'self_id': 1}
    assert ob.normalize_event(raw, 'ob') is None


def test_onebot_filters_self_uid_message():
    raw = {'post_type': 'message', 'message_type': 'group',
           'user_id': 100, 'group_id': 2, 'message': 'x', 'self_id': 100}
    assert ob.normalize_event(raw, 'ob') is None


def test_onebot_keeps_other_users():
    raw = {'post_type': 'message', 'message_type': 'group',
           'user_id': 200, 'group_id': 2, 'message': 'x', 'self_id': 100}
    assert ob.normalize_event(raw, 'ob') is not None
