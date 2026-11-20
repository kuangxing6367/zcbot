"""WebUI 应急管理员端到端回归：DB 故障时仍能登录、token 仍能过校验。

复用真实 Framework + Flask test_client 发真 HTTP，但把应急凭据目录指到临时路径、
把 db.query_one 换成抛异常的替身来模拟「SQLite 损坏」。验证：

  · DB 正常时普通登录、普通 token 校验完全不受影响；
  · DB 损坏时应急账号能登录，返回的应急 token 在后续请求里放行；
  · 应急 token 在 DB 彻底不可用时也不查库（校验链路不碰 db.query_one）；
  · 密码错误、非应急账号在 DB 损坏时仍被拒。
"""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.api import emergency_admin as ema
from framework.api.webapp import create_web_app
from framework.core import Framework


def _write_config(tmp_path):
    cfg = tmp_path / 'config.yaml'
    db_path = (tmp_path / 'eme.db').as_posix()
    cfg.write_text(
        f"database:\n  type: sqlite\n  path: {db_path}\n"
        "plugin:\n  heartbeat_interval: 60\n"
        "log:\n  level: ERROR\n"
        "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
        "web:\n  host: 127.0.0.1\n  port: 0\n  session_timeout: 3600\n"
        "core_plugins:\n"
        "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
        "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
        "  telegram: false\n  discord: false\n  session: false\n"
        "  scheduler: false\n  image_renderer: false\n",
        encoding='utf-8',
    )
    return cfg, db_path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # 应急凭据落临时目录，绝不碰真实 data/
    eme_dir = tmp_path / 'eme_data'
    eme_dir.mkdir()
    monkeypatch.setattr(ema, 'state_dir', lambda: str(eme_dir))

    cfg_path, db_path = _write_config(tmp_path)
    fw = Framework(config_path=str(cfg_path), role='standard')
    app = create_web_app(fw)
    ctx = {
        'fw': fw, 'client': app.test_client(), 'db_path': db_path,
        'eme_dir': str(eme_dir), 'tmp': str(tmp_path),
    }
    ema.ensure_config(str(eme_dir), announce=False)
    yield ctx
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
    shutil.rmtree(str(tmp_path), ignore_errors=True)


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def _break_db(monkeypatch, fw):
    def boom(*a, **k):
        raise RuntimeError('database disk image is malformed')
    monkeypatch.setattr(fw.db, 'query_one', boom)


def test_normal_login_unaffected(env):
    r = env['client'].post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200, r.get_json()
    token = r.get_json()['data']['token']
    assert not r.get_json()['data'].get('emergency')
    me = env['client'].get('/api/me', headers=_auth(token))
    assert me.status_code == 200
    assert not me.get_json()['data'].get('emergency')


def test_emergency_login_when_db_broken(env, monkeypatch):
    _break_db(monkeypatch, env['fw'])
    cfg = ema.load_config(env['eme_dir'])
    r = env['client'].post('/api/login', json={
        'username': cfg['username'], 'password': 'irrelevant'})
    # 密码错误时，即便 DB 坏也不能放行
    assert r.status_code in (401, 500)
    assert r.get_json().get('code') != 0


def test_emergency_login_ok_and_token_survives_db_broken(env, monkeypatch):
    cfg, pwd = ema.reset_password(env['eme_dir'], announce=False)
    _break_db(monkeypatch, env['fw'])

    r = env['client'].post('/api/login', json={
        'username': cfg['username'], 'password': pwd})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()['data']
    assert data['emergency'] is True
    assert data['role'] == 'super'
    token = data['token']

    # 后续请求用应急 token；_verify_token 必须不查库即可放行
    me = env['client'].get('/api/me', headers=_auth(token))
    assert me.status_code == 200, me.get_json()
    assert me.get_json()['data']['emergency'] is True


def test_file_admin_fallback_after_db_password_failure(env):
    """DB 里有同名账号但口令不匹配时，文件管理员凭据应作为第二来源生效。"""
    ema.add_admin('admin', 'file-only-pass', base=env['eme_dir'])
    r = env['client'].post('/api/login', json={
        'username': 'admin', 'password': 'file-only-pass'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['data']['emergency'] is True
    assert r.get_json()['data']['username'] == 'admin'


def test_file_admin_login_after_admin_table_dropped(env):
    """管理员表被删（补建成空表 / 缺表）后，文件管理员仍能登进 WebUI。

    这是「数据库侧塌了也不至于完全进不去」的核心保障：DROP admin_users 后
    db.query_one 抛 OperationalError，登录必须回落到 data/admin_emergency.yaml，
    且返回的 token 要能继续通过 /api/me（校验不查库）。
    """
    ema.add_admin('ops', 'file-only-pass-2', base=env['eme_dir'])
    env['fw'].db.execute('DROP TABLE admin_users')

    r = env['client'].post('/api/login', json={
        'username': 'ops', 'password': 'file-only-pass-2'})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()['data']
    assert data['username'] == 'ops'
    assert data['role'] == 'super'
    token = data['token']

    me = env['client'].get('/api/me', headers=_auth(token))
    assert me.status_code == 200, me.get_json()


def test_forged_emergency_token_rejected_even_if_db_ok(env):
    bad = ema.TOKEN_PREFIX + 'forged.payload.deadbeef'
    me = env['client'].get('/api/me', headers=_auth(bad))
    assert me.status_code in (401, 444, 403)


def test_non_emergency_user_rejected_when_db_broken(env, monkeypatch):
    _break_db(monkeypatch, env['fw'])
    r = env['client'].post('/api/login', json={
        'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 500
    assert r.get_json().get('code') == 500


@pytest.mark.parametrize('token_factory', [
    lambda cfg: ema.TOKEN_PREFIX + 'x.y',
    lambda cfg: '',
])
def test_bad_tokens_never_grant_access(env, token_factory):
    cfg = ema.load_config(env['eme_dir'])
    me = env['client'].get('/api/me', headers=_auth(token_factory(cfg)))
    assert me.status_code in (401, 444, 403)
