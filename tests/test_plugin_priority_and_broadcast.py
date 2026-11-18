"""插件优先级 DB 覆盖 + 广播/定向事件投递回归。

口径：
- plugins.priority 是插件声明值（心跳刷新），priority_override 是 WebUI 覆盖值；
  NULL = 回落声明值。插件升级不会冲掉用户排好的顺序。
- event_bus/ctx 的 emit 默认广播给该事件名全部订阅者，targets 可定向投递。
"""
import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

from framework.ctx.events import PluginEventsMixin
from framework.database.db import Database
from framework.database.schema import _migrate_plugins_priority_override
from framework.messaging.event_bus import EventBus
from framework.messaging.router import MessageRouter

pytest.importorskip('flask')


def _db():
    root = tempfile.mkdtemp(prefix='zcbot_prio_')
    return Database({'type': 'sqlite', 'path': os.path.join(root, 'test.db')})


def _make_plugin_table(db, with_override=True):
    db.execute("CREATE TABLE IF NOT EXISTS plugins ("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, plugin_name TEXT UNIQUE, "
               "version TEXT DEFAULT '', author TEXT DEFAULT '', description TEXT DEFAULT '', "
               "priority INTEGER DEFAULT 50, "
               + ("priority_override INTEGER DEFAULT NULL, " if with_override else "")
               + "status TEXT DEFAULT 'running', is_active INTEGER DEFAULT 1, "
                 "has_register INTEGER DEFAULT 1, loaded_at TEXT DEFAULT '', "
                 "created_at TEXT DEFAULT '2026-01-01')")


# ── DB 覆盖 ──────────────────────────────────────────────────
def test_migration_adds_priority_override_idempotently():
    db = _db()
    _make_plugin_table(db, with_override=False)
    assert not db.table_has_column('plugins', 'priority_override')
    _migrate_plugins_priority_override(db)
    assert db.table_has_column('plugins', 'priority_override')
    _migrate_plugins_priority_override(db)   # 重入不报错


def test_effective_priority_uses_override_then_falls_back():
    db = _db()
    _make_plugin_table(db)
    db.execute("INSERT INTO plugins (plugin_name, priority) VALUES ('a', 50)")
    db.execute("INSERT INTO plugins (plugin_name, priority, priority_override) "
               "VALUES ('b', 10, 200)")
    rows = db.query("SELECT plugin_name, COALESCE(priority_override, priority) "
                    "AS priority FROM plugins ORDER BY priority ASC")
    assert [r['plugin_name'] for r in rows] == ['a', 'b']
    db.execute("UPDATE plugins SET priority_override = NULL WHERE plugin_name = 'b'")
    rows = db.query("SELECT plugin_name, COALESCE(priority_override, priority) "
                    "AS priority FROM plugins ORDER BY priority ASC")
    assert [r['plugin_name'] for r in rows] == ['b', 'a']


# ── 路由排序真的读覆盖值 ─────────────────────────────────────
def test_router_order_respects_override():
    db = _db()
    _make_plugin_table(db)
    db.execute("CREATE TABLE commands (id INTEGER PRIMARY KEY, plugin_name TEXT, "
               "pattern TEXT, alias TEXT, handler TEXT, require_level INTEGER DEFAULT 0, "
               "require_perm TEXT DEFAULT '', is_dynamic INTEGER DEFAULT 0, "
               "is_active INTEGER DEFAULT 1, created_at TEXT DEFAULT '2026-01-01')")
    db.execute("CREATE TABLE dynamic_commands (id INTEGER PRIMARY KEY, keyword TEXT)")
    db.execute("INSERT INTO plugins (plugin_name, priority, priority_override) "
               "VALUES ('first_declared', 10, NULL)")
    db.execute("INSERT INTO plugins (plugin_name, priority, priority_override) "
               "VALUES ('override_wins', 90, 1)")

    class Loader:
        plugins_dir = ''
        def get_loaded_plugins(self):
            return {'first_declared': {'priority': 10},
                    'override_wins': {'priority': 90}}
        def get_plugin_module(self, name):
            return object()
        def _refresh_group_plugin_cache(self): pass

    class FW:
        pass

    fw = FW()
    fw.db = db
    fw.plugin_loader = Loader()
    router = MessageRouter(fw)
    router._compute_signature = lambda: None
    router._load_keyword_rules = lambda: []
    router._rebuild_routes()
    assert router._plugin_order[0] == 'override_wins'


# ── WebUI API ────────────────────────────────────────────────
def _passthrough(fn):
    def view(*a, **kw):
        request.admin = {'id': 1, 'username': 'tester', 'role': 'super'}
        return fn(*a, **kw)
    view.__name__ = fn.__name__
    return view


def _api_client(db, monkeypatch):
    from framework.api import plugins as plugins_api
    from framework.api import plugin_market, plugin_meta
    monkeypatch.setattr(plugin_market, 'register', lambda ctx: None)
    monkeypatch.setattr(plugin_meta, 'register', lambda ctx: None)

    class Loader:
        plugins_dir = tempfile.mkdtemp()
        plugins_dat_dir = tempfile.mkdtemp()
        def get_loaded_plugins(self): return {}
        def read_plugin_yaml(self, name): return {}
        def read_config_schema(self, name): return {}
        def get_dep_status(self, name):
            return {'has_missing': False, 'missing': [],
                    'has_conflict': False, 'conflicts': []}

    class Router:
        def _invalidate_cache(self): pass

    class FW:
        plugin_loader = Loader()
        router = Router()

    class Ctx:
        app = Flask(__name__)
        framework = FW()
        require_auth = staticmethod(_passthrough)
        require_super = staticmethod(_passthrough)
        audit_log = staticmethod(lambda *a, **kw: None)
        plugins_dir = Loader.plugins_dir
        _data_dir = lambda self: tempfile.mkdtemp()
        _read_market_sources_custom = lambda self: []
        _save_market_sources_custom = lambda self, v: None
        _DEFAULT_MARKET = []
        _MIRROR_MARKETS = []
        _github_url_candidates = lambda self, url: []
        _fetch_market_source = lambda self, src: []
        _market_installed_set = lambda self: set()
        _download_and_extract_plugin = lambda self, *a, **kw: None

    ctx = Ctx()
    ctx.db = db
    ctx.app.config['TESTING'] = True
    plugins_api.register(ctx)
    return ctx.app.test_client()


def test_priority_api_set_and_reset(monkeypatch):
    db = _db()
    _make_plugin_table(db)
    db.execute("INSERT INTO plugins (plugin_name, priority) VALUES ('demo', 50)")
    client = _api_client(db, monkeypatch)

    listing = client.get('/api/plugins').get_json()['data'][0]
    assert listing['priority'] == 50 and listing['priority_source'] == 'declared'

    r = client.put('/api/plugins/demo/priority', json={'priority': 7})
    assert r.status_code == 200 and r.get_json()['data']['priority'] == 7
    listing = client.get('/api/plugins').get_json()['data'][0]
    assert listing['priority'] == 7
    assert listing['priority_declared'] == 50
    assert listing['priority_source'] == 'override'

    # 插件心跳刷新声明值，不应冲掉覆盖
    db.execute("UPDATE plugins SET priority = 99 WHERE plugin_name = 'demo'")
    listing = client.get('/api/plugins').get_json()['data'][0]
    assert listing['priority'] == 7 and listing['priority_declared'] == 99

    r = client.put('/api/plugins/demo/priority', json={'reset': True})
    assert r.get_json()['data']['priority_source'] == 'declared'
    listing = client.get('/api/plugins').get_json()['data'][0]
    assert listing['priority'] == 99 and listing['priority_override'] is None

    assert client.put('/api/plugins/demo/priority',
                      json={'priority': 99999}).status_code == 400


# ── 广播 / 定向投递 ──────────────────────────────────────────
def test_ctx_emit_broadcast_and_targeted():
    bus = EventBus()
    got = []

    bus.subscribe('demo.event', 'plugin_a', lambda p: got.append(('a', p['n'])))
    bus.subscribe('demo.event', 'plugin_b', lambda p: got.append(('b', p['n'])))

    class FW:
        event_bus = bus

    class Ctx(PluginEventsMixin):
        def __init__(self):
            self._framework = FW()
            self._register_journal = []

    ctx = Ctx()
    ctx.emit('demo.event', {'n': 1})
    # emit 是 fire-and-forget，用一次异步 emit 收尾保证前面的任务跑完
    asyncio.run(ctx.aemit('demo.event', {'n': 99}, targets='plugin_a'))
    assert ('a', 1) in got and ('b', 1) in got
    assert ('a', 99) in got and ('b', 99) not in got


def test_aemit_targets_multiple_plugins():
    bus = EventBus()
    got = []
    for name in ('a', 'b', 'c'):
        bus.subscribe('x', name, lambda p, n=name: got.append(n))

    class FW:
        event_bus = bus

    class Ctx(PluginEventsMixin):
        def __init__(self):
            self._framework = FW()
            self._register_journal = []

    asyncio.run(Ctx().aemit('x', {}, targets=['a', 'c']))
    assert sorted(got) == ['a', 'c']


if __name__ == '__main__':
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, '-q']))
