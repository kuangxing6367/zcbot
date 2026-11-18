"""蜜罐拉黑阈值回归：弱证据不能「碰一下就封一小时」。

线上体感是「随机刷新几次就被自己的服务器封掉」，根因有两层：
1. 未知 /api/* 与噪声诱饵（/health、/status、旧缓存 chunk 请求的接口）走的是
   默认分支 —— 单次命中直接拉黑 + tarpit，运维探针和浏览器预取都会被算成攻击；
2. 已登录管理员界面里调了一个本构建没有的接口（发版后浏览器还跑着旧 chunk），
   同样会被封 —— 能拿出有效 token 的人本来就在里面，封他没有安全收益。

现在改成：
- 强证据（读 .env / 备份 / git / 陷阱 / 绕过手法）单次即封；
- 弱证据（未知路径、噪声路径）窗口内累计到 `ban_threshold` 次才封；
- 携带有效管理员凭据的未知路径按「界面兼容」放行，只计良性，不封不禁。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope='module')
def env():
    from framework.api import honeypot
    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_hp_ban_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'hp.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
            "log:\n  level: ERROR\n"
            "security:\n"
            "  silent_auth: false\n"
            "  whitelist_ips: []\n"          # 默认白名单含 127.0.0.1，会让「拉黑」用例永远不生效
            "  human_check:\n    enabled: false\n"
            "  honeypot:\n"
            "    enabled: true\n"
            "    tarpit_ms: 0\n"
            "    blacklist: true\n"
            "    blacklist_ttl: 2\n"
            "    ban_internal_ips: true\n"      # 测试客户端是 127.0.0.1，需要它不被内网豁免
            "    ban_threshold: 3\n"
            "    ban_window: 60\n"
            "    decoy_set: full\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    app = create_web_app(fw)
    client = app.test_client()
    honeypot.reset_stats()
    yield {'fw': fw, 'app': app, 'client': client, 'hp': honeypot, 'db_path': db_path}

    try:
        app.dual_auth.unblacklist('127.0.0.1')
    except Exception:
        pass
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


def _clean(env):
    """每个用例开始前清干净：黑名单、计数、弱证据计数"""
    env['app'].dual_auth.unblacklist('127.0.0.1')
    env['hp'].reset_stats()
    env['hp']._strikes.clear()


def _blacklisted(env) -> bool:
    return env['app'].dual_auth.is_blacklisted('127.0.0.1')


def test_weak_single_hit_does_not_ban(env):
    """GET /health 一次（监控探针的典型行为）不能被封。"""
    _clean(env)
    c, hp = env['client'], env['hp']
    r = c.get('/health')
    assert r.status_code in (404, 403, 200)
    assert not _blacklisted(env), '单次弱证据就拉黑 = 把运维探针一起封了'
    s = hp.get_stats()
    assert s['total_hits'] == 1, '弱证据仍要计入威胁画像'
    assert s['blacklisted'] == 0


def test_weak_bans_after_threshold(env):
    """同一 IP 连续探测超过阈值才封。"""
    _clean(env)
    c, hp = env['client'], env['hp']
    for i in range(2):
        c.get('/health')
        assert not _blacklisted(env), f'第 {i + 1} 次就封了（阈值 3）'
    c.get('/health')
    assert _blacklisted(env), '达到阈值后必须真的拉黑'
    assert hp.get_stats()['blacklisted'] == 1


def test_strong_evidence_bans_immediately(env):
    """读 / .env 是强证据：单次即封，不受阈值影响。"""
    _clean(env)
    c, hp = env['client'], env['hp']
    r = c.get('/.env')
    assert r.status_code == 200, '强证据路径仍应返回高仿诱饵'
    assert _blacklisted(env), '强证据不该等阈值'
    assert hp.get_stats()['blacklisted'] == 1


def test_authenticated_admin_unknown_api_is_benign(env):
    """已登录管理员访问不存在的接口 → 普通 404，计良性，不封。"""
    _clean(env)
    c, hp = env['client'], env['hp']
    r = c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    token = (r.get_json().get('data') or {}).get('token')
    assert token, '登录未返回 token'
    hp.reset_stats()

    auth = {'Authorization': 'Bearer ' + token}
    resp = c.get('/api/v1/not-in-this-build', headers=auth)
    assert resp.status_code == 404
    body = resp.get_json() or {}
    assert body.get('code') == 404, '不应返回诱饵的 code=0 伪数据'
    assert not _blacklisted(env), '管理员被自己的服务器封了'
    s = hp.get_stats()
    assert s['benign_hits'] == 1
    assert s['total_hits'] == 0
    assert s['top_ips'] == []
