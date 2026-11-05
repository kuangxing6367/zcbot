# -*- coding: utf-8 -*-
"""P0 安全修复回归：真实 HTTP 行为（不是源码字符串匹配）。

覆盖：普通 admin 不能经 Web 终端执行 shell、不能重启框架、不能读配置文件、
未配 CORS 白名单时不反射任意 Origin。
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

from framework.core import Framework
from framework.api.webapp import create_web_app


@pytest.fixture(scope='module')
def env():
    tmp = tempfile.mkdtemp(prefix='zcbot_p0_sec_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'sec.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n  type: sqlite\n  path: {db}\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n".format(db=db_path)
        )
    fw = Framework(config_path=cfg, role='standard')
    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    try:
        fw.db.execute(
            "INSERT INTO admin_users (username, password_hash, role, is_active) "
            "VALUES (%s, %s, 'admin', 1)", ('normaladmin', pw))
    except Exception:
        pass
    client = create_web_app(fw).test_client()
    yield {'fw': fw, 'client': client, 'tmp': tmp, 'db_path': db_path}
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


def _login(client, username='admin', password='admin123'):
    r = client.post('/api/login', json={'username': username, 'password': password})
    return r.get_json().get('data', {}).get('token') if r.status_code == 200 else None


def test_normal_admin_cannot_run_shell_via_terminal(env):
    """修复前：任何 admin token 可 POST 'shell <任意命令>' 拿到宿主 RCE。"""
    c = env['client']
    token = _login(c, 'normaladmin')
    assert token
    r = c.post('/api/terminal/exec',
               json={'command': 'shell id'},
               headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 403, r.get_json()
    assert '超级管理员' in r.get_json().get('msg', '')


def test_normal_admin_can_run_safe_query_command(env):
    c = env['client']
    token = _login(c, 'normaladmin')
    r = c.post('/api/terminal/exec',
               json={'command': 'help'},
               headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.get_json()


def test_super_can_run_shell(env):
    """超级管理员仍保留终端能力（防止把修法变成全面禁用）。"""
    c = env['client']
    token = _login(c)
    r = c.post('/api/terminal/exec',
               json={'command': 'shell echo zcbot-p0-ok'},
               headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.get_json()


def test_normal_admin_cannot_restart_framework(env):
    c = env['client']
    token = _login(c, 'normaladmin')
    r = c.post('/api/restart', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 403, r.get_json()


def test_normal_admin_cannot_read_sensitive_file(env):
    """文件浏览器允许根=项目根，读写权限必须一致：写要 super，读也要 super。

    用仓库里必然存在的 local.debug.js（已列入 _SENSITIVE_NAMES）而不是
    config.yaml —— 后者被 .gitignore 排除，CI 干净 checkout 里没有，
    请求会先返回 400「文件不存在」而不是 403（本地全绿≠CI过）。
    """
    c = env['client']
    token = _login(c, 'normaladmin')
    r = c.get('/api/files/read?path=core_plugins/webui/web/js/local.debug.js',
              headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 403, (r.status_code, r.get_json())


def test_cors_does_not_reflect_arbitrary_origin(env):
    """未配 cors_allowed_origins 时不得反射任意 Origin + Allow-Credentials。"""
    c = env['client']
    token = _login(c, 'normaladmin')
    r = c.get('/api/me', headers={
        'Authorization': f'Bearer {token}',
        'Origin': 'https://evil.example',
    })
    assert r.headers.get('Access-Control-Allow-Origin') is None
    assert r.headers.get('Access-Control-Allow-Credentials') is None
