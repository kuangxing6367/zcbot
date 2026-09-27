# -*- coding: utf-8 -*-
"""
复现：禁用插件失效（自愈逻辑把已禁用插件重新拉起）

1. 插件加载失败（main.py 损坏）→ 登记 _failed_mtimes
2. 用户禁用该插件（DB is_active=0 + unload_plugin）
3. 用户修复 main.py
4. 心跳 retry_failed_plugins → 应不再重载（禁用必须彻底停止自愈）
   —— 当前实现仍会 load+register 并把 DB 状态改回 running（BUG）
"""
import os
import sys
import time
import tempfile
import shutil
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MockDB:
    def __init__(self):
        self._active = {}          # plugin_name -> is_active
        self.calls = []

    def set_active(self, name, val):
        self._active[name] = val

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if isinstance(params, (list, tuple)) and len(params) == 2:
            if 'SET status' in sql and self._active.get(params[0], 1):
                pass  # 仅记录
        return None

    def query(self, sql, params=None):
        return []

    def query_one(self, sql, params=None):
        # SELECT is_active FROM plugins WHERE plugin_name = %s → (name,)
        if params and isinstance(params, (list, tuple)) and len(params) == 1:
            name = params[0]
            if name in self._active:
                return {'is_active': self._active[name]}
        if sql.startswith('SELECT id FROM plugins'):
            return None
        return None


def main():
    tmp = tempfile.mkdtemp(prefix='zcbot_disable_bug_')
    plugins_dir = os.path.join(tmp, 'plugins')
    dat_dir = os.path.join(tmp, 'plugins_dat')
    plug_dir = os.path.join(plugins_dir, 'plug_x')
    os.makedirs(plug_dir)
    main_py = os.path.join(plug_dir, 'main.py')
    with open(main_py, 'w', encoding='utf-8') as f:
        f.write("def register(ctx):\n    syntax error\n")

    db = MockDB()
    from framework.loader.base import PluginLoader
    fw = types.SimpleNamespace(
        db=db,
        config={'plugin': {}},
        router=types.SimpleNamespace(_invalidate_cache=lambda: None),
    )
    fw.register_raw_message_handler = lambda *a, **k: None
    loader = PluginLoader(plugins_dir, fw, plugins_dat_dir=dat_dir)
    loader.check_dependencies = lambda name: {'missing': [], 'conflicts': []}
    loader._record_dep_status = lambda *a, **k: None

    # 1) 损坏导致加载失败
    assert loader.load_plugin('plug_x') is False
    assert 'plug_x' in loader._failed_mtimes, "失败登记缺失"
    print("[1] 加载失败已登记 _failed_mtimes")

    # 2) 用户禁用：DB is_active=0 + unload_plugin
    db.set_active('plug_x', 0)
    loader.unload_plugin('plug_x')
    print(f"[2] 禁用后 unload，_failed_mtimes 残留: {'plug_x' in loader._failed_mtimes}")
    print("    -> unload_plugin 对从未加载成功的插件直接 return，未清理失败记录")

    # 3) 用户修复文件
    time.sleep(0.05)
    with open(main_py, 'w', encoding='utf-8') as f:
        f.write("def register(ctx):\n    return True\n")
    time.sleep(0.05)

    # 4) 心跳自愈触发
    loader.heartbeat_register()
    reloaded = 'plug_x' in loader._loaded_plugins
    running_update = any(
        c[0] for c in db.calls
        if c[0] and "status='running'" in c[0] and c[1] and c[1][0] == 'plug_x'
    )
    print(f"[4] 心跳后插件被重新加载: {reloaded}，DB 被改回 running: {running_update}")
    print(f"    _failed_mtimes 已清理: {'plug_x' not in loader._failed_mtimes}")
    print("\n=== 结论 ===")
    if reloaded:
        print("BUG 复现：已禁用（is_active=0）的插件被自愈逻辑重新拉起，用户无法关闭")
    else:
        print("OK：禁用后自愈逻辑不再重载该插件，可正常关闭")

    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if reloaded else 0


if __name__ == '__main__':
    sys.exit(main())