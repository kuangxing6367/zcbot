"""蜜罐误伤回归：前端探测未启用功能时不能被判成攻击。

起因（v1.22.0 线上）：WebUI 一打开就发两个请求 ——
`POST /api/session/handshake`（会话令牌默认关闭）和
`GET /api/plugins/image_renderer/status`（该插件没启用）。这两条落到
`/api/<path>` 兜底诱饵，走的是默认分支：**拉黑 IP + tarpit 600ms + 写审计**。
用户体感就是刷新几次后随机转圈/白屏，日志里刷满「命中诱饵」。

这组用例锁住：前端探测路径走良性分支，真正的未知路径仍然按攻击处理。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope='module')
def web_env():
    from framework.api import honeypot
    from framework.api.webapp import create_web_app
    from framework.core import Framework

    honeypot.reset_stats()
    tmp = tempfile.mkdtemp(prefix='zcbot_hp_fp_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'hp.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
            "log:\n  level: ERROR\n"
            "security:\n"
            "  silent_auth: false\n"
            "  human_check:\n    enabled: false\n"
            "  honeypot:\n"
            "    enabled: true\n"
            "    tarpit_ms: 0\n"
            "    blacklist: true\n"
            "    blacklist_ttl: 3600\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    client = create_web_app(fw).test_client()
    yield {'fw': fw, 'client': client, 'hp': honeypot, 'tmp': tmp, 'db_path': db_path}
    try:
        fw._db_executor.shutdown(wait=False)
    except Exception:
        pass
    try:
        fw.db.close()
    except Exception:
        pass
    for p in (db_path, db_path + '-wal', db_path + '-shm'):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def _stats(hp):
    s = hp.get_stats()
    return {k: s[k] for k in ('total_hits', 'benign_hits', 'blacklisted')}


def test_session_handshake_is_not_an_attack(web_env):
    """会话令牌关闭时，前端仍会 POST 握手；这不该被当成探测。"""
    c, hp = web_env['client'], web_env['hp']
    before = _stats(hp)
    r = c.post('/api/session/handshake', json={'did': 'x' * 32})
    after = _stats(hp)

    assert r.status_code == 404, '未启用功能应返回明确 404，而非诱饵假数据'
    body = r.get_json()
    assert body and body.get('code') == 404
    # 诱饵响应是 body_generic_decoy()，带伪造的业务字段，这里必须区分开
    assert 'data' not in body or not isinstance(body.get('data'), dict) or \
        'token' not in (body.get('data') or {})

    assert after['benign_hits'] == before['benign_hits'] + 1
    assert after['total_hits'] == before['total_hits'], '良性探测被计入攻击命中'
    assert after['blacklisted'] == before['blacklisted'], '良性探测触发了拉黑'


def test_plugin_status_probe_is_not_an_attack(web_env):
    """插件没启用时前端仍会拉一次状态；旧实现把 admin 自己拉黑了。"""
    c, hp = web_env['client'], web_env['hp']
    before = _stats(hp)
    r = c.get('/api/plugins/image_renderer/status')
    after = _stats(hp)

    assert r.status_code == 404
    assert r.get_json().get('code') == 404

    assert after['benign_hits'] == before['benign_hits'] + 1
    assert after['total_hits'] == before['total_hits']
    assert after['blacklisted'] == before['blacklisted']


def test_unknown_api_still_treated_as_probe(web_env):
    """放行前端探测不能顺手把兜底诱饵废掉：真未知路径照样计入攻击画像。"""
    c, hp = web_env['client'], web_env['hp']
    before = _stats(hp)
    r = c.get('/api/v1/admin/exec')
    after = _stats(hp)

    assert r.status_code == 200, '未知路径应由诱饵接管并返回伪数据'
    assert after['total_hits'] == before['total_hits'] + 1
    assert after['benign_hits'] == before['benign_hits'], '攻击命中被误记成良性'

    s = hp.get_stats()
    assert any(x['key'] == '/api/v1/admin/exec' for x in s['top_paths'])


def test_frontend_probe_does_not_pollute_threat_ranking(web_env):
    """良性探测不进 TOP IP 榜：安全看板的第一名不该是打开后台的自己。"""
    c, hp = web_env['client'], web_env['hp']
    hp.reset_stats()
    for _ in range(5):
        c.post('/api/session/handshake', json={'did': 'y' * 32})
        c.get('/api/plugins/image_renderer/status')

    s = hp.get_stats()
    assert s['benign_hits'] == 10
    assert s['total_hits'] == 0
    assert s['top_ips'] == []
    assert s['unique_ips'] == 0
