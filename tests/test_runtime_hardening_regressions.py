"""本轮加固的回归：内存看门狗 / 插件内存口径 / 日志缓存 / 坏行隔离 / 配置脱敏 / 更新误报。

真实行为验证：前 7 组直接调被改函数，后面用 Flask test_client 走真实路由。
不依赖 Windows 私有内存字段（用 fake proc 注入），Linux CI 同样成立。
路径一律由 __file__ 推导，不依赖 cwd、不写死时区。
"""
import asyncio
import io
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import zipfile
from contextlib import contextmanager
from functools import wraps
from types import SimpleNamespace

import bcrypt
import pytest
from flask import Flask, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.api import framework_update, static_routes
from framework.api.config import _mask_sensitive, _strip_masks
from framework.core.runtime import FrameworkRuntimeMixin
from framework.core.stats_writer import AsyncStatsWriter
from framework.loader.runtime import PluginRuntimeMixin, _estimate_module_size
from framework.log_broker import LogBroker
from framework.memory import process_memory_snapshot


class _Sink(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.rows = []

    def emit(self, record):
        try:
            self.rows.append({'level': record.levelname, 'msg': record.getMessage()})
        except Exception:
            pass

    def text(self):
        return '\n'.join(r['msg'] for r in self.rows)


@pytest.fixture
def sink():
    h = _Sink()
    lg = logging.getLogger('zcbot')
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


# ---------------------------------------------------------------- 内存快照

def test_memory_snapshot_distinguishes_rss_from_commit_bytes():
    """工作集裁剪后 RSS 会假性暴跌，判定必须能拿到私有提交字节。"""
    class _Info:
        rss = 100 * 1048576
        private = 91 * 1048576

    class _Proc:
        def memory_info(self):
            return _Info()

    assert process_memory_snapshot(_Proc()) == (100.0, 91.0)

    class _LinuxInfo:
        rss = 42 * 1048576  # 无 private 字段

    class _LinuxProc:
        def memory_info(self):
            return _LinuxInfo()

    assert process_memory_snapshot(_LinuxProc()) == (42.0, 42.0)


def test_watchdog_keeps_over_limit_after_workingset_trim(monkeypatch, sink):
    """修复前：trim 把 RSS 从 100 压到 1，后续周期再也不判超限（自我抑制）。"""
    from framework.core import runtime as rt

    seq = [(100.0, 91.0), (1.0, 91.0)]  # 清理前 / 清理后，提交内存纹丝不动

    def fake_snapshot(_proc):
        return seq.pop(0) if seq else (1.0, 91.0)

    monkeypatch.setattr(rt, 'process_memory_snapshot', fake_snapshot)
    monkeypatch.setattr(rt, 'trim_memory', lambda: True)

    fake = SimpleNamespace(_running=True, _memory_check_interval=0.01,
                           _memory_limit_mb=65)

    async def run():
        task = asyncio.create_task(FrameworkRuntimeMixin._memory_watchdog_loop(fake))
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    assert getattr(fake, '_sticky_over_mb', 0) > 0, '超限状态必须被钉住'
    assert '对象未释放' in sink.text(), sink.text()
    assert '节省' not in sink.text(), '不得把工作集换出虚报成内存释放'
    assert '提交内存 91.0→91.0MB' in sink.text(), sink.text()


# ------------------------------------------------------------ 插件内存口径

def test_estimate_module_size_sees_payload_hidden_in_container():
    """旧口径 sum(getsizeof(v) for v in vars(module)) 看不见容器里的大对象。"""
    m = types.ModuleType('probe_hidden')
    m.ballast = [bytearray(8 * 1024 * 1024)]
    assert _estimate_module_size(m) >= 7.5

    shallow = sum(v.__sizeof__() for v in vars(m).values()) / 1024 / 1024
    assert shallow < 1.0, '证明这确实是旧口径会漏判的形状'

    m2 = types.ModuleType('probe_bare')
    m2.bare = bytearray(8 * 1024 * 1024)
    assert _estimate_module_size(m2) >= 7.5


def test_plugin_memory_monitor_stops_and_can_restart():
    """修复前 Framework.stop() 后线程仍 alive 并继续打告警。"""
    class _Loader(PluginRuntimeMixin):
        def __init__(self):
            self._memory_monitor_running = False
            self._memory_monitor_stop = threading.Event()
            self._memory_monitor_thread = None
            self._memory_violations = {}
            self._lock = threading.Lock()
            self._loaded_plugins = {}
            self.framework = SimpleNamespace(config={'plugin': {'max_memory_mb': 64}})

    loader = _Loader()
    loader._start_memory_monitor()
    t = loader._memory_monitor_thread
    assert t is not None and t.is_alive()
    loader._stop_memory_monitor()
    assert not t.is_alive()
    assert loader._memory_monitor_running is False

    loader._start_memory_monitor()
    t2 = loader._memory_monitor_thread
    assert t2 is not None and t2.is_alive() and t2 is not t
    loader._stop_memory_monitor()
    assert not t2.is_alive()


# ---------------------------------------------------------------- 日志缓存

def test_log_broker_retains_errors_beyond_ring_capacity():
    b = LogBroker(max_size=50)
    b.log('framework', 'ERROR', '第一条错误')
    for i in range(200):
        b.log('framework', 'INFO', f'洪水{i}')
    b.log('framework', 'ERROR', '第二条错误')

    errs = b.get_logs(level='ERROR', limit=50)
    assert [e['message'] for e in errs] == ['第一条错误', '第二条错误']
    assert b.get_stats()['errors_retained'] == 2
    # 主缓存确实已经被冲掉，说明独立通道是必要的
    assert len(b.get_logs(level=None, limit=500)) == 50
    b.clear()
    assert b.get_logs(level='ERROR', limit=50) == []
    assert b.get_stats()['errors_retained'] == 0


# ------------------------------------------------------------ 坏数据逐行隔离

def test_stats_writer_isolates_bad_row_instead_of_dropping_batch(sink):
    class _DB:
        def __init__(self):
            self.calls = []

        @contextmanager
        def transaction(self):
            yield

        def execute_many(self, sql, rows):
            rows = list(rows)
            self.calls.append(rows)
            if len(rows) > 1:
                raise RuntimeError('OverflowError: Python int too large')
            if rows[0][0] == 2:
                raise RuntimeError('bad row')

    w = AsyncStatsWriter.__new__(AsyncStatsWriter)
    w.db = _DB()
    w._execute_many_resilient('INSERT ...', [(1, 'a'), (2, 'b'), (3, 'c')], '测试批')

    # 1 次整批尝试 + 3 次逐行；坏行之外的 2 行必须落库
    assert len(w.db.calls) == 4
    assert [r[0][0] for r in w.db.calls[1:]] == [1, 2, 3]
    assert '转为逐行隔离' in sink.text()
    assert '成功 2 行，丢弃 1 行' in sink.text()


def test_stats_writer_batch_success_path_keeps_single_transaction():
    class _DB:
        def __init__(self):
            self.calls = []

        @contextmanager
        def transaction(self):
            yield

        def execute_many(self, sql, rows):
            self.calls.append(list(rows))

    w = AsyncStatsWriter.__new__(AsyncStatsWriter)
    w.db = _DB()
    w._execute_many_resilient('INSERT ...', [(1, 'a'), (2, 'b')], '正常批')
    assert len(w.db.calls) == 1 and len(w.db.calls[0]) == 2


# ---------------------------------------------------------------- 配置脱敏

def test_mask_sensitive_only_hits_credential_scalars():
    doc = {
        'database': {'password': 'p@ss', 'host': '127.0.0.1', 'port': 3306},
        'web': {'secret_key': 's3cr3t', 'token_timeout': 3600},
        'onebot': {'access_token': 'atk-123', 'listen_port': 6830},
        'fields': [{'key': 'access_token', 'type': 'password'}],
        'restart_keys': ['listen_port'],
    }
    m = _mask_sensitive(doc)
    assert m['database']['password'] == '***'
    assert m['web']['secret_key'] == '***'
    assert m['onebot']['access_token'] == '***'
    assert m['database']['host'] == '127.0.0.1'
    assert m['web']['token_timeout'] == 3600, 'timeout 不能当凭据打码'
    assert m['fields'][0]['key'] == 'access_token', '字段名不能被打码'
    assert m['restart_keys'] == ['listen_port']
    assert doc['database']['password'] == 'p@ss', '不得就地修改原对象'
    assert _strip_masks({'a': '***', 'b': 2}) == {'b': 2}


def test_static_route_never_serves_debug_credentials():
    """蜜罐禁用时也不得把 local.debug.js 原文直出给未登录访客。"""
    app = Flask(__name__, static_folder=None)
    ctx = SimpleNamespace(
        app=app,
        framework=SimpleNamespace(plugin_loader=SimpleNamespace(get_override_webui=lambda: None)),
        _project_root=lambda: ROOT,
    )
    static_routes.register(ctx)
    c = app.test_client()
    r = c.get('/js/local.debug.js')
    assert r.status_code == 404
    assert 'jwt_secret' not in r.get_data(as_text=True)
    # 正常静态文件不受影响：挑一个产物目录里真实存在的 js 请求，必须 200
    js_dir = os.path.join(ROOT, 'core_plugins', 'webui', 'web', 'js')
    others = [n for n in os.listdir(js_dir)
              if n.endswith('.js') and n != 'local.debug.js']
    assert others, '发布产物目录应有构建后的 js'
    assert c.get('/js/' + others[0]).status_code == 200


# ------------------------------------------------------- 真实 Web 路由鉴权

@pytest.fixture(scope='module')
def web_env():
    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_hardening_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'harden.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'db-real-pw'\n"
            "onebot:\n  access_token: 'atk-real-value'\n  listen_port: 6830\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n  token_timeout: 3600\n"
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
            "VALUES (%s, %s, 'admin', 1)", ('normaladmin', pw))
    except Exception:
        pass
    client = create_web_app(fw).test_client()
    yield {'fw': fw, 'client': client, 'tmp': tmp, 'db_path': db_path, 'cfg': cfg}
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


def test_normal_admin_gets_masked_config(web_env):
    c = web_env['client']
    token = _login(c, 'normaladmin')
    assert token
    r = c.get('/api/config/yaml', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['masked'] is True
    assert body['data']['database']['password'] == '***'
    assert body['data']['onebot']['access_token'] == '***'
    assert body['data']['web']['token_timeout'] == 3600
    assert 'db-real-pw' not in str(body)
    assert 'atk-real-value' not in str(body)


def test_super_admin_still_reads_plaintext_config(web_env):
    c = web_env['client']
    token = _login(c)
    assert token
    r = c.get('/api/config/yaml', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['masked'] is False
    assert body['data']['database']['password'] == 'db-real-pw'


def test_connection_config_masked_for_normal_admin(web_env):
    c = web_env['client']
    token = _login(c, 'normaladmin')
    r = c.get('/api/connection', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200
    body = r.get_json()['data']
    assert body['masked'] is True
    assert 'atk-real-value' not in str(body)


def test_put_mask_placeholder_does_not_overwrite_config(web_env):
    """前端把脱敏表单整页回传时，不能把 '***' 写成真实配置。"""
    c = web_env['client']
    token = _login(c)
    before = open(web_env['cfg'], encoding='utf-8').read()
    r = c.put('/api/config/yaml/onebot', json={'data': {'access_token': '***'}},
              headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200, r.get_json()
    assert '已忽略' in r.get_json()['msg']
    after = open(web_env['cfg'], encoding='utf-8').read()
    assert after == before
    assert 'atk-real-value' in after


# ------------------------------------------------------- 更新依赖失败如实回报

def _build_update_client(install_fn, audit_calls):
    tmp = tempfile.mkdtemp(prefix='zcbot_fwupd_')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('zcbot-main/requirements.txt', '# test\n')
        z.writestr('zcbot-main/framework/marker.txt', 'x')
    zip_path = os.path.join(tmp, 'update.zip')
    with open(zip_path, 'wb') as f:
        f.write(buf.getvalue())

    app = Flask(__name__)
    app.config['TESTING'] = True

    def _download(_urls):
        dst = os.path.join(tmp, 'download.zip')
        shutil.copy(zip_path, dst)
        return dst

    def _audit(*args, **kwargs):
        audit_calls.append(args)

    def _auth(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            request.admin = {'id': 1, 'username': 'tester', 'role': 'super'}
            return fn(*a, **kw)
        return wrapper

    ctx = SimpleNamespace(
        app=app, framework=SimpleNamespace(), require_auth=_auth, require_super=_auth,
        audit_log=_audit, _project_root=lambda: tmp,
        _get_framework_local_version=lambda: '1.20.2',
        _install_new_requirements=install_fn,
        _parse_version_tuple=lambda v: None,
        _latest_release_tag=lambda repo: '',
        _download_zip_file=_download,
        _github_url_candidates=lambda u: [u],
    )
    framework_update.register(ctx)
    return app.test_client(), tmp


def test_update_reports_dependency_failure_not_success():
    def _boom():
        raise RuntimeError('依赖安装失败: pip 超时')

    calls = []
    client, tmp = _build_update_client(_boom, calls)
    try:
        r = client.post('/api/framework/update', json={})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['data']['dependency_status'] == 'failed'
        assert '依赖安装失败' in body['msg']
        assert '更新成功' not in body['msg']
        assert calls and calls[-1][6] == 'partial', calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_success_path_still_records_success():
    calls = []
    client, tmp = _build_update_client(lambda: None, calls)
    try:
        r = client.post('/api/framework/update', json={})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['data']['dependency_status'] == 'ok'
        assert calls and calls[-1][6] == 'success', calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ /readyz 误报

def test_db_probe_rejects_swallowed_select_errors():
    """旧实现只 try/except，不校验 SELECT 1 返回值：吞异常返回 None 也报 ready。"""
    from framework.api.observability import db_probe

    class _DB:
        def __init__(self, value):
            self.value = value

        def scalar(self, sql):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    for bad in (None, [], {}, [[]], '', 'x', False, 0):
        ok, detail = db_probe(SimpleNamespace(db=_DB(bad)))
        assert not ok, (bad, detail)
    for good in (1, '1', b'1', 1.0, [1], {'1': 1}):
        ok, detail = db_probe(SimpleNamespace(db=_DB(good)))
        assert ok, (good, detail)

    ok, detail = db_probe(SimpleNamespace(db=_DB(RuntimeError('db down'))))
    assert not ok and 'db down' in detail

    class _NoScalar:
        def query_one(self, sql):
            return {'1': 1}

    class _NoScalarBad:
        def query_one(self, sql):
            return None

    assert db_probe(SimpleNamespace(db=_NoScalar()))[0] is True
    assert db_probe(SimpleNamespace(db=_NoScalarBad()))[0] is False


def test_readyz_real_http_503_when_db_swallows_errors(web_env):
    """真实 HTTP：数据库吞异常返回 None 时 /readyz 必须 503，不能误报 ready。"""
    fw, c = web_env['fw'], web_env['client']
    fw._running, fw._ready = True, True
    original = fw.db
    try:
        assert c.get('/readyz').status_code == 200, '先确认正常数据库下确实 ready'

        class _SilentDB:
            def scalar(self, sql):
                return None  # 模拟调试/降级引擎吞掉查询异常

        fw.db = _SilentDB()
        r = c.get('/readyz')
        assert r.status_code == 503, r.get_json()
        assert r.get_json()['checks']['database']['ok'] is False
    finally:
        fw.db = original
        fw._running, fw._ready = False, False


# ------------------------------------------------ API Key 摘要化（v11）

def test_api_key_hash_helpers():
    """摘要带前缀、常量时间比对、明文不会被当成摘要。"""
    from framework.token_hash import (
        API_KEY_HASH_PREFIX,
        api_key_matches,
        hash_api_key,
        hash_session_token,
        is_hashed_api_key,
    )
    raw = 'a' * 64
    stored = hash_api_key(raw)
    assert stored.startswith(API_KEY_HASH_PREFIX) and len(stored) == 71
    assert is_hashed_api_key(stored)
    assert api_key_matches(stored, raw)
    assert not api_key_matches(stored, raw[:-1] + 'b')
    assert not api_key_matches(raw, raw), '库里仍是明文时必须判不命中'
    assert not is_hashed_api_key('')
    assert hash_session_token(raw) == stored[len(API_KEY_HASH_PREFIX):]


def test_v11_migration_idempotent_and_keys_stay_usable(tmp_path):
    """真 SQLite：明文换成摘要、原文仍能比对、重复执行不二次哈希。"""
    from framework.database.db import Database
    from framework.database.schema import _migrate_api_tokens_hash
    from framework.token_hash import api_key_matches, hash_api_key

    db = Database({'type': 'sqlite', 'path': (tmp_path / 'apikeys.db').as_posix()})
    db.execute(
        "CREATE TABLE api_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "token VARCHAR(512) NOT NULL UNIQUE, name VARCHAR(100), "
        "role VARCHAR(20), is_active INTEGER DEFAULT 1)")
    raw1, raw2 = 'k' * 64, 'm' * 64
    db.execute("INSERT INTO api_tokens (token, name) VALUES (%s, %s)", (raw1, 'k1'))
    db.execute("INSERT INTO api_tokens (token, name) VALUES (%s, %s)", (raw2, 'k2'))

    _migrate_api_tokens_hash(db)
    stored = [r['token'] for r in db.query("SELECT token FROM api_tokens ORDER BY id")]
    assert stored == [hash_api_key(raw1), hash_api_key(raw2)]
    assert raw1 not in stored and raw2 not in stored
    assert api_key_matches(stored[0], raw1) and api_key_matches(stored[1], raw2)

    _migrate_api_tokens_hash(db)  # 中断重入 / 重复执行都必须收敛
    assert [r['token'] for r in db.query("SELECT token FROM api_tokens ORDER BY id")] == stored
    try:
        db.close()
    except Exception:
        pass


def test_apikey_create_stores_hash_and_raw_still_authenticates(web_env):
    """真实路由：创建返回明文，库里只有摘要，且原始 Key 立即能用。"""
    from framework.token_hash import hash_api_key
    fw, c = web_env['fw'], web_env['client']
    token = _login(c, 'admin')
    assert token, 'super 登录失败'
    r = c.post('/api/apikeys', json={'name': 'probe-key', 'role': 'admin'},
               headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200 and r.get_json()['code'] == 0, r.get_json()
    raw = r.get_json()['data']['token']
    row = fw.db.query_one("SELECT token FROM api_tokens WHERE name = %s", ('probe-key',))
    assert row and row['token'] == hash_api_key(raw)
    assert raw not in str(list(row.values())), '明文绝不能落库'
    assert c.get('/api/config/yaml',
                 headers={'Authorization': f'Bearer {raw}'}).status_code == 200


def test_legacy_plaintext_apikey_converges_on_first_use(web_env):
    """未跑 v11 的老库：明文 Key 首次调用即原地收敛，且继续可用。"""
    from framework.token_hash import hash_api_key
    fw, c = web_env['fw'], web_env['client']
    raw = 'L' * 64
    fw.db.execute(
        "INSERT INTO api_tokens (token, name, role, is_active) "
        "VALUES (%s, %s, 'admin', 1)", (raw, 'legacy-key'))
    h = {'Authorization': f'Bearer {raw}'}
    assert c.get('/api/config/yaml', headers=h).status_code == 200
    row = fw.db.query_one("SELECT token FROM api_tokens WHERE name = %s", ('legacy-key',))
    assert row['token'] == hash_api_key(raw)
    assert c.get('/api/config/yaml', headers=h).status_code == 200


# ------------------------------------------------ L2 单条写超时不双写

def test_l2_single_write_waits_for_slow_success():
    """底层超时但最终落盘成功时必须返回 True，否则同一事件会回落重复消费。"""
    from framework.core.event_buffer import EventBuffer

    class _Slow:
        def write(self, ev):
            time.sleep(0.15)
            return True

    buf = object.__new__(EventBuffer)   # 绕过 __init__，只测这一条路径
    buf.l2_mode, buf._l2_file = 'file', _Slow()
    buf.sqlite_write_timeout, buf._overflow_to_sqlite = 0.02, 0
    assert asyncio.run(buf._l2_write({'x': 1})) is True
    assert buf._overflow_to_sqlite == 1

    class _Fail:
        def write(self, ev):
            return False

    buf._l2_file, buf._overflow_to_sqlite = _Fail(), 0
    assert asyncio.run(buf._l2_write({'x': 1})) is False
    assert buf._overflow_to_sqlite == 0


# ------------------------------------------------ 部署 / 依赖前移

def test_deploy_and_update_exclude_closed_source_webui():
    deploy = open(os.path.join(ROOT, 'tools', 'deploy_remote.sh'), encoding='utf-8').read()
    assert "--exclude='./webui'" in deploy
    assert "grep -q '^\\./webui/'" in deploy
    upd = open(os.path.join(ROOT, 'framework', 'api', 'framework_update.py'),
               encoding='utf-8').read()
    whitelist = upd.split('_FW_UPDATE_INCLUDE')[1].split('}')[0]
    assert "'webui'" not in whitelist, '根目录闭源源码不能被更新包覆盖'
    assert 'core_plugins/webui/web' in whitelist, '构建产物必须仍可更新'


def test_optional_driver_install_moved_out_of_event_loop(tmp_path, monkeypatch):
    import framework.deps.pip as pipmod
    from framework.bootstrap import deps_check

    (tmp_path / 'config.yaml').write_text('database:\n  type: sqlite\n', encoding='utf-8')
    monkeypatch.setattr(deps_check, '_PROJECT_ROOT', str(tmp_path))
    called = []
    monkeypatch.setattr(pipmod, 'pip_install_requirements',
                        lambda *a, **k: called.append(1) or {'success': True})
    deps_check.ensure_optional_driver()
    assert not called, 'sqlite 配置不应触发 pip'
    # 用 AST 判断「实际调用」，注释里提到旧实现的字样不算数
    import ast
    src = open(os.path.join(ROOT, 'framework', 'database', 'db_conn.py'),
               encoding='utf-8').read()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == 'run'
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == 'subprocess']
    assert not calls, '运行期不得再同步跑 pip 阻塞事件循环'


# ------------------------------------------------ migrations 两阶段记账

def test_migration_ledger_marks_running_before_apply(tmp_path):
    from framework.database import migrations as M
    from framework.database.db import Database

    db = Database({'type': 'sqlite', 'path': (tmp_path / 'ledger.db').as_posix()})
    seen = []

    def _apply(database):
        row = database.query_one(
            "SELECT success, note FROM schema_migrations WHERE version = %s", (1,))
        seen.append(((row or {}).get('success'), (row or {}).get('note')))

    m = M.Migration(1, 'probe', _apply, 'probe', level=M.LEVEL_ADDITIVE)
    report = M.run_pending(db, (m,), M.MigrationContext(source='test'))
    assert report['ok'] is True, report
    assert seen == [(0, 'running')], 'apply 执行时账本必须已有 running 痕迹'
    row = db.query_one("SELECT success, note FROM schema_migrations WHERE version = %s", (1,))
    assert row['success'] == 1 and row['note'] == '', '成功后覆盖为 success=1'

    # 模拟「apply 中途被杀」：账本停在 running，下轮必须显性报告并幂等重跑
    def _noop(database):
        pass

    m3 = M.Migration(3, 'crashed', _noop, 'crashed', level=M.LEVEL_ADDITIVE)
    M._write_record(db, m3, '', 0, False, note='running')
    report2 = M.run_pending(db, (m, m3), M.MigrationContext(source='test'))
    assert any(x['version'] == 3 for x in report2['interrupted']), report2
    assert report2['ok'] is True
    try:
        db.close()
    except Exception:
        pass


# ------------------------------------------------ scheduler 在途语义

def _loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True, name='hardening-loop')
    t.start()
    return loop, t


def _stop_loop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_scheduler_drain_covers_tasks_registered_while_waiting():
    from framework.scheduler import TaskScheduler
    loop, t = _loop_thread()
    try:
        s = TaskScheduler(SimpleNamespace(loop=loop, config={'scheduler': {}}))
        done = []

        async def _first():
            await asyncio.sleep(0.1)
            done.append('first')

        async def _second():
            await asyncio.sleep(0.1)
            done.append('second')

        f1 = asyncio.run_coroutine_threadsafe(_first(), loop)
        with s._inflight_lock:
            s._inflight.add(f1)

        def _late():
            f2 = asyncio.run_coroutine_threadsafe(_second(), loop)
            with s._inflight_lock:
                s._inflight.add(f2)

        timer = threading.Timer(0.04, _late)
        timer.start()
        t0 = time.monotonic()
        s.stop(graceful=True, drain_timeout=2)
        assert time.monotonic() - t0 < 1.5, '不能误判超时'
        assert 'second' in done, 'drain 期间新登记的任务也必须等完'
    finally:
        _stop_loop(loop, t)


def test_scheduler_timeout_blocks_reentry_until_thread_really_finishes():
    from framework.scheduler import TaskScheduler
    loop, t = _loop_thread()
    try:
        s = TaskScheduler(SimpleNamespace(
            loop=loop, config={'scheduler': {'task_timeout': 0.05}}))
        entered = []

        def _slow():
            entered.append(1)
            time.sleep(0.25)

        async def _twice():
            r1 = await s._invoke(_slow, '[probe] handler')
            r2 = await s._invoke(_slow, '[probe] handler')
            return r1, r2

        r1, r2 = asyncio.run_coroutine_threadsafe(_twice(), loop).result(timeout=3)
        assert r1 == 'timeout', r1
        assert r2 == 'skipped', '超时后实例未真正结束，重入必须被跳过'
        assert len(entered) == 1, '同步 handler 不能被第二次触发'
        # 等底层线程真正结束，label 才会释放
        time.sleep(0.3)
        assert s._active_tasks('[probe] ') == []
    finally:
        _stop_loop(loop, t)


def test_drain_plugin_tasks_skips_when_called_on_loop_thread():
    from framework.scheduler import TaskScheduler
    loop, t = _loop_thread()
    try:
        s = TaskScheduler(SimpleNamespace(loop=loop, config={'scheduler': {}}))
        r = asyncio.run_coroutine_threadsafe(
            _async_call(lambda: s.drain_plugin_tasks('probe')), loop).result(timeout=2)
        assert r is False, '同线程调用必须跳过，否则 fut.result() 会死锁'
    finally:
        _stop_loop(loop, t)


async def _async_call(fn):
    return fn()


def test_sql_sim_close_quiet_when_log_stream_closed(tmp_path):
    """close() 也是 atexit 回调，日志流已关闭时不得刷 logging 内部 traceback。

    对照组：同一环境下直接 logger.debug 必须真的触发 handleError，证明
    用例环境能复现问题，不是靠「什么都没打」蒙混过关。
    """
    import contextlib

    from framework.database import sql_sim

    eng = sql_sim.SqlSimEngine({
        'path': str(tmp_path / 'zcbot.db'),
        'fallback_dir': str(tmp_path / 'db'),
        'debug_flush_ms': 0,
    })
    # 制造一张脏表，确保 close() 真的走到日志分支（否则用例是假阳性）
    eng._cache['t'] = {'schema': [], 'rows': [], 'next_id': 1}
    eng._dirty.add('t')

    handler = logging.StreamHandler(io.StringIO())
    handler.stream.close()
    sql_sim.logger.addHandler(handler)
    old_level = sql_sim.logger.level
    old_raise = logging.raiseExceptions
    # 显式固定级别与 handleError 行为：pytest 单独跑时 zcbot 可能不是 DEBUG，
    # 对照组会不 emit，用例就变成「什么都没打」的假阴性。
    sql_sim.logger.setLevel(logging.DEBUG)
    logging.raiseExceptions = True
    stderr_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr_buf):
            eng.close()
            sql_sim.logger.debug('对照组：关闭流上的日志')
    finally:
        sql_sim.logger.removeHandler(handler)
        sql_sim.logger.setLevel(old_level)
        logging.raiseExceptions = old_raise

    out = stderr_buf.getvalue()
    assert 'Logging error' in out, '对照组未复现，测试环境无效'
    assert 'I/O operation on closed file' in out
    assert out.count('Logging error') == 1, 'close() 自身也产生了 logging 内部报错'
    assert os.path.exists(str(tmp_path / 'db' / 'sql' / 't.json')), '脏表仍应正常落盘'


def test_route_table_rebuilds_after_core_plugins_loaded(tmp_path):
    """核心插件加载完成必须触发路由表重建（否则命令全部 unmatched）。

    回归背景：router._compute_signature() 只统计 plugins / commands /
    dynamic_commands 三张表，而核心插件（core_plugins/，含 echo/help/webui）
    **不入 plugins 表** —— 所以「插件加载完成」不改变任何 DB 签名。

    历史行为：启动早期那次 _rebuild_routes 时 loaded 仍为空 → 建出空路由表；
    随后插件加载完，签名不变且 _force_refresh=False → 永久跳过重建。
    实测（aarch64 / Linux，plugins/ 目录为空的纯核心插件部署）：
        LOADED=["echo","scheduler","webui","help"] 却 ROUTES=[]
        /echo 你好 链路走到 unmatched，黑盒集成 4 条断言失败。
    """
    from framework.database.db import Database
    from framework.database.init_db import auto_init_database
    from framework.database.schema import _auto_create_tables
    from framework.messaging.router import MessageRouter

    db = Database({"type": "sqlite", "path": str(tmp_path / "route.db")})
    auto_init_database(db)
    _auto_create_tables(db)
    db.execute(
        "INSERT INTO commands (plugin_name, pattern, handler, priority, "
        "is_dynamic, is_active, created_at) VALUES (%s, %s, %s, 50, 0, 1, %s)",
        ("echo", "/echo", "handle_echo", "2026-01-01 00:00:00"))

    class _Loader:
        def __init__(self):
            self.loaded = {}

        def get_loaded_plugins(self):
            return dict(self.loaded)

        def get_plugin_module(self, name):
            return object() if name in self.loaded else None

        def _refresh_group_plugin_cache(self):
            pass

    class _Fw:
        def __init__(self, db):
            self.db = db
            self.plugin_loader = _Loader()

    fw = _Fw(db)
    router = MessageRouter(fw)

    # 中间态：插件尚未加载完 → 空路由表是正常的
    router._rebuild_routes()
    assert router._routes == {}

    # 核心插件加载完成：DB 三表完全没变（这正是历史盲区）
    fw.plugin_loader.loaded = {"echo": {"priority": 50}}

    router._rebuild_routes()
    assert "echo" in router._routes, (
        "插件加载完成后路由表仍为空：签名未纳入「已加载插件集」")
    assert [c.pattern for c in router._routes["echo"].commands] == ["/echo"]


def test_route_table_tolerates_non_numeric_priority(tmp_path):
    """priority 是字符串时排序不能崩（否则整个路由表建不出来）。

    SQLite 不校验列类型，插件 __plugin_meta__ 也可能把 priority 写成
    "high"；_rebuild_routes 用 (priority, created_at) 当 sorted key，一旦
    str 和 int 混在一起就抛
    ``TypeError: '<' not supported between instances of 'str' and 'int'``，
    路由表永久建不出来 → 所有命令 unmatched（实测由 db_malformed_probe 抓到）。

    顺带守住 priority=0（最高优先级）不能被 `or 50` 的 falsy 判断吞成 50。
    """
    from framework.database.db import Database
    from framework.database.init_db import auto_init_database
    from framework.database.schema import _auto_create_tables
    from framework.messaging.router import MessageRouter

    db = Database({"type": "sqlite", "path": str(tmp_path / "prio.db")})
    auto_init_database(db)
    _auto_create_tables(db)

    class _Loader:
        def __init__(self):
            self.loaded = {}

        def get_loaded_plugins(self):
            return dict(self.loaded)

        def get_plugin_module(self, name):
            return object() if name in self.loaded else None

        def _refresh_group_plugin_cache(self):
            pass

    class _Fw:
        def __init__(self, db):
            self.db = db
            self.plugin_loader = _Loader()

    fw = _Fw(db)
    fw.plugin_loader.loaded = {
        'p_int': {'priority': 10},
        'p_str': {'priority': 'abc'},
        'p_zero': {'priority': 0},
    }
    router = MessageRouter(fw)
    router._rebuild_routes()   # 修复前这里抛 TypeError

    assert set(router._routes) == {'p_int', 'p_str', 'p_zero'}
    # priority=0 必须排在 10 前面：不能被 `value or 50` 误判成默认值
    assert router._plugin_order[0] == 'p_zero'


def test_sync_commands_sanitizes_and_isolates_bad_priority(tmp_path):
    """一条 priority 越界/非法的命令不能毁掉整批命令注册。

    _sync_commands 用 execute_many 批量写；SQLite INTEGER 是 64 位有符号，
    priority=2**200 在 bind 阶段抛 OverflowError → 整批失败 → 该插件**所有**
    命令都注册不上，只在日志留一行「同步命令失败」（实测 38 例探针中命中）。
    """
    from framework.database.db import Database
    from framework.database.init_db import auto_init_database
    from framework.database.schema import _auto_create_tables
    from framework.loader.base import PluginLoader, _safe_priority

    assert _safe_priority(2 ** 200) == 50
    assert _safe_priority('abc') == 50
    assert _safe_priority('7') == 7
    assert _safe_priority(0) == 0          # 0 是合法优先级，不能当 falsy 丢掉

    db = Database({"type": "sqlite", "path": str(tmp_path / "sync.db")})
    auto_init_database(db)
    _auto_create_tables(db)

    class _Holder:
        _cmd_has_perm_col = True

        def __init__(self, db):
            self.db = db

        def _commands_has_require_perm(self):
            return True

    def _cmd(handler, pattern, prio):
        return {'plugin_name': 'p1', 'pattern': pattern, 'alias': None,
                'description': None, 'priority': prio, 'handler_name': handler,
                'is_dynamic': 0, 'require_level': '', 'require_perm': ''}

    holder = _Holder(db)
    PluginLoader._sync_commands(holder, 'p1', [
        _cmd('h_ok', '/ok', 50),
        _cmd('h_bad', '/bad', 2 ** 200),
        _cmd('h_str', '/str', 'high'),
    ])

    rows = db.query(
        "SELECT handler, priority FROM commands WHERE plugin_name = %s", ('p1',))
    got = {r['handler']: r['priority'] for r in rows}
    assert 'h_ok' in got, '正常命令没写进去：整批被坏行毁了'
    assert got.get('h_bad') == 50, '越界 priority 未被收敛'
    assert got.get('h_str') == 50, '非数值 priority 未被收敛'
