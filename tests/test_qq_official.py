# -*- coding: utf-8 -*-
"""qq_official 适配器单元测试（v1.7.2 修复防回归：发送判定/token 锁/被动窗口/
幂等去重/主动消息出口/上传文件名统一）。全部离线，不触网。"""
import asyncio
import time
import types

import pytest

from core_plugins.qq_official.main import (
    QQOfficialAdapter, _next_msg_seq, normalize_event, normalize_message)


def _adapter(**kw):
    return QQOfficialAdapter(None, app_id='appid', app_secret='secret', **kw)


# ── 归一化 ───────────────────────────────────────────────────


def test_normalize_message_cq_base64_to_segment():
    out = normalize_message('[CQ:image,file=base64://aGk=]')
    assert isinstance(out, list) and out[0]['type'] == 'image'
    assert out[0]['data']['base64'] == 'aGk='


def test_normalize_message_plain_text_passthrough():
    assert normalize_message('你好') == '你好'


def test_next_msg_seq_in_range():
    seqs = {_next_msg_seq() for _ in range(100)}
    assert all(1 <= s <= 9999 for s in seqs)


def test_normalize_event_group_and_private():
    g = normalize_event({'_t': 'GROUP_AT_MESSAGE_CREATE', 'id': 'E1', 'd': {
        'id': 'M1', 'group_openid': 'G123', 'author': {'id': 'U1'},
        'content': 'hi'}}, 'bot')
    assert g['message_type'] == 'group' and g['group_id'] == 'G123'
    assert g['reply_msg_id'] == 'M1' and g['user_id'] == 'U1'

    p = normalize_event({'_t': 'C2C_MESSAGE_CREATE', 'id': 'E2', 'd': {
        'id': 'M2', 'author': {'user_openid': 'U9'}, 'content': 'yo'}}, 'bot')
    assert p['message_type'] == 'private' and p['group_id'] is None
    assert p['user_id'] == 'U9'


def test_normalize_event_attachments_structured():
    att = [{'url': 'https://example.com/a.png', 'content_type': 'image/png'}]
    ev = normalize_event({'_t': 'GROUP_AT_MESSAGE_CREATE', 'id': 'E3', 'd': {
        'id': 'M3', 'group_openid': 'G1', 'author': {'id': 'U1'},
        'content': '看图', 'attachments': att}}, 'bot')
    # content 仍是纯文本（含 URL 提示行），但附件同时结构化透传
    assert 'https://example.com/a.png' in ev['message']
    assert ev['attachments'] == att


# ── 被动回复窗口（群 5 分钟 / 单聊 60 分钟，分开清理） ───────────


def test_reply_window_per_kind():
    a = _adapter()
    now = time.time()
    a._pending_reply = {
        'G1': ('MG', now - 400, 'group'),    # 群窗口(300s)已过 → 清掉
        'U1': ('MU', now - 400, 'private'),  # 单聊窗口(3600s)未过 → 保留
    }
    assert a._reply_msg_id('G1') == ''
    assert 'G1' not in a._pending_reply
    assert a._reply_msg_id('U1') == 'MU'
    assert 'U1' in a._pending_reply


# ── 发送成败判定 ─────────────────────────────────────────────


def test_send_group_success_wraps_ok():
    async def run():
        a = _adapter()
        a._api_request = lambda *ar, **kw: {'id': 'MSG1'}
        r = await a._send_group('G1', 'hello', reply_msg_id='M1')
        assert r['status'] == 'ok' and r['data'] == {'id': 'MSG1'}
    asyncio.run(run())


def test_send_group_http_failure_wraps_failed():
    async def run():
        a = _adapter()

        def _raise(*ar, **kw):
            raise RuntimeError('POST /v2/groups/G1/messages -> HTTP 429: limited')
        a._api_request = _raise
        r = await a._send_group('G1', 'hello', reply_msg_id='M1')
        assert r['status'] == 'failed' and '429' in r['msg']
    asyncio.run(run())


def test_active_flag_skips_msg_id():
    async def run():
        a = _adapter()
        a._pending_reply = {'G1': ('MOLD', time.time(), 'group')}
        captured = {}

        async def fake_send_group(openid, message, reply_msg_id=''):
            captured['reply'] = reply_msg_id
            return {'status': 'ok', 'retcode': 0, 'data': {}}
        a._send_group = fake_send_group
        await a.call_api('send_msg', group_id='G1', message='主动', active=True)
        assert captured['reply'] == ''           # active=True 不挂旧 msg_id
        await a.call_api('send_msg', group_id='G1', message='被动')
        assert captured['reply'] == 'MOLD'       # 默认仍自动补被动 id
    asyncio.run(run())


# ── HTTP 状态与 token ────────────────────────────────────────


def test_api_request_non_2xx_raises():
    a = _adapter()
    a._access_token = 'tok'
    a._token_expire_at = time.time() + 1000
    fake = types.SimpleNamespace(status_code=429, text='rate limited', content=b'')
    a._http = types.SimpleNamespace(request=lambda *ar, **kw: fake)
    with pytest.raises(RuntimeError, match='429'):
        a._api_request('POST', '/v2/groups/G1/messages', json_body={})


def test_token_reused_when_fresh_and_refreshed_when_stale():
    a = _adapter()

    class _NoHTTP:
        def post(self, *ar, **kw):
            raise AssertionError('fresh token 不应触发刷新请求')
    a._http = _NoHTTP()
    a._access_token = 'fresh'
    a._token_expire_at = time.time() + 1000
    assert a._refresh_token_if_stale() == 'fresh'

    calls = []

    def fake_post(*ar, **kw):
        calls.append(1)
        return types.SimpleNamespace(
            content=b'{"access_token":"new"}', status_code=200,
            json=lambda: {'access_token': 'new', 'expires_in': 7200})
    a._http = types.SimpleNamespace(post=fake_post)
    a._token_expire_at = time.time() - 1        # 过期
    assert a._refresh_token_if_stale() == 'new'
    assert len(calls) == 1
    # 拿锁后复查：未过期时再调不再发请求
    assert a._refresh_token_if_stale() == 'new'
    assert len(calls) == 1
