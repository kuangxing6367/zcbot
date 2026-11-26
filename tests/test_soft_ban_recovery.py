"""软封禁 / 硬封禁分层回归：旧缓存文件误封后真人能自救。

起因：浏览器缓存的旧 chunk 请求已下线接口 → 落蜜罐弱证据兜底 → IP 被拉黑。
旧实现把弱证据和强证据一视同仁：拉黑后全局守卫拦截**所有**路由，连登录页
和 /api/login 都进不去，用户只能干等 TTL。本组用例锁住三件事：
1. 软封禁（弱证据 / 认证累计失败）不阻断登录自救链路；
2. 登录成功自动解除软封禁，硬封禁不受影响；
3. 软封禁 TTL 独立于硬封禁，且更短。
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

    tmp = tempfile.mkdtemp(prefix='zcbot_softban_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'softban.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
            "log:\n  level: ERROR\n"
            "security:\n"
            "  silent_auth: false\n"
            "  whitelist_ips: []\n"
            "  human_check:\n    enabled: false\n"
            "  honeypot:\n"
            "    enabled: true\n"
            "    tarpit_ms: 0\n"
            "    blacklist: true\n"
            "    ban_internal_ips: true\n"
            "    blacklist_ttl: 3600\n"
            "    soft_ban_ttl: 120\n"
            "    ban_threshold: 3\n"
            "    ban_window: 60\n"
            "    auth_fail_blacklist: 3\n"
            "    auth_fail_window: 300\n"
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
    honeypot._strikes.clear()
    yield {'fw': fw, 'app': app, 'client': client, 'hp': honeypot, 'db_path': db_path,
           'cfg_path': cfg}

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
    env['app'].dual_auth.unblacklist('127.0.0.1')
    env['hp'].reset_stats()
    env['hp']._strikes.clear()


def _ban_soft(env):
    """触发弱证据软封禁：未知 API 路径累计到阈值。

    用全新的 test_client（不带任何登录 cookie），否则已登录管理员的未知路径
    会按「界面兼容」benign 放行，攒不够阈值。
    """
    c = env['app'].test_client()
    for _ in range(3):
        c.get('/api/v1/definitely-not-in-this-build')
    assert env['app'].dual_auth.is_blacklisted('127.0.0.1')
    assert env['app'].dual_auth.is_soft_blacklisted('127.0.0.1')


def test_soft_ban_allows_login_self_help(env):
    """软封禁不阻断登录页与登录接口，仍能登录"""
    _clean(env)
    c = env['client']
    _ban_soft(env)

    # 登录页与静态资源可达
    assert c.get('/').status_code in (200, 302)
    assert c.get('/js/app.B5kUiDZJ.js').status_code in (200, 404)
    # 登录链路接口可达
    assert c.get('/api/challenge').status_code == 200
    assert c.get('/api/version').status_code == 200
    r = c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


def test_login_clears_soft_ban(env):
    """登录成功自动解除软封禁"""
    _clean(env)
    c = env['client']
    _ban_soft(env)
    assert env['app'].dual_auth.is_soft_blacklisted('127.0.0.1')

    r = c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200
    assert not env['app'].dual_auth.is_blacklisted('127.0.0.1'), '登录成功应解除软封禁'


def test_soft_ban_still_blocks_business_api(env):
    """软封禁不是全面放行：非自救的业务 API 仍应返回 403"""
    _clean(env)
    _ban_soft(env)
    c = env['app'].test_client()  # 无 cookie，避免已登录兼容放行
    r = c.get('/api/users')
    assert r.status_code == 403, '软封禁应阻断普通业务 API，只放行登录自救链路'


def test_login_does_not_clear_hard_ban(env):
    """硬封禁（强证据）不会被登录解除，且阻断登录接口"""
    _clean(env)
    c = env['client']
    # 读 .env 是强证据，单次即硬封禁
    c.get('/.env')
    assert env['app'].dual_auth.is_blacklisted('127.0.0.1')
    assert not env['app'].dual_auth.is_soft_blacklisted('127.0.0.1')

    # 硬封禁连登录接口都拦
    r = c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 403
    assert env['app'].dual_auth.is_blacklisted('127.0.0.1'), '硬封禁不应被登录绕过'


def test_soft_ban_ttl_is_short(env):
    """软封禁 TTL 使用 soft_ban_ttl（120s），与硬封禁 3600s 不同"""
    from framework.api import honeypot as hp

    _clean(env)
    c = env['client']
    # 配置在 resolve 后生效；直接检查解析出的档位值
    from framework.api.security_levels import resolve
    sec = resolve(env['fw'].config.get('security'))
    assert sec['honeypot']['soft_ban_ttl'] == 120
    assert sec['honeypot']['blacklist_ttl'] == 3600

    _ban_soft(env)
    entry = env['app'].dual_auth._blacklist.get('127.0.0.1')
    assert entry is not None
    assert entry['source'].endswith('_soft')
    now = __import__('time').time()
    assert 0 < entry['expires_at'] - now <= 121, '软封禁 TTL 应接近 120s 而非 3600s'


def test_auth_failures_cause_soft_ban(env):
    """认证连续失败累计到阈值 → 软封禁（而非硬封禁），且登录接口仍可达"""
    _clean(env)
    c = env['app'].test_client()
    # 用无效 token 反复访问需要鉴权的接口，累计认证失败
    for _ in range(3):
        c.get('/api/me', headers={'Authorization': 'Bearer ' + 'x' * 2048})
    assert env['app'].dual_auth.is_blacklisted('127.0.0.1')
    assert env['app'].dual_auth.is_soft_blacklisted('127.0.0.1'), '认证失败应是软封禁'
    # 软封禁下登录接口仍可达（能自救）
    assert c.get('/api/challenge').status_code == 200


def test_frontend_decoy_get_is_benign(env):
    """前端主动调用型诱饵 GET 记良性，不拉黑、不写审计、不进画像"""
    _clean(env)
    c = env['client']
    hp = env['hp']
    before = hp.get_stats()
    for _ in range(10):
        r = c.get('/api/internal/telemetry')
        assert r.status_code == 200
    after = hp.get_stats()
    assert after['total_hits'] == before['total_hits'], '良性调用被记成攻击命中'
    assert after['benign_hits'] == before['benign_hits'] + 10
    assert not env['app'].dual_auth.is_blacklisted('127.0.0.1')
