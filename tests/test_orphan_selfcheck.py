# -*- coding: utf-8 -*-
"""孤儿自检（self_check_orphans）不能误删官方插件的命令

线上现象：每次启动约 60 秒后出现
    [自检] 清理孤儿 commands: 插件 [echo] 已不存在
而 echo/help 明明已加载、目录也都在 —— 只要这条日志出现，/echo 与 /help
就真的从 commands 表里消失（路由按库重建），命令直接失效。

根因：判定「插件还在不在」只用了 discover()，它只扫 plugins/（用户插件），
官方插件在 core_plugins/，于是每个官方插件都被当成孤儿。

断言：
1. core_plugins/ 下的插件命令不被删；
2. 正在内存里加载的插件命令不被删（不依赖目录扫描是否漏掉）；
3. 真的被删掉的用户插件，其命令仍然要清理（不能修成永不清理）。
"""
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.loader.runtime import PluginRuntimeMixin  # noqa: E402


class FakeDB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    def query(self, sql):
        return [{'plugin_name': r} for r in set(self.rows)]

    def execute(self, sql, params):
        pn = params[0]
        self.deleted.append(pn)
        self.rows = [r for r in self.rows if r != pn]


def _loader(core_dir, user_dir, loaded, rows):
    obj = types.SimpleNamespace(
        _lock=threading.Lock(),
        _loaded_plugins={n: {} for n in loaded},
        framework=types.SimpleNamespace(
            _get_core_plugins_dir=lambda: core_dir,
            scheduler=None,
        ),
        db=FakeDB(rows),
    )
    obj.discover = lambda: [
        n for n in (os.listdir(user_dir) if os.path.isdir(user_dir) else [])
        if os.path.isfile(os.path.join(user_dir, n, 'main.py'))
    ]
    return obj


def _mkplugin(root, name):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'main.py'), 'w', encoding='utf-8') as f:
        f.write('def register(ctx):\n    pass\n')


def test_core_plugins_not_deleted():
    """core_plugins/ 下的 echo/help 不能被当成孤儿"""
    tmp = tempfile.mkdtemp()
    core, user = os.path.join(tmp, 'core_plugins'), os.path.join(tmp, 'plugins')
    os.makedirs(user, exist_ok=True)
    _mkplugin(core, 'echo')
    _mkplugin(core, 'help')
    obj = _loader(core, user, loaded=['echo', 'help'], rows=['echo', 'help'])
    PluginRuntimeMixin.self_check_orphans(obj)
    assert obj.db.deleted == [], f'官方插件命令被误删: {obj.db.deleted}'
    return 'core 插件命令未被删'


def test_loaded_plugin_not_deleted():
    """目录扫描漏掉（比如软链/权限）也不能删正在跑的插件"""
    tmp = tempfile.mkdtemp()
    core = os.path.join(tmp, 'core_plugins')
    user = os.path.join(tmp, 'plugins')
    os.makedirs(core, exist_ok=True)
    os.makedirs(user, exist_ok=True)
    obj = _loader(core, user, loaded=['weird'], rows=['weird'])
    PluginRuntimeMixin.self_check_orphans(obj)
    assert obj.db.deleted == [], f'在跑的插件命令被删: {obj.db.deleted}'
    return '已加载插件受保护'


def test_really_removed_plugin_still_cleaned():
    """真正被删掉的用户插件，命令仍要清理"""
    tmp = tempfile.mkdtemp()
    core = os.path.join(tmp, 'core_plugins')
    user = os.path.join(tmp, 'plugins')
    os.makedirs(core, exist_ok=True)
    os.makedirs(user, exist_ok=True)
    _mkplugin(core, 'echo')
    obj = _loader(core, user, loaded=['echo'], rows=['echo', 'gone_plugin'])
    PluginRuntimeMixin.self_check_orphans(obj)
    assert obj.db.deleted == ['gone_plugin'], f'应只删 gone_plugin，实际 {obj.db.deleted}'
    return '真孤儿仍被清理'


def test_no_core_dir_falls_back_safely():
    """core_plugins 目录不存在时不能异常、不能删已加载插件"""
    tmp = tempfile.mkdtemp()
    user = os.path.join(tmp, 'plugins')
    os.makedirs(user, exist_ok=True)
    obj = _loader(os.path.join(tmp, 'nope'), user, loaded=['echo'], rows=['echo'])
    PluginRuntimeMixin.self_check_orphans(obj)
    assert obj.db.deleted == [], f'异常路径下误删: {obj.db.deleted}'
    return '目录缺失时安全'


def _loader_with_scheduler(core_dir, user_dir, loaded, rows, plugin_tasks):
    """带最小调度器桩：只看 _plugin_tasks 的键与 plugin_name。"""
    obj = _loader(core_dir, user_dir, loaded, rows)
    removed = []

    class _Sched:
        def __init__(self):
            self._plugin_tasks = dict(plugin_tasks)

            class _Inner:
                @staticmethod
                def remove_job(tid):
                    removed.append(tid)
            self._scheduler = _Inner()

    obj.framework.scheduler = _Sched()
    obj._removed_jobs = removed
    return obj


def test_builtin_task_not_removed_as_orphan():
    """框架内置任务（__builtin__ 前缀）不隶属任何插件，孤儿自检必须放过。

    线上现象：v1.20.1 引入内置「插件缓存 TTL 清理」后，启动约 60 秒出现
        [自检] 移除调度器孤儿任务: __builtin__:cache_cleanup
    缓存清理从此再也不执行，而日志只说「移除孤儿任务」，完全看不出是内置
    任务被误杀 —— 与之前 echo/help 命令被当孤儿删掉是同一类事故。
    """
    tmp = tempfile.mkdtemp()
    core, user = os.path.join(tmp, 'core_plugins'), os.path.join(tmp, 'plugins')
    os.makedirs(core, exist_ok=True)
    os.makedirs(user, exist_ok=True)
    _mkplugin(core, 'echo')

    obj = _loader_with_scheduler(
        core, user, loaded=['echo'], rows=['echo'],
        plugin_tasks={
            '__builtin__:cache_cleanup': {'plugin_name': '__builtin__',
                                          'handler_name': 'cache_cleanup'},
            'echo:cleanup': {'plugin_name': 'echo', 'handler_name': 'cleanup'},
            'gone:tick': {'plugin_name': 'gone', 'handler_name': 'tick'},
        })
    PluginRuntimeMixin.self_check_orphans(obj)

    assert '__builtin__:cache_cleanup' not in obj._removed_jobs, \
        f'内置任务被误删: {obj._removed_jobs}'
    assert 'echo:cleanup' not in obj._removed_jobs, '已加载插件的任务被删'
    assert 'gone:tick' in obj._removed_jobs, '真孤儿任务应被清理'
    return '内置任务受保护、真孤儿仍清理'


if __name__ == '__main__':
    tests = [test_core_plugins_not_deleted, test_loaded_plugin_not_deleted,
             test_really_removed_plugin_still_cleaned, test_no_core_dir_falls_back_safely,
             test_builtin_task_not_removed_as_orphan]
    ok = fail = 0
    for t in tests:
        try:
            msg = t()
            print(f'PASS {t.__name__}: {msg}')
            ok += 1
        except AssertionError as e:
            print(f'FAIL {t.__name__}: {e}')
            fail += 1
        except Exception as e:  # noqa: BLE001
            print(f'ERROR {t.__name__}: {type(e).__name__}: {e}')
            fail += 1
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
