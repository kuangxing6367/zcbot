"""v1.29 新增能力的运行期契约测试：管理员管理 / Web 聊天 / 富媒体 / 遥测。

用真实 Framework + SQLite 临时库建表，create_web_app 挂全部路由，
用 test_client 发真请求；不 start()，不拉起网络与后台线程。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bcrypt
import pytest

from framework.api.webapp import create_web_app
from framework.core import Framework


@pytest.fixture(scope='module')
def env():
    tmp = tempfile.mkdtemp(prefix='zcbot_v129_')
    cfg = os.path.join(tmp, 'config.yaml')
    db = os.path.join(tmp, 'v129.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db}\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n"
            "core_plugins:\n  onebot_adapter: false\n  webui: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    fw.db.execute(
        "INSERT INTO admin_users (username, password_hash, role, is_active) "
        "VALUES (%s, %s, 'admin', 1)", ('normaladmin', pw))
    app = create_web_app(fw)
    data = {'fw': fw, 'client': app.test_client(), 'tmp': tmp, 'db': db}
    yield data
    try:
        fw._db_executor.shutdown(wait=False)
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _login(c, u='admin', p='admin123'):
    r = c.post('/api/login', json={'username': u, 'password': p})
    if r.status_code == 200:
        return r.get_json().get('data', {}).get('token')
    return None


def _hdr(token):
    return {'Authorization': 'Bearer ' + token}


def test_admin_update_and_password_reset(env):
    c = env['client']
    token = _login(c)
    assert token
    h = _hdr(token)

    row = env['fw'].db.query_one("SELECT id FROM admin_users WHERE username = 'normaladmin'")
    assert row
    rid = row['id']

    r = c.put(f'/api/admins/{rid}', json={'username': 'renamed', 'role': 'admin',
                                          'is_active': False}, headers=h)
    assert r.status_code == 200, r.get_json()
    assert c.post('/api/login', json={'username': 'renamed', 'password': 'admin123'}).status_code != 200

    r = c.post(f'/api/admins/{rid}/password', json={'password': 'newpass123'}, headers=h)
    assert r.status_code == 200, r.get_json()

    r = c.put(f'/api/admins/{rid}', json={'username': 'renamed', 'role': 'admin',
                                          'is_active': True}, headers=h)
    assert r.status_code == 200
    assert _login(c, 'renamed', 'newpass123')


def test_cannot_disable_self_or_last_super(env):
    c = env['client']
    h = _hdr(_login(c))
    me = env['fw'].db.query_one("SELECT id FROM admin_users WHERE username = 'admin'")
    r = c.put(f'/api/admins/{me["id"]}', json={'username': 'admin', 'role': 'admin',
                                              'is_active': True}, headers=h)
    assert r.status_code == 400
    assert '不能降级' in r.get_json()['msg']


def test_webchat_tables_and_api(env):
    c = env['client']
    h = _hdr(_login(c))
    assert c.get('/api/webchat/messages', headers=h).status_code == 200
    r = c.post('/api/webchat/send', json={'text': 'hello', 'user_id': 123}, headers=h)
    assert r.status_code == 200
    rows = env['fw'].db.query("SELECT direction, content FROM webchat_messages")
    assert any(x['direction'] == 'out' and x['content'] == 'hello' for x in rows)


def test_media_registry_api(env):
    c = env['client']
    h = _hdr(_login(c))
    r = c.get('/api/media/messages?limit=10', headers=h)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert 'voice' in body['known_types']
    assert 'file' in body['known_types']


def test_telemetry_defaults_off(env):
    c = env['client']
    h = _hdr(_login(c))
    r = c.get('/api/telemetry/status', headers=h)
    assert r.status_code == 200
    d = r.get_json()['data']
    assert d['enabled'] is False
    assert d['interval_days'] == 15
    assert d['notice']
    assert len(d['instance_id']) == 36


def test_aiwriter_chat_guard_when_plugin_disabled(env):
    c = env['client']
    h = _hdr(_login(c))
    r = c.post('/api/aiwriter/chat', json={'text': 'hi'}, headers=h)
    assert r.status_code == 503
    assert 'aiwriter' in r.get_json()['msg']
