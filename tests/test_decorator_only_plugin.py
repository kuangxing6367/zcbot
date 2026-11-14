# -*- coding: utf-8 -*-
"""装饰器-only 插件契约：可以省略 register，且失败重载不重复注册。

对应文档 docs/指南/编写插件.md「声明式装饰器 API」：
- 导入期用 @command/@on 等登记过、但没有 ``def register(ctx)`` 的插件必须能加载；
- 既没 register、也没任何装饰器登记的插件仍然拒绝加载（空插件）；
- 加载失败留下的待登记项必须清掉，否则重载后 flush 会把旧登记再落一遍，
  同一命令被注册两次。
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DECO_ONLY = (
    "from framework.plugin import command\n"
    "@command('ping')\n"
    "def ping(event, match):\n"
    "    return 'pong'\n"
)


class _MockDB:
    def execute(self, sql, params=None):
        return None

    def query(self, sql, params=None):
        return []

    def query_one(self, sql, params=None):
        return None


class _RecCtx:
    """记录 ctx 收到的注册项，其余能力一律吞掉。"""

    instances = {}

    def __init__(self, name, fw):
        self._plugin_name = name
        self._framework = fw
        self.commands = []
        self._register_journal = []
        _RecCtx.instances[name] = self

    def command(self, **kw):
        self.commands.append(kw)

    def on(self, *a, **k):
        pass

    def _get_commands(self):
        return [(c.get('pattern') or c['handler'].__name__, c.get('handler'))
                for c in self.commands]

    def _get_tasks(self):
        return []

    def _get_dashboard_cards(self):
        return []

    def _get_raw_message_handlers(self):
        return []

    def _get_group_extensions(self):
        return []

    def _get_user_extensions(self):
        return []

    def __getattr__(self, name):
        return lambda *a, **k: None


def _make_loader(tmp_path, monkeypatch):
    import framework.ctx as ctx_pkg
    from framework.loader.base import PluginLoader

    monkeypatch.setattr(ctx_pkg, 'PluginContext', _RecCtx)
    _RecCtx.instances.clear()

    plugins_dir = tmp_path / 'plugins'
    dat_dir = tmp_path / 'plugins_dat'
    plugins_dir.mkdir()
    dat_dir.mkdir()

    fw = types.SimpleNamespace(
        db=_MockDB(),
        config={'plugin': {}},
        router=types.SimpleNamespace(_invalidate_cache=lambda: None),
        scheduler=None,
        messaging=None,
        event_bus=types.SimpleNamespace(
            unsubscribe_plugin=lambda n: None,
            unsubscribe=lambda *a, **k: None),
    )
    fw.register_raw_message_handler = lambda *a, **k: None

    loader = PluginLoader(str(plugins_dir), fw, plugins_dat_dir=str(dat_dir))
    loader.check_dependencies = lambda name: {'missing': [], 'conflicts': []}
    loader._record_dep_status = lambda *a, **k: None
    return loader, plugins_dir


def _write_plugin(plugins_dir, name, source):
    d = plugins_dir / name
    d.mkdir(exist_ok=True)
    (d / 'main.py').write_text(source, encoding='utf-8')
    return d / 'main.py'


def _pending_keys(name):
    from framework.plugin import _PENDING
    return {k: len(v) for k, v in _PENDING.items() if name in k}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    from framework.plugin import _PENDING
    for k in [k for k in _PENDING if 'test_deco_only' in k]:
        _PENDING.pop(k, None)


def test_decorator_only_plugin_loads_without_register(tmp_path, monkeypatch):
    """没有 def register，但导入期登记过 @command → 允许加载并落账。"""
    loader, plugins_dir = _make_loader(tmp_path, monkeypatch)
    _write_plugin(plugins_dir, 'test_deco_only_a', DECO_ONLY)

    assert loader.load_plugin('test_deco_only_a') is True
    assert loader.register_commands('test_deco_only_a') is True

    ctx = _RecCtx.instances['test_deco_only_a']
    assert len(ctx.commands) == 1
    assert ctx.commands[0]['pattern'] == 'ping'
    assert _pending_keys('test_deco_only_a') == {}


def test_plugin_without_register_or_decorators_is_rejected(tmp_path, monkeypatch):
    """既无 register 也无装饰器 → 空插件，拒绝加载。"""
    loader, plugins_dir = _make_loader(tmp_path, monkeypatch)
    _write_plugin(plugins_dir, 'test_deco_only_empty', "VALUE = 1\n")

    assert loader.load_plugin('test_deco_only_empty') is False
    assert 'test_deco_only_empty' not in loader._loaded_plugins


def test_import_failure_clears_pending_and_retry_registers_once(tmp_path, monkeypatch):
    """导入期登记后抛异常 → 待登记项必须清空；修好重载只注册一次。"""
    loader, plugins_dir = _make_loader(tmp_path, monkeypatch)
    main_py = _write_plugin(plugins_dir, 'test_deco_only_b',
                            DECO_ONLY + "raise RuntimeError('boom')\n")

    assert loader.load_plugin('test_deco_only_b') is False
    assert _pending_keys('test_deco_only_b') == {}, '加载失败后待登记项残留'

    main_py.write_text(DECO_ONLY, encoding='utf-8')
    assert loader.load_plugin('test_deco_only_b') is True
    assert loader.register_commands('test_deco_only_b') is True

    ctx = _RecCtx.instances['test_deco_only_b']
    assert len(ctx.commands) == 1, '重载后重复注册了同一命令'
    assert _pending_keys('test_deco_only_b') == {}
