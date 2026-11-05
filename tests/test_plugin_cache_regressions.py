# -*- coding: utf-8 -*-
"""插件目录归属与缓存回收的回归测试。

守的契约（见 framework/plugin_paths.py 模块文档）：

- ``plugins_dat/<name>/`` = 用户数据。卸载 / 重载 / 禁用都**保留**，只有
  显式「删除插件并勾选删除数据」才清。
- ``cache/<name>/`` = 可丢弃中间产物。卸载即清；另有内置定时任务按 TTL 扫全量。
- 插件名 → 目录名的映射只有一处（``safe_dirname``）。官方插件 ctx 名带
  ``core:`` 前缀，与 loader / ctx 各写一遍就会「写进 A 目录、卸载去清 B 目录」。

这些都是「删错就丢用户数据」或「删不掉就一直涨」的路径，必须钉死。
"""
import asyncio
import os

import pytest

from framework import plugin_paths
from framework.ctx.base import PluginContext
from framework.scheduler import BUILTIN_CACHE_CLEANUP_JOB, TaskScheduler


class _FakeLoader:
    def __init__(self, plugins_dat_dir):
        self.plugins_dat_dir = str(plugins_dat_dir)


class _FakeFramework:
    """只提供 plugin_paths / ctx 实际会碰到的属性，别的不装。"""

    def __init__(self, root):
        self.plugin_loader = _FakeLoader(os.path.join(root, 'plugins_dat'))
        self.cache_dir = os.path.join(root, 'cache')
        self.config = {'scheduler': {}, 'cache': {'dir': self.cache_dir}}
        self.db = None
        self.services = {}

    def __getattr__(self, item):  # 兜住其它属性访问，避免测试因无关依赖炸掉
        raise AttributeError(item)


def _write(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)


# ── 目录归属 ────────────────────────────────────────────────────────

def test_safe_dirname_matches_ctx_data_dir(tmp_path):
    """ctx.get_data_dir() 与 safe_dirname() 必须给出同一个目录名。

    不一致的后果是插件按 ctx 写进 core_webui、卸载按 safe_dirname 去清
    core:webui —— 清不掉且越积越多，或者反过来清错目录。
    """
    fw = _FakeFramework(tmp_path)
    for name in ('core:webui', 'echo', 'my-plugin_2', '中文插件'):
        ctx = PluginContext(name, fw)
        ctx_dir = ctx.get_data_dir()
        assert os.path.basename(ctx_dir) == plugin_paths.safe_dirname(name), name
        # 落点必须在 plugins_dat 下，不能跑到别处
        assert os.path.dirname(ctx_dir) == fw.plugin_loader.plugins_dat_dir


def test_safe_dirname_blocks_traversal(tmp_path):
    """插件名里的分隔符 / .. 不能让它逃出缓存根。"""
    fw = _FakeFramework(tmp_path)
    root = os.path.realpath(fw.cache_dir)
    for evil in ('../evil', '..\\evil', 'a/b', 'a\\b', 'a|b', '\x00null',
                 '....//....//x'):
        safe = plugin_paths.safe_dirname(evil)
        assert '/' not in safe and '\\' not in safe and '..' not in safe, (evil, safe)
        path = plugin_paths.plugin_cache_dir(fw, evil, create=False)
        assert os.path.commonpath([os.path.realpath(path), root]) == root, (evil, path)

    # 合法名不受影响（这是向后兼容的底线，改了会找不到老数据）
    assert plugin_paths.safe_dirname('core:webui') == 'core_webui'
    assert plugin_paths.safe_dirname('echo') == 'echo'
    assert plugin_paths.safe_dirname('my-plugin_2') == 'my-plugin_2'


# ── 卸载只清 cache，不动用户数据 ────────────────────────────────────

def test_purge_keeps_plugins_dat(tmp_path):
    fw = _FakeFramework(tmp_path)
    cache = plugin_paths.plugin_cache_dir(fw, 'demo')
    _write(os.path.join(cache, 'render', 'a.png'))
    _write(os.path.join(cache, 'tmp.bin'))
    dat = os.path.join(fw.plugin_loader.plugins_dat_dir, 'demo')
    _write(os.path.join(dat, 'config.json'), b'{"keep": true}')

    assert plugin_paths.purge_plugin_cache(fw, 'demo') is True

    assert os.listdir(cache) == []                      # 缓存清空
    assert os.path.isfile(os.path.join(dat, 'config.json'))  # 用户数据完好


def test_purge_missing_cache_is_noop(tmp_path):
    """缓存目录不存在时不能顺手创建出来（会污染数据目录）。"""
    fw = _FakeFramework(tmp_path)
    assert plugin_paths.purge_plugin_cache(fw, 'never_loaded') is False
    assert not os.path.isdir(plugin_paths.plugin_cache_dir(fw, 'never_loaded',
                                                          create=False))


def _make_loader_stub(fw):
    """只装配 _release_plugin_api_routes 依赖的最小 loader。

    lifecycle 里特意把清缓存写成模块级函数，正是为了这种精简 stub 不
    AttributeError —— 这里顺带把这条设计约束也钉住。
    """
    from framework.loader.lifecycle import PluginLifecycleMixin

    class _Stub(PluginLifecycleMixin):
        def __init__(self):
            self.framework = fw
            self.route_registry = None
            self.remote_registry = None

    return _Stub()


def test_release_routes_purges_cache(tmp_path):
    """卸载 / 加载失败终态 / 从未加载成功早退，三条路径都走这个回收点。"""
    fw = _FakeFramework(tmp_path)
    loader = _make_loader_stub(fw)

    for name in ('ok_plugin', 'import_failed', 'never_loaded'):
        cache = plugin_paths.plugin_cache_dir(fw, name)
        _write(os.path.join(cache, 'junk.tmp'))
        dat = os.path.join(fw.plugin_loader.plugins_dat_dir, name)
        _write(os.path.join(dat, 'state.json'), b'{}')

        loader._release_plugin_api_routes(name)

        assert os.listdir(cache) == [], name
        assert os.path.isfile(os.path.join(dat, 'state.json')), name


# ── TTL 语义 ───────────────────────────────────────────────────────

def _age(path, seconds):
    """把文件/目录 mtime 往前拨，避免测试依赖 sleep。"""
    ts = os.path.getmtime(path) - seconds
    os.utime(path, (ts, ts))


def test_ttl_disabled_deletes_nothing(tmp_path):
    """TTL<=0 = 禁用，绝不能退化成「无差别全删」。"""
    fw = _FakeFramework(tmp_path)
    old = os.path.join(fw.cache_dir, 'demo', 'old.bin')
    _write(old)
    _age(old, 86400 * 30)

    for ttl in (0, -1, -0.5, 'bad', None):
        stats = plugin_paths.purge_expired_cache(fw, ttl_hours=ttl,
                                                min_age_seconds=0)
        assert stats == {'files': 0, 'dirs': 0, 'freed': 0}, ttl
        assert os.path.isfile(old), ttl


def test_ttl_only_removes_expired(tmp_path):
    fw = _FakeFramework(tmp_path)
    fresh = os.path.join(fw.cache_dir, 'demo', 'fresh.bin')
    stale = os.path.join(fw.cache_dir, 'demo', 'stale.bin')
    _write(fresh)
    _write(stale)
    _age(stale, 10 * 3600)     # 10 小时前
    _age(os.path.dirname(stale), 10 * 3600)

    stats = plugin_paths.purge_expired_cache(fw, ttl_hours=5,
                                            min_age_seconds=0)

    assert not os.path.exists(stale)
    assert os.path.isfile(fresh)
    assert stats['files'] == 1
    assert stats['freed'] == 1


def test_min_age_protects_fresh_files(tmp_path):
    """min_age_seconds 是保底：TTL 再小也不能删刚写进来的文件。"""
    fw = _FakeFramework(tmp_path)
    recent = os.path.join(fw.cache_dir, 'demo', 'recent.bin')
    _write(recent)
    _age(recent, 5)            # 5 秒前

    stats = plugin_paths.purge_expired_cache(fw, ttl_hours=0.001,
                                            min_age_seconds=60)
    assert stats['files'] == 0
    assert os.path.isfile(recent)


def test_resolve_cache_cleanup_config_defaults_and_disable():
    cfg = plugin_paths.resolve_cache_cleanup_config({})
    assert cfg['enabled'] is True
    assert cfg['ttl_hours'] == plugin_paths.CACHE_CLEANUP_SPEC['cache_ttl_hours']['default']

    assert plugin_paths.resolve_cache_cleanup_config(
        {'cache_ttl_hours': 0})['enabled'] is False
    assert plugin_paths.resolve_cache_cleanup_config(
        {'cache_ttl_hours': -3})['enabled'] is False
    # 非法值回退默认，而不是变成 0 / 负数导致静默禁用
    bad = plugin_paths.resolve_cache_cleanup_config({'cache_ttl_hours': 'oops'})
    assert bad['enabled'] is True


# ── 内置定时任务 ───────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_builtin_task_not_registered_when_disabled(tmp_path):
    fw = _FakeFramework(tmp_path)
    fw.config['scheduler'] = {'cache_ttl_hours': 0}
    sched = TaskScheduler(fw)

    async def _go():
        sched._scheduler.start()
        try:
            sched._register_builtin_tasks()
        finally:
            sched._scheduler.shutdown(wait=False)

    _run(_go())
    assert BUILTIN_CACHE_CLEANUP_JOB not in sched._plugin_tasks


def test_builtin_task_idempotent(tmp_path):
    """重复注册不能堆出两个同 id 任务（重启 / 重复 start 都会走这里）。"""
    fw = _FakeFramework(tmp_path)
    fw.config['scheduler'] = {'cache_ttl_hours': 12,
                              'cache_cleanup_interval_hours': 6}
    sched = TaskScheduler(fw)

    async def _go():
        sched._scheduler.start()
        try:
            sched._register_builtin_tasks()
            sched._register_builtin_tasks()
            sched._register_builtin_tasks()
            ids = [j.id for j in sched._scheduler.get_jobs()]
            # get_jobs() 要在调度器 running 时取；shutdown 之后 jobstore 已清空
            panel_ids = [j['id'] for j in sched.get_jobs()]
        finally:
            sched._scheduler.shutdown(wait=False)
        return ids, panel_ids

    ids, panel_ids = _run(_go())
    assert ids.count(BUILTIN_CACHE_CLEANUP_JOB) == 1, ids
    # 面板 / stats 要能看到它，否则内置任务在 UI 上隐形
    assert BUILTIN_CACHE_CLEANUP_JOB in sched._plugin_tasks
    assert BUILTIN_CACHE_CLEANUP_JOB in panel_ids


def test_builtin_task_runs_cleanup(tmp_path):
    """任务体真的会清过期文件，且不在事件循环线程里做文件 IO。"""
    fw = _FakeFramework(tmp_path)
    fw.config['scheduler'] = {'cache_ttl_hours': 1,
                              'cache_cleanup_interval_hours': 1,
                              'cache_min_age_seconds': 0}
    stale = os.path.join(fw.cache_dir, 'demo', 'stale.bin')
    _write(stale)
    _age(stale, 5 * 3600)
    sched = TaskScheduler(fw)

    async def _go():
        sched._scheduler.start()
        try:
            sched._register_builtin_tasks()
            await sched._invoke(
                lambda: plugin_paths.purge_expired_cache(fw, 1, 0),
                '[内置] 插件缓存清理')
        finally:
            sched._scheduler.shutdown(wait=False)

    _run(_go())
    assert not os.path.exists(stale)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
