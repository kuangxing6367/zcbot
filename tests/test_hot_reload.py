"""热重载（HMR）逻辑单元测试。

不启动完整框架，仅校验 PluginRuntimeMixin 的心跳分派逻辑：
  - hot_reload=true 时，源码变更触发 unload -> load -> register 全量重载；
  - hot_reload=false 时，沿用旧行为仅 register_commands；
  - 无变更时不触发任何重载；
  - 重载加载失败时返回 False 且不会卡死 / 不会更新 mtime 快照（交由自愈）。

运行：python tests/test_hot_reload.py
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.loader.runtime import PluginRuntimeMixin


class _RouterStub:
    def _invalidate_cache(self):
        pass


class _FrameworkStub:
    def __init__(self, hot_reload):
        self.config = {'plugin': {'hot_reload': hot_reload}}
        self.router = _RouterStub()


class _DbStub:
    def execute(self, *args, **kwargs):
        pass


class FakeLoader(PluginRuntimeMixin):
    """最小化装配 PluginRuntimeMixin，记录关键方法的调用顺序。"""

    def __init__(self, hot_reload, load_ok=True, register_ok=True, snap_after=200.0):
        self.framework = _FrameworkStub(hot_reload)
        self.plugins_dir = '/tmp/plugins'
        self._lock = threading.Lock()
        self._loaded_plugins = {
            'demo': {'path': os.path.join(self.plugins_dir, 'demo')}
        }
        self._plugin_mtimes = {'demo': 100.0}
        self._failed_mtimes = {}
        self.db = _DbStub()

        self._snap_after = snap_after
        self._calls = []
        self._load_ok = load_ok
        self._register_ok = register_ok

        # 可控快照：模拟"磁盘当前 mtime"（变更场景返回新值，无变更场景返回原值）
        # 与 _plugin_mtimes 中记录的"上次已处理 mtime"比较以判定是否变更。

    def _snapshot_mtime(self, name):
        return self._snap_after

    def unload_plugin(self, name):
        self._calls.append(('unload', name))

    def load_plugin(self, name):
        self._calls.append(('load', name))
        return self._load_ok

    def register_commands(self, name):
        self._calls.append(('register', name))
        return self._register_ok

    def retry_failed_plugins(self):
        self._calls.append(('retry',))


class TestHotReload(unittest.TestCase):

    def test_hot_reload_true_triggers_full_reload(self):
        loader = FakeLoader(hot_reload=True)
        loader.heartbeat_register()
        self.assertIn(('unload', 'demo'), loader._calls)
        self.assertIn(('load', 'demo'), loader._calls)
        self.assertIn(('register', 'demo'), loader._calls)
        # 顺序必须是 unload -> load -> register
        order = [c[0] for c in loader._calls if c[0] in ('unload', 'load', 'register')]
        self.assertEqual(order, ['unload', 'load', 'register'])
        # mtime 快照更新为新值
        self.assertEqual(loader._plugin_mtimes['demo'], 200.0)

    def test_hot_reload_false_only_register(self):
        loader = FakeLoader(hot_reload=False)
        loader.heartbeat_register()
        self.assertNotIn(('unload', 'demo'), loader._calls)
        self.assertNotIn(('load', 'demo'), loader._calls)
        self.assertIn(('register', 'demo'), loader._calls)
        self.assertEqual(loader._plugin_mtimes['demo'], 200.0)

    def test_no_change_no_reload(self):
        loader = FakeLoader(hot_reload=True, snap_after=100.0)
        loader.heartbeat_register()
        self.assertEqual([c[0] for c in loader._calls], ['retry'])
        self.assertEqual(loader._plugin_mtimes['demo'], 100.0)

    def test_reload_failure_does_not_update_snapshot(self):
        loader = FakeLoader(hot_reload=True, load_ok=False)
        loader.heartbeat_register()
        self.assertIn(('unload', 'demo'), loader._calls)
        self.assertIn(('load', 'demo'), loader._calls)
        # 加载失败：返回 False，快照不更新（避免每轮心跳反复无效重载）
        self.assertEqual(loader._plugin_mtimes['demo'], 100.0)

    def test_reload_register_failure_does_not_update_snapshot(self):
        loader = FakeLoader(hot_reload=True, register_ok=False)
        loader.heartbeat_register()
        self.assertIn(('unload', 'demo'), loader._calls)
        self.assertIn(('load', 'demo'), loader._calls)
        self.assertIn(('register', 'demo'), loader._calls)
        self.assertEqual(loader._plugin_mtimes['demo'], 100.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
