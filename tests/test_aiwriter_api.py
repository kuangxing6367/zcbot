"""AI 智能体 Web 接口：会话与改动（含真实回滚）。

断言尽量落到磁盘与真实 changeset：接口说「已回滚」不算数，
文件真变回去才算。
"""
import os
import shutil
import tempfile

import pytest

from core_plugins.aiwriter import changeset


class FakeAgentService:
    """替身服务：会话走内存，改动走真实 changeset（这样回滚能真验）。"""

    def __init__(self, ws):
        self.ws = ws
        self.dir = os.path.join(ws, '.changes')
        self.active = 'terminal'
        self._sessions = [{'key': 'terminal', 'name': 'terminal', 'turns': 1,
                           'updated': 10, 'created': 10, 'model': '', 'mode': ''}]
        self.last_change_id = None

    def workspace(self):
        return self.ws

    def active_session(self):
        return self.active

    def sessions(self):
        return self._sessions

    def history(self, key):
        return [{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '在'}]

    def new_session(self, name=None):
        key = name or '会话2'
        self._sessions.append({'key': key, 'name': key})
        return True, key

    def set_session(self, key):
        self.active = key
        return True, key

    def rename_session(self, key, name):
        return True, name

    def delete_session(self, key):
        self._sessions = [s for s in self._sessions if s['key'] != key]
        return True, '已删除会话：%s' % key

    def changes(self, limit=50):
        return changeset.list_changes(self.dir, limit=limit)

    def change(self, cid):
        return changeset.load_change(self.dir, cid)

    def rollback(self, cid):
        return changeset.rollback(self.dir, cid)


@pytest.fixture(scope='module')
def env():
    import bcrypt

    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_aiwriter_api_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'c.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "  honeypot:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    try:
        fw.db.execute("INSERT INTO admin_users (username, password_hash, role, is_active) "
                      "VALUES ('ops3', %s, 'admin', 1)", (pw,))
    except Exception:
        fw.db.execute("UPDATE admin_users SET password_hash=%s WHERE username='ops3'", (pw,))

    ws = os.path.join(tmp, 'ws')
    os.makedirs(ws, exist_ok=True)
    svc = FakeAgentService(ws)
    fw.services.register('aiwriter', svc)

    app = create_web_app(fw)

    sup = app.test_client()
    r = sup.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200
    sup.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r.get_json()['data']['token']

    ops = app.test_client()
    r2 = ops.post('/api/login', json={'username': 'ops3', 'password': 'admin123'})
    ops.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r2.get_json()['data']['token']

    yield {'fw': fw, 'sup': sup, 'ops': ops, 'svc': svc, 'ws': ws, 'db_path': db_path}

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


# ── 会话 ───────────────────────────────────────────────────────────────
def test_sessions_list(env):
    r = env['sup'].get('/api/aiwriter/sessions')
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['active'] == 'terminal'
    assert data['sessions'][0]['key'] == 'terminal'
    assert data['workspace'] == env['ws']


def test_sessions_require_super(env):
    """diff 里就是源码，非超管一律拒绝——不能只靠前端藏按钮。"""
    assert env['ops'].get('/api/aiwriter/sessions').status_code == 403
    assert env['ops'].get('/api/aiwriter/changes').status_code == 403


def test_session_new_activate_delete(env):
    r = env['sup'].post('/api/aiwriter/sessions', json={'name': '写个插件'})
    assert r.status_code == 200
    key = r.get_json()['data']['key']
    assert key == '写个插件'

    r = env['sup'].post('/api/aiwriter/sessions/%s/activate' % key)
    assert r.status_code == 200
    assert env['svc'].active_session() == key

    r = env['sup'].delete('/api/aiwriter/sessions/%s' % key)
    assert r.status_code == 200
    assert all(s['key'] != key for s in env['svc'].sessions())


def test_session_history(env):
    r = env['sup'].get('/api/aiwriter/sessions/terminal')
    assert r.status_code == 200
    msgs = r.get_json()['data']['messages']
    assert [m['role'] for m in msgs] == ['user', 'assistant']


def test_missing_service_is_503(env):
    """插件没启用时要给「去启用」的指引，而不是 500 让前端以为崩了。"""
    saved = env['fw'].services.get('aiwriter')
    try:
        env['fw'].services.remove('aiwriter')
        r = env['sup'].get('/api/aiwriter/changes')
        assert r.status_code == 503
        assert 'aiwriter' in r.get_json()['msg']
    finally:
        env['fw'].services.register('aiwriter', saved)


# ── 改动 ───────────────────────────────────────────────────────────────
def test_changes_list_and_detail_then_rollback(env):
    path = os.path.join(env['ws'], 'target.py')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write('原始内容\n')

    j = changeset.ChangeJournal(env['svc'].dir, root=env['ws'], session='terminal')
    j.record(path, changeset.OP_WRITE, '原始内容\n', 'AI 改过的内容\n')
    cid = j.save()
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write('AI 改过的内容\n')

    r = env['sup'].get('/api/aiwriter/changes')
    assert r.status_code == 200
    rows = r.get_json()['data']
    assert [x['id'] for x in rows] == [cid]

    r = env['sup'].get('/api/aiwriter/changes/%s' % cid)
    assert r.status_code == 200
    detail = r.get_json()['data']
    assert '-原始内容' in detail['files'][0]['diff']
    assert '+AI 改过的内容' in detail['files'][0]['diff']

    r = env['sup'].post('/api/aiwriter/changes/%s/rollback' % cid)
    assert r.status_code == 200
    assert r.get_json()['data']['status'] == changeset.STATUS_ROLLED_BACK

    with open(path, encoding='utf-8') as f:
        assert f.read() == '原始内容\n'


def test_change_detail_unknown_id_is_404(env):
    r = env['sup'].get('/api/aiwriter/changes/does-not-exist')
    assert r.status_code == 404


def test_rollback_rejects_path_traversal_id(env):
    """编号直接拼文件名，必须挡住 ../。"""
    r = env['sup'].post('/api/aiwriter/changes/..%2F..%2Fetc%2Fpasswd/rollback')
    assert r.status_code in (400, 404)
