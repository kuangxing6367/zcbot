# -*- coding: utf-8 -*-
"""会话令牌协议测试（默认关闭；开启后才注册握手端点与请求闸门）"""
import math
import sys
import types
import os
import random
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify

from framework.api import session_tokens as st


def _split_even(s, n):
    size = math.ceil(len(s) / n)
    return [s[i:i + size] for i in range(0, len(s), size)]


def _reset():
    with st._lock:
        st._pending.clear()
        st._sessions.clear()


def _build(enabled=True, ttl=1800):
    app = Flask(__name__)

    @app.route('/api/ping')
    def ping():
        return jsonify({'code': 0, 'msg': 'pong'})

    class FW:
        config = {'security': {'session_tokens': {'enabled': enabled, 'ttl': ttl}}}

    ctx = types.SimpleNamespace(app=app, framework=FW(),
                                get_client_ip=lambda: '203.0.113.7')
    st.register(ctx)
    return app


def test_disabled_keeps_behaviour_unchanged():
    """关闭时不注册端点、闸门不生效 —— 现有行为完全不变"""
    _reset()
    c = _build(enabled=False).test_client()
    assert c.get('/api/ping').status_code == 200
    assert c.post('/api/session/handshake').status_code == 404


def test_handshake_issues_2048bit_master():
    _reset()
    c = _build().test_client()
    r = c.post('/api/session/handshake')
    assert r.status_code == 200
    master = r.get_json()['data']['master']
    assert len(master) == 512, len(master)          # 512 hex = 2048 bit
    assert len(_split_even(master, 6)) == 6


def test_confirm_accepts_shuffled_tokens_and_returns_444():
    """客户端拆 6 份并打乱回传：集合一致即通过，返回 444 并下发 sid Cookie"""
    _reset()
    c = _build().test_client()
    master = c.post('/api/session/handshake').get_json()['data']['master']
    parts = _split_even(master, 6)
    random.shuffle(parts)
    r = c.post('/api/session/confirm', json={'tokens': parts})
    assert r.status_code == 444, r.status_code
    assert 'zcbot_sid' in (r.headers.get('Set-Cookie') or '')


def test_gate_blocks_without_tokens():
    _reset()
    c = _build().test_client()
    master = c.post('/api/session/handshake').get_json()['data']['master']
    parts = _split_even(master, 6)
    c.post('/api/session/confirm', json={'tokens': parts})

    assert c.get('/api/ping').status_code == 444                       # 不带令牌
    assert c.get('/api/ping', headers={'X-ZC-Set': ','.join(parts)}).status_code == 200
    assert c.get('/api/ping', headers={'X-ZC-Set': 'a,b,c,d,e,f'}).status_code == 444  # 乱猜


def test_master_is_one_time():
    """主令牌一次性：重复 confirm 应失败"""
    _reset()
    c = _build().test_client()
    master = c.post('/api/session/handshake').get_json()['data']['master']
    parts = _split_even(master, 6)
    assert c.post('/api/session/confirm', json={'tokens': parts}).status_code == 444
    assert c.post('/api/session/confirm', json={'tokens': parts}).status_code == 444


def test_expired_tokens_are_rejected():
    """令牌过期后闸门拦截（444）→ 客户端现有逻辑会回到登录流程重新握手"""
    _reset()
    c = _build().test_client()
    master = c.post('/api/session/handshake').get_json()['data']['master']
    parts = _split_even(master, 6)
    c.post('/api/session/confirm', json={'tokens': parts})
    assert c.get('/api/ping', headers={'X-ZC-Set': ','.join(parts)}).status_code == 200

    with st._lock:
        for sid in list(st._sessions):
            st._sessions[sid]['expires_at'] = time.time() - 1

    assert c.get('/api/ping', headers={'X-ZC-Set': ','.join(parts)}).status_code == 444


def test_split_even_covers_whole_string():
    s = 'a' * 512
    parts = _split_even(s, 6)
    assert len(parts) == 6
    assert ''.join(parts) == s


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f"  {name}: OK")
    print("all passed")
