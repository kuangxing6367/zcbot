"""前端探测端点的稳定契约 + 操作审计落库。

两组回归：

1. **stub 端点**：WebUI 页面加载时会无条件 `POST /api/session/handshake`
   和 `GET /api/plugins/<名>/status`。这两个路径原本只在特定开关打开时才注册，
   默认配置下直接落进蜜罐的 `/api/<path>` 兜底 —— 被判「未知路径探测」→ 拉黑 IP
   + tarpit 600ms，表现为「随机转圈/白屏」，安全看板 TOP IP 还是自己。
   现在无论开关状态都返回明确 JSON（200），前端不再反复重试，也不会再命中诱饵。

2. **操作审计**：``audit_logs`` 表此前只记 Web 管理面操作（登录、改配置）。
   消息侧的命令执行 / 权限拒绝 / 黑名单拦截完全没有留痕，出问题只能翻运行日志。
   这里锁住 AuditWriter 的落库结果与 router 的调用点。

直接运行：PYTHONPATH=. python tests/test_api_stubs_and_audit.py
或：PYTHONPATH=. python -m pytest tests/test_api_stubs_and_audit.py -q
"""
import asyncio
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, '.')


@pytest.fixture(scope='module')
def env():
    import bcrypt

    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_stub_audit_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'stub.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'db-real-pw'\n"
            "onebot:\n  access_token: 'atk-real-value'\n  listen_port: 6831\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n  token_timeout: 3600\n"
            "framework:\n  audit:\n    enabled: true\n    command_events: true\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    # 框架初始化会建默认 admin，这里只把密码统一成测试口令（不重复插入）
    try:
        fw.db.execute(
            "INSERT INTO admin_users (username, password_hash, role, is_active) "
            "VALUES (%s, %s, 'super', 1)", ('admin', pw))
    except Exception:
        fw.db.execute(
            "UPDATE admin_users SET password_hash = %s, role = 'super', is_active = 1 "
            "WHERE username = 'admin'", (pw,))
    app = create_web_app(fw)
    client = app.test_client()

    yield {'fw': fw, 'app': app, 'client': client, 'db_path': db_path, 'tmp': tmp}

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


def _token(client):
    r = client.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200, r.data[:200]
    return r.get_json()['data']['token']


# ── 1. stub 端点 ────────────────────────────────────────────

def test_session_handshake_registered_when_disabled(env):
    """会话令牌默认关闭，但握手端点必须存在并明确回 enabled=false。"""
    r = env['client'].post('/api/session/handshake')
    assert r.status_code == 200, f'握手端点不存在（会落进蜜罐兜底）: {r.status_code}'
    body = r.get_json()
    assert body['code'] == 0
    assert body['data']['enabled'] is False
    assert body['data']['master'] is None


def test_session_confirm_registered_when_disabled(env):
    r = env['client'].post('/api/session/confirm', json={'tokens': []})
    assert r.status_code == 200
    assert r.get_json()['data']['enabled'] is False


def test_frontend_probe_does_not_hit_honeypot(env):
    """两个探测端点被真实路由接住，蜜罐威胁计数与黑名单都不该增长。"""
    from framework.api import honeypot

    before = honeypot._stats['total_hits']
    before_benign = honeypot._stats['benign_hits']
    c = env['client']
    c.post('/api/session/handshake')
    tok = _token(c)
    c.get('/api/plugins/image_renderer/status',
          headers={'Authorization': f'Bearer {tok}'})
    assert honeypot._stats['total_hits'] == before, '正常前端探测被记成了攻击'
    assert honeypot._stats['benign_hits'] == before_benign, '不该再走良性兜底分支'


def test_plugin_status_contract(env):
    """未安装的插件也要给稳定结构，前端不必当异常处理。"""
    tok = _token(env['client'])
    r = env['client'].get('/api/plugins/image_renderer/status',
                          headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['code'] == 0
    data = body['data']
    assert data['name'] == 'image_renderer'
    assert data['installed'] is False
    assert data['loaded'] is False
    assert data['status'] == 'not_installed'


def test_plugin_status_requires_auth(env):
    # 用全新 client：同一个 test_client 会把登录后的 cookie 带过来
    c = env['app'].test_client()
    r = c.get('/api/plugins/image_renderer/status')
    assert r.status_code in (401, 444), '状态端点不能裸奔'


def test_unknown_api_still_goes_to_decoy(env):
    """兜底诱饵没被废掉：真未知路径仍按攻击处理。"""
    from framework.api import honeypot

    before = honeypot._stats['total_hits']
    env['client'].get('/api/definitely-not-a-route-xyz')
    assert honeypot._stats['total_hits'] == before + 1


# ── 2. 操作审计 ────────────────────────────────────────────

async def test_audit_writer_persists_rows(env):
    """命令执行审计要真的落进 audit_logs，而不是只写运行日志。"""
    from framework.core.audit_writer import AuditWriter

    fw = env['fw']
    w = AuditWriter(fw, flush_interval=0.05)
    w.record('command_exec', actor='group:1/user:2', target_type='command',
             target_name='/help', detail={'plugin': 'demo', 'handler': 'on_help'})
    w.record('command_denied', actor='user:3', target_type='command',
             target_name='/admin', detail={'require': 'admin'},
             result='failure', error='权限不足')
    await w._flush_once()

    rows = fw.db.query(
        "SELECT admin_name, action, target_type, target_name, detail, result, error_message "
        "FROM audit_logs WHERE action IN ('command_exec', 'command_denied') "
        "ORDER BY id ASC")
    assert len(rows) == 2, rows
    assert rows[0]['action'] == 'command_exec'
    assert rows[0]['admin_name'] == 'group:1/user:2'
    assert rows[0]['target_name'] == '/help'
    assert rows[0]['result'] == 'success'
    assert '"plugin": "demo"' in rows[0]['detail']
    assert rows[1]['result'] == 'failure'
    assert rows[1]['error_message'] == '权限不足'


async def test_audit_writer_drops_text_when_disabled(env):
    """record_message_text=false 时正文不入库（审计表不是聊天记录表）。"""
    from framework.core.audit_writer import AuditWriter

    fw = env['fw']
    w = AuditWriter(fw)
    w.record_text = False
    ev = SimpleNamespace(adapter='onebot', bot_name='default', user_id=7, group_id=8)
    w.command_executed(ev, 'demo', 'on_help', '/help', message='我是机密内容')
    await w._flush_once()

    row = fw.db.query_one(
        "SELECT detail FROM audit_logs WHERE action='command_exec' "
        "AND admin_name='group:8/user:7'")
    assert row is not None
    assert '我是机密内容' not in (row['detail'] or '')
    assert '"adapter": "onebot"' in row['detail']


class _CollectingAudit:
    """记录调用参数，验证 router 在各分支上确实写了审计。"""

    def __init__(self):
        self.calls = []

    def command_executed(self, ev, plugin, handler, pattern, matched_by='',
                         message='', result='success', error=None):
        self.calls.append(('exec', plugin, handler, pattern, result, error))

    def command_denied(self, ev, plugin, handler, pattern, reason, require='', message=''):
        self.calls.append(('denied', plugin, handler, pattern, require, reason))

    def command_blocked(self, ev, plugin, handler, pattern, message=''):
        self.calls.append(('blocked', plugin, handler, pattern))

    def message_unmatched(self, ev, message=''):
        self.calls.append(('unmatched', message))


def _router_with_audit():
    from framework.messaging.router import MessageRouter

    class _Hooks:
        async def trigger_async(self, *a, **k):
            return []

    class _FW:
        db = None
        stats_writer = None
        hooks = _Hooks()
        audit = _CollectingAudit()

        async def reply_text(self, ev, text):
            self.replied = text

    return MessageRouter(_FW()), _FW()


def _cmd(pattern, handler_name='on_cmd', require_level='', require_perm=''):
    from framework.messaging.router import _RouteCommand
    return _RouteCommand(id=1, pattern=pattern, handler_name=handler_name,
                         require_level=require_level, rx=None, simple=pattern,
                         aliases=[], require_perm=require_perm)


class _Ev:
    bot_name = 'default'
    adapter = 'onebot'
    user_id = 42
    group_id = 7

    def __init__(self, role='member'):
        self.role = role
        self.is_admin = role in ('admin', 'owner')
        self.is_superuser = role == 'owner'

    def has_perm(self, node):
        return True


async def test_router_audits_executed_command():
    from framework.messaging.router_match import _PluginRoute

    called = []

    async def on_cmd(ev, match):
        called.append(match.group(1))

    r, fw = _router_with_audit()
    entry = _PluginRoute(SimpleNamespace(on_cmd=on_cmd), [_cmd('/help')])
    assert await r._match_plugin_commands(entry, _Ev(), '/help', 'demo') is True
    assert called == ['']
    assert fw.audit.calls == [('exec', 'demo', 'on_cmd', '/help', 'success', None)], \
        fw.audit.calls


async def test_router_audits_handler_failure():
    from framework.messaging.router_match import _PluginRoute

    async def on_cmd(ev, match):
        raise RuntimeError('boom')

    r, fw = _router_with_audit()
    entry = _PluginRoute(SimpleNamespace(on_cmd=on_cmd), [_cmd('/help')])
    assert await r._match_plugin_commands(entry, _Ev(), '/help', 'demo') is True
    kind, _plugin, _handler, _pattern, result, error = fw.audit.calls[-1]
    assert kind == 'exec' and result == 'failure'
    assert 'boom' in error


async def test_router_audits_permission_denied():
    from framework.messaging.router_match import _PluginRoute

    async def on_cmd(ev, match):
        raise AssertionError('权限不足不该执行')

    r, fw = _router_with_audit()
    entry = _PluginRoute(SimpleNamespace(on_cmd=on_cmd),
                         [_cmd('/admin', require_level='admin')])
    assert await r._match_plugin_commands(entry, _Ev('member'), '/admin', 'demo') is True
    assert fw.audit.calls[0][0] == 'denied'
    assert fw.audit.calls[0][4] == 'admin'


async def test_router_audits_blacklist_block():
    from framework.messaging.router_match import _PluginRoute

    async def on_cmd(ev, match):
        raise AssertionError('黑名单不该执行')

    r, fw = _router_with_audit()
    entry = _PluginRoute(SimpleNamespace(on_cmd=on_cmd), [_cmd('/help')])
    assert await r._match_plugin_commands(entry, _Ev('blacklist'), '/help', 'demo') is True
    assert fw.audit.calls[0][0] == 'blocked'


async def test_router_does_not_audit_when_not_matched():
    from framework.messaging.router_match import _PluginRoute

    async def on_cmd(ev, match):
        raise AssertionError('不该执行')

    r, fw = _router_with_audit()
    entry = _PluginRoute(SimpleNamespace(on_cmd=on_cmd), [_cmd('/help')])
    assert await r._match_plugin_commands(entry, _Ev(), '/nothing', 'demo') is False
    assert fw.audit.calls == []


if __name__ == '__main__':
    import traceback

    failed = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith('test_')]
    # fixture 依赖：脚本模式下按顺序手动建一次 env
    env_gen = env.__wrapped__() if hasattr(env, '__wrapped__') else None
    _env = next(env_gen) if env_gen else None
    try:
        for name, fn in tests:
            try:
                if asyncio.iscoroutinefunction(fn):
                    asyncio.run(fn(_env))
                else:
                    fn(_env)
                print(f'PASS {name}: {(fn.__doc__ or "").strip()}')
            except Exception:
                failed += 1
                print(f'FAIL {name}')
                traceback.print_exc()
    finally:
        if env_gen:
            try:
                next(env_gen)
            except StopIteration:
                pass
    print(f'\n{len(tests) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
