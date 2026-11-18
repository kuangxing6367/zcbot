"""Web 终端 / 智能体桥接回归。

`/api/console/*` 不自己实现执行器，而是直接调用 `ConsoleServer.run_command` /
`run_agent` —— 这样 Web 终端与运维终端的能力必然一致，不会各自漂移。
这组用例锁住：
- 控制台未启动时明确 503（不是 500，也不是静默 404 掉进蜜罐）；
- 命令 / 智能体的输出原样透传，且只在超管可用；
- 执行异常转成 JSON 错误而不是把栈抛给前端。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest


class FakeServer:
    def __init__(self):
        self.calls = []
        # 与真实 ConsoleServer 对齐：run_agent 后由调用方读这个属性拿改动编号
        self.last_agent_change = None

    def run_command(self, command):
        self.calls.append(('cmd', command))
        if command == 'boom':
            raise RuntimeError('炸了')
        return f'output of {command}\n'

    def run_agent(self, text, session=None):
        self.calls.append(('agent', text, session))
        self.last_agent_change = 'cs-test-0001'
        return '[tool] read({"path": "a.py"})\n  -> ok: 12 bytes\n已改好 a.py'


@pytest.fixture(scope='module')
def env():
    import bcrypt

    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_console_')
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
                      "VALUES ('ops2', %s, 'admin', 1)", (pw,))
    except Exception:
        fw.db.execute("UPDATE admin_users SET password_hash=%s WHERE username='ops2'", (pw,))
    app = create_web_app(fw)

    sup = app.test_client()
    r = sup.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200
    sup.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r.get_json()['data']['token']

    ops = app.test_client()
    r2 = ops.post('/api/login', json={'username': 'ops2', 'password': 'admin123'})
    ops.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r2.get_json()['data']['token']

    yield {'fw': fw, 'sup': sup, 'ops': ops, 'db_path': db_path}

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


def test_commands_list_available(env):
    r = env['sup'].get('/api/console/commands')
    assert r.status_code == 200
    body = r.get_json()
    assert body['code'] == 0
    assert isinstance(body['data'], list)


def test_exec_without_console_is_503(env):
    env['fw'].console_server = None
    r = env['sup'].post('/api/console/exec', json={'command': 'status'})
    assert r.status_code == 503
    assert '控制台' in r.get_json()['msg']


def test_exec_passes_output_through(env):
    env['fw'].console_server = FakeServer()
    r = env['sup'].post('/api/console/exec', json={'command': 'plugin list'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['code'] == 0
    assert 'output of plugin list' in body['data']['output']


def test_exec_exception_becomes_json_error(env):
    env['fw'].console_server = FakeServer()
    r = env['sup'].post('/api/console/exec', json={'command': 'boom'})
    assert r.status_code == 500
    assert '炸了' in r.get_json()['msg']


def test_agent_returns_tool_trace(env):
    srv = FakeServer()
    env['fw'].console_server = srv
    r = env['sup'].post('/api/console/agent', json={'text': '改一下 a.py'})
    assert r.status_code == 200
    body = r.get_json()['data']
    assert '[tool] read(' in body['output'] and '已改好' in body['output']
    # 本轮改动编号要回给前端，否则「改完自动摊开 diff」无从触发
    assert body['change'] == 'cs-test-0001'


def test_agent_passes_session_through(env):
    """指定会话必须传到底层服务——否则「在哪个会话里说」形同虚设。"""
    srv = FakeServer()
    env['fw'].console_server = srv
    r = env['sup'].post('/api/console/agent', json={'text': '继续', 'session': '写个插件'})
    assert r.status_code == 200
    assert srv.calls[-1] == ('agent', '继续', '写个插件')
    assert r.get_json()['data']['session'] == '写个插件'


def test_requires_super(env):
    env['fw'].console_server = FakeServer()
    assert env['ops'].post('/api/console/exec', json={'command': 'status'}).status_code == 403
    assert env['ops'].post('/api/console/agent', json={'text': 'x'}).status_code == 403


def test_empty_input_rejected(env):
    env['fw'].console_server = FakeServer()
    assert env['sup'].post('/api/console/exec', json={'command': '  '}).status_code == 400
    assert env['sup'].post('/api/console/agent', json={'text': ''}).status_code == 400
