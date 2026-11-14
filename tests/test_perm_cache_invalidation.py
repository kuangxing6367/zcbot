"""权限写操作后进程内权限缓存必须立刻失效。

背景：命令分发热路径的性能修复把角色/权限判定缓存到了进程内（TTL 5s）。
如果 Web 上改了权限却要等 TTL 才生效，管理员看到的就是「改了没用」——
这类问题最难被自查发现，必须用真实 HTTP 写请求钉住。
"""
import os
import shutil
import sys
import tempfile

import bcrypt
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.api.webapp import create_web_app
from framework.core import Framework


@pytest.fixture(scope='module')
def env():
    tmp = tempfile.mkdtemp(prefix='zcbot_perm_cache_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'perm.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    try:
        fw.db.execute(
            "INSERT INTO admin_users (username, password_hash, role, is_active) "
            "VALUES (%s, %s, 'super', 1)", ('root', pw))
    except Exception:
        pass
    client = create_web_app(fw).test_client()
    yield {'fw': fw, 'client': client}
    try:
        fw._db_executor.shutdown(wait=False)
    except Exception:
        pass
    for p in (db_path, db_path + '-wal', db_path + '-shm'):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def _login(c):
    r = c.post('/api/login', json={'username': 'root', 'password': 'admin123'})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']['token']


def test_permission_write_invalidates_router_cache(env):
    fw = env['fw']
    router = getattr(fw, 'router', None)
    if router is None:
        pytest.skip('当前 Framework 未创建 router')

    cache = getattr(router, '_perm_cache', None)
    if cache is None:
        pytest.skip('当前 router 无进程内权限缓存（功能未启用）')

    # 先塞一条假缓存，模拟「已经判定过某人权限」
    cache.put(('12345', 'default', '', ''), 'member', object())
    assert len(cache) > 0

    token = _login(env['client'])
    r = env['client'].post(
        '/api/perm/groups', json={'name': 'cacheprobe', 'display_name': '探针'},
        headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.get_json()

    assert len(cache) == 0, '权限组创建成功后 router 权限缓存必须被清空'


def test_read_only_request_does_not_clear_cache(env):
    """读接口不该顺手清缓存（否则缓存等于没有）。"""
    fw = env['fw']
    router = getattr(fw, 'router', None)
    if router is None:
        pytest.skip('当前 Framework 未创建 router')
    cache = getattr(router, '_perm_cache', None)
    if cache is None:
        pytest.skip('当前 router 无进程内权限缓存')

    token = _login(env['client'])
    cache.put(('999', 'default', '', ''), 'member', object())
    r = env['client'].get('/api/perm/groups',
                          headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200
    assert len(cache) > 0, 'GET 请求不应清空权限缓存'
