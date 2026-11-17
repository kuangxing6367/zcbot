"""WebUI 开关适配器（官方插件启停）的三条回归：

1. **保注释写回**：`framework.config.set_core_plugin_enabled` 旧实现用
   `yaml.safe_dump` 全量重写，用户手写的注释、键顺序、未知字段一次冲光。
   现在按行原地替换，且可选同步内存 config（即时生效的数据前提）。
2. **缺块补 schema**：插件块原本不存在时，除 enabled 外补齐该插件的 schema 默认值。
3. **卸载钩子**：官方适配器用 `unregister()` 停 WS 监听，用户插件用 `on_unload()`。
   历史只调 on_unload，导致 WebUI 禁用适配器后端口仍在监听、机器人还在收消息。
   现在两个钩子都调、各自隔离。

直接运行：py -3 tests/test_webui_adapter_switch.py
或：py -3 -m pytest tests/test_webui_adapter_switch.py -q
"""
import functools
import os
import sys
import types

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import framework.config as fc
from framework.loader.lifecycle import PluginLifecycleMixin


def _write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── 1) 保注释写回 + 内存同步 ────────────────────────────────
def test_set_enabled_preserves_comments_and_order(tmp_path, monkeypatch):
    yaml_path = str(tmp_path / 'core_plugins.yaml')
    base = (
        "# 这是用户手写的顶部注释\n"
        "core_plugins:\n"
        "  onebot_adapter:\n"
        "    enabled: false   # 关着，等部署后再开\n"
        "    listen_host: 0.0.0.0  # 监听所有网卡\n"
        "    my_custom_key: keep-me\n"
        "  echo:\n"
        "    enabled: true\n"
    )
    _write(yaml_path, base)
    monkeypatch.setattr(fc, 'CORE_PLUGINS_YAML', yaml_path)
    # 只放行真实存在的插件名，避免 _scan_core_plugins 的机会主义差异
    monkeypatch.setattr(fc, '_scan_core_plugins', lambda: {'onebot_adapter', 'echo'})

    mem = {'core_plugins': {'onebot_adapter': {'enabled': False}}}
    ok, needs_restart = fc.set_core_plugin_enabled('onebot_adapter', True, mem)
    assert ok is True
    assert needs_restart is True

    text = _read(yaml_path)
    # 注释、自定义键、键顺序全部保留
    assert "# 这是用户手写的顶部注释" in text
    assert "# 关着，等部署后再开" in text
    assert "# 监听所有网卡" in text
    assert "my_custom_key: keep-me" in text
    assert text.index('onebot_adapter') < text.index('  echo:')

    doc = yaml.safe_load(text)
    assert doc['core_plugins']['onebot_adapter']['enabled'] is True
    assert doc['core_plugins']['echo']['enabled'] is True
    # 内存同步：即时生效分支读到的值与落盘一致
    assert mem['core_plugins']['onebot_adapter']['enabled'] is True


def test_set_enabled_reject_unknown_plugin(tmp_path, monkeypatch):
    yaml_path = str(tmp_path / 'core_plugins.yaml')
    _write(yaml_path, "core_plugins:\n  echo:\n    enabled: true\n")
    monkeypatch.setattr(fc, 'CORE_PLUGINS_YAML', yaml_path)
    monkeypatch.setattr(fc, '_scan_core_plugins', lambda: {'echo'})
    ok, needs_restart = fc.set_core_plugin_enabled('not_installed', True)
    assert (ok, needs_restart) == (False, False)
    assert "not_installed" not in _read(yaml_path)


def test_patch_key_inserts_inside_target_block_not_at_eof(tmp_path, monkeypatch):
    """目标插件块不是最后一个块时，补的 schema 键必须落在该块内，不能追加到文件末尾。"""
    yaml_path = str(tmp_path / 'core_plugins.yaml')
    # onebot_adapter 在中间，后面还有 zzz 块
    _write(yaml_path,
           "core_plugins:\n"
           "  onebot_adapter:\n"
           "    enabled: true\n"
           "  zzz:\n"
           "    enabled: false\n")
    monkeypatch.setattr(fc, 'CORE_PLUGINS_YAML', yaml_path)
    monkeypatch.setattr(fc, '_scan_core_plugins', lambda: {'onebot_adapter', 'zzz'})
    ok, _ = fc.set_core_plugin_enabled('onebot_adapter', True)
    assert ok
    doc = yaml.safe_load(_read(yaml_path))
    # listen_host 属于 onebot_adapter，绝不能跑进 zzz
    assert 'listen_host' in doc['core_plugins']['onebot_adapter']
    assert 'listen_host' not in doc['core_plugins']['zzz']
    assert doc['core_plugins']['zzz']['enabled'] is False


def test_set_enabled_fills_schema_on_missing_block(tmp_path, monkeypatch):
    yaml_path = str(tmp_path / 'core_plugins.yaml')
    _write(yaml_path, "core_plugins:\n  echo:\n    enabled: true\n")
    monkeypatch.setattr(fc, 'CORE_PLUGINS_YAML', yaml_path)
    monkeypatch.setattr(fc, '_scan_core_plugins', lambda: {'echo', 'onebot_adapter'})
    # onebot_adapter 块不存在 → 追加块并补 schema 默认键
    ok, _ = fc.set_core_plugin_enabled('onebot_adapter', True)
    assert ok
    doc = yaml.safe_load(_read(yaml_path))
    blk = doc['core_plugins']['onebot_adapter']
    assert blk['enabled'] is True
    # schema 里的默认键被补齐（listen_host 等）
    assert 'listen_host' in fc._CORE_PLUGIN_SCHEMA['onebot_adapter']
    assert 'listen_host' in blk
    # 已有插件块没被破坏
    assert doc['core_plugins']['echo']['enabled'] is True


# ── 2) 卸载钩子双调 ─────────────────────────────────────────
def test_unload_calls_both_hooks():
    calls = []

    class Mod:
        def on_unload(self):
            calls.append('on_unload')

        def unregister(self):
            calls.append('unregister')

    # 不构造完整 loader：辅助方法只用 logger，不碰实例字段
    PluginLifecycleMixin._call_unload_hooks(object.__new__(PluginLifecycleMixin), 'x', Mod())
    assert calls == ['on_unload', 'unregister']


def test_unload_hook_only_one_defined():
    calls = []

    class Mod:
        def unregister(self):
            calls.append('unregister')

    PluginLifecycleMixin._call_unload_hooks(object.__new__(PluginLifecycleMixin), 'x', Mod())
    assert calls == ['unregister']


def test_unload_one_hook_raises_other_still_called():
    calls = []

    class Mod:
        def on_unload(self):
            calls.append('on_unload')
            raise RuntimeError('钩子坏了')

        def unregister(self):
            calls.append('unregister')

    PluginLifecycleMixin._call_unload_hooks(object.__new__(PluginLifecycleMixin), 'x', Mod())
    assert calls == ['on_unload', 'unregister']


def test_unload_no_hooks_is_noop():
    PluginLifecycleMixin._call_unload_hooks(
        object.__new__(PluginLifecycleMixin), 'x', types.SimpleNamespace())


# ── 3) API 即时生效分支 ─────────────────────────────────────
def test_toggle_api_applies_live(tmp_path, monkeypatch):
    pytest = __import__('pytest')
    pytest.importorskip('flask')
    from flask import Flask, request

    import framework.api.config as cfg_api

    yaml_path = str(tmp_path / 'core_plugins.yaml')
    _write(yaml_path,
           "core_plugins:\n  echo:\n    enabled: false\n")
    monkeypatch.setattr(fc, 'CORE_PLUGINS_YAML', yaml_path)
    monkeypatch.setattr(fc, '_scan_core_plugins', lambda: {'echo'})

    calls = []

    class Loader:
        _loaded_plugins = {}

        def unload_plugin(self, name):
            calls.append(('unload', name))
            self._loaded_plugins.pop(name, None)

    class Router:
        def _invalidate_cache(self):
            calls.append(('invalidate',))

    class FW:
        config = {'core_plugins': {'echo': {'enabled': False}}}
        plugin_loader = Loader()
        router = Router()

        def _load_core_plugins(self, only=None):
            calls.append(('load', frozenset(only or [])))
            for name in (only or []):
                self.plugin_loader._loaded_plugins[name] = {'module': object()}

    fw = FW()

    def _auth(fn):
        @functools.wraps(fn)
        def view(*a, **kw):
            request.admin = {'id': 1, 'username': 't', 'role': 'super'}
            return fn(*a, **kw)
        return view

    class Ctx:
        app = Flask(__name__)
        framework = fw
        db = None
        require_auth = staticmethod(_auth)
        require_super = staticmethod(_auth)
        audit_log = staticmethod(lambda *a, **kw: None)
        _yaml_config_path = lambda self: str(tmp_path / 'config.yaml')
        _read_yaml_section = lambda self, s: {}
        _update_yaml_section = lambda self, s, v: True

    cfg_api.register(Ctx())
    client = Ctx.app.test_client()

    # 启用 → 落盘 + 即时 load
    r = client.put('/api/config/core-plugins', json={'name': 'echo', 'enabled': True})
    body = r.get_json()
    assert r.status_code == 200 and body['code'] == 0, body
    assert body['applied'] is True
    assert ('load', frozenset({'echo'})) in calls
    assert fw.config['core_plugins']['echo']['enabled'] is True
    assert yaml.safe_load(_read(yaml_path))['core_plugins']['echo']['enabled'] is True

    calls.clear()
    # 禁用 → 即时 unload
    r = client.put('/api/config/core-plugins', json={'name': 'echo', 'enabled': False})
    body = r.get_json()
    assert body['applied'] is True
    assert ('unload', 'echo') in calls
    assert yaml.safe_load(_read(yaml_path))['core_plugins']['echo']['enabled'] is False


def test_toggle_api_rejects_bad_name(tmp_path, monkeypatch):
    pytest = __import__('pytest')
    pytest.importorskip('flask')
    from flask import Flask, request
    import framework.api.config as cfg_api

    def _auth(fn):
        @functools.wraps(fn)
        def view(*a, **kw):
            request.admin = {'id': 1, 'username': 't', 'role': 'super'}
            return fn(*a, **kw)
        return view

    class Ctx:
        app = Flask(__name__)
        framework = None
        db = None
        require_auth = staticmethod(_auth)
        require_super = staticmethod(_auth)
        audit_log = staticmethod(lambda *a, **kw: None)
        _yaml_config_path = lambda self: str(tmp_path / 'c.yaml')
        _read_yaml_section = lambda self, s: {}
        _update_yaml_section = lambda self, s, v: True

    cfg_api.register(Ctx())
    client = Ctx.app.test_client()
    r = client.put('/api/config/core-plugins', json={'name': '../../evil', 'enabled': True})
    assert r.status_code == 400


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
