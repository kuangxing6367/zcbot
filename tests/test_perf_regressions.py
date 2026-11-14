"""性能回归：命令权限缓存 + DB 初始化批量化。

覆盖：
  1. 权限缓存命中不再查库（首次 miss 允许查库，且必须在工作线程）
  2. 权限数据变更后缓存失效、新权限立即生效
  3. 缓存容量上限 / TTL / 线程安全
  4. 批量初始化不破坏迁移账本语义（校验和、当前态、append-only 流水、
     老账本缺列 SELECT * 容错）

测试铁律：路径由 __file__ / tmp_path 推导，不写死时区（TTL 用
time.monotonic 并可注入），不依赖 cwd，所有库建在 tmp_path 不污染项目目录。
"""
import asyncio
import os
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database.db import Database
from framework.database.init_db import auto_init_database
from framework.database.schema import MIGRATIONS, _auto_create_tables
from framework.messaging.event import Event
from framework.messaging.router import (
    MessageRouter,
    _PermissionDecisionCache,
    invalidate_permission_cache,
)

# ── 工具 ──────────────────────────────────────────────────────

def _init_db(tmp_path, name='perf.db'):
    """完整走一遍启动期初始化（自动建表 + 扩展表 + 版本化迁移）。"""
    db = Database({'type': 'sqlite', 'path': str(tmp_path / name)})
    auto_init_database(db)
    _auto_create_tables(db)
    return db


def _fw(db):
    return SimpleNamespace(db=db)


def _clear_perm_globals():
    """清掉底层模块级缓存（跨用例会残留），保证「首次 miss 一定查库」的前提。

    event._user_role_cache / perm._perm_cache 都是进程级全局且带 60s TTL，
    不清理会让后续用例直接命中上一个用例填充的条目，测不出真实往返。
    """
    from framework import perm as _perm_mod
    from framework.messaging import event as _ev_mod
    _ev_mod.invalidate_user_role_cache()
    _ev_mod.invalidate_group_role_cache()
    _perm_mod.invalidate_all()


def _make_event(user_id=1001, group_id=2002):
    return Event({'post_type': 'message', 'message': 'hi',
                  'user_id': user_id, 'group_id': group_id,
                  'message_type': 'group'}, 'default')


def _count_sql(monkeypatch, bucket):
    """把 Database 的公开 SQL 入口包一层计数（try/finally 由 monkeypatch 复原）。"""
    for name in ('query', 'query_one', 'execute', 'execute_many', 'insert'):
        orig = getattr(Database, name)

        def _mk(n, f):
            def _wrapper(self, sql, *a, **k):
                bucket.append((n, sql))
                return f(self, sql, *a, **k)
            return _wrapper

        monkeypatch.setattr(Database, name, _mk(name, orig))


# ── 1. 缓存命中不再查库 ───────────────────────────────────────

def test_permission_cache_hit_does_not_query_db(tmp_path, monkeypatch):
    _clear_perm_globals()
    db = _init_db(tmp_path)
    try:
        fw = _fw(db)
        router = MessageRouter(fw)
        calls = []
        _count_sql(monkeypatch, calls)

        async def run():
            # 首次：miss，解析 role + 权限节点（同步 DB 全在 asyncio.to_thread 里）
            ev1 = _make_event()
            ev1._framework = fw
            await router._prepare_permissions(ev1)
            assert calls, '首次 miss 应当查库完成权限解析'

            # 同一用户 / 群 / 上下文再次准备：命中进程内缓存，零 DB 往返
            calls.clear()
            ev2 = _make_event()
            ev2._framework = fw
            await router._prepare_permissions(ev2)
            assert calls == [], f'缓存命中仍查了库: {calls}'

            # 语义不变：两次注入的快照给出一致判定
            assert ev2.role == ev1.role
            assert ev2.has_perm('nothing.here') == ev1.has_perm('nothing.here')
            assert ev2.is_admin == ev1.is_admin
        asyncio.run(run())
    finally:
        db.close()


def test_permission_miss_runs_db_outside_event_loop(tmp_path, monkeypatch):
    """首次 miss 的同步 DB IO 不得发生在事件循环线程上。"""
    _clear_perm_globals()
    db = _init_db(tmp_path)
    try:
        fw = _fw(db)
        router = MessageRouter(fw)
        loop_thread = threading.get_ident()
        seen = []
        orig_q1 = Database.query_one

        def _q1(self, sql, *a, **k):
            seen.append(threading.get_ident())
            return orig_q1(self, sql, *a, **k)

        monkeypatch.setattr(Database, 'query_one', _q1)

        async def run():
            ev = _make_event()
            ev._framework = fw
            await router._prepare_permissions(ev)
        asyncio.run(run())

        assert seen, '本次 miss 未发生任何 DB 查询，用例前提不成立'
        assert loop_thread not in seen, '同步 DB IO 跑在了事件循环线程上'
    finally:
        db.close()


# ── 2. 权限变更后缓存失效 ─────────────────────────────────────

def test_permission_invalidation_after_change(tmp_path):
    from framework import perm

    _clear_perm_globals()
    db = _init_db(tmp_path)
    try:
        fw = _fw(db)
        router = MessageRouter(fw)

        async def run():
            ev1 = _make_event()
            ev1._framework = fw
            await router._prepare_permissions(ev1)
            assert len(router._perm_cache) == 1
            assert ev1.has_perm('chat.ban') is False

            # 真实权限变更：授予用户节点（perm 内部会清它自己的缓存）
            perm.set_user_node(db, 1001, 'chat.ban', value=True)

            # TTL 内若无人显式失效，缓存仍返回旧快照 —— 这正是需要失效机制的原因
            ev2 = _make_event()
            ev2._framework = fw
            await router._prepare_permissions(ev2)
            assert ev2.has_perm('chat.ban') is False

            # 显式失效后重新解析：新权限立即生效
            router.invalidate_permissions(1001)
            assert len(router._perm_cache) == 0
            ev3 = _make_event()
            ev3._framework = fw
            await router._prepare_permissions(ev3)
            assert ev3.has_perm('chat.ban') is True
        asyncio.run(run())
    finally:
        db.close()


def test_module_level_invalidation_entry(tmp_path):
    """其它修改线（framework/api/**）可用模块级入口触发失效。"""
    db = _init_db(tmp_path)
    try:
        fw = _fw(db)
        router = MessageRouter(fw)
        fw.router = router

        async def run():
            ev = _make_event()
            ev._framework = fw
            await router._prepare_permissions(ev)
            assert len(router._perm_cache) == 1
            invalidate_permission_cache(fw)
            assert len(router._perm_cache) == 0
        asyncio.run(run())
    finally:
        db.close()


# ── 3. 缓存上限 / TTL / 线程安全 ──────────────────────────────

def test_permission_cache_maxsize_evicts_lru():
    cache = _PermissionDecisionCache(ttl=60.0, maxsize=3)
    for i in range(10):
        cache.put((str(i), '', '', '', 'member', ()), 'member', object())
    assert len(cache) == 3, '容量上限未生效'
    assert cache.get(('0', '', '', '', 'member', ())) is None, '最久未用未被淘汰'
    assert cache.get(('9', '', '', '', 'member', ())) is not None


def test_permission_cache_ttl_expiry(monkeypatch):
    import framework.messaging.router as router_mod

    clock = [100.0]
    monkeypatch.setattr(router_mod.time, 'monotonic', lambda: clock[0])
    cache = _PermissionDecisionCache(ttl=5.0, maxsize=10)
    key = ('u', '', '', '', 'member', ())
    cache.put(key, 'member', object())
    assert cache.get(key) is not None, 'TTL 内应命中'
    clock[0] += 6.0
    assert cache.get(key) is None, '超过 TTL 应失效'


def test_permission_cache_thread_safe():
    cache = _PermissionDecisionCache(ttl=60.0, maxsize=64)
    errors = []

    def worker(i):
        try:
            for j in range(200):
                key = (str(i), '', '', '', 'member', ())
                cache.put(key, 'member', object())
                cache.get(key)
                if j % 50 == 0:
                    cache.invalidate(None)
                len(cache)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f'并发访问抛异常: {errors}'
    assert len(cache) <= 64, '并发下容量上限失效'


# ── 4. 批量初始化不破坏迁移账本语义 ───────────────────────────

def test_batch_init_preserves_migration_ledger(tmp_path, monkeypatch):
    db = _init_db(tmp_path, 'ledger.db')
    try:
        # 当前态：v1~v9 全部成功、校验和非空（MIGRATIONS 内容/顺序未变）
        rows = db.query("SELECT * FROM schema_migrations ORDER BY version")
        assert [int(r['version']) for r in rows] == \
            [m.version for m in MIGRATIONS]
        assert all(int(r['success']) == 1 for r in rows)
        assert all((r.get('checksum') or '') for r in rows)

        # append-only 执行证据
        runs = db.query("SELECT * FROM schema_migration_runs ORDER BY id")
        assert len(runs) >= 1
        n_runs = len(runs)

        # 二次启动：账本当前态不变、流水不新增（已应用不重跑）
        auto_init_database(db)
        _auto_create_tables(db)
        assert db.query("SELECT * FROM schema_migrations ORDER BY version") == rows
        assert len(db.query("SELECT * FROM schema_migration_runs ORDER BY id")) \
            == n_runs

        # 稳态往返：元数据探测被合并，旧式逐表 table_exists 必须绝迹
        calls = []
        _count_sql(monkeypatch, calls)
        db.invalidate_schema_snapshot()   # 强制冷启动加载一次快照
        auto_init_database(db)
        _auto_create_tables(db)
        sqls = [s for _, s in calls]
        assert not any("AND name=?" in s for s in sqls), \
            '仍在逐表探测表是否存在'
        assert len([s for s in sqls
                    if 'LEFT JOIN pragma_table_info' in s]) == 1, \
            '批量元数据探测应恰好 1 次'
        assert any('SELECT * FROM schema_migrations' in s for s in sqls), \
            '账本读取必须保持 SELECT *'
    finally:
        db.close()


def test_legacy_ledger_missing_columns_still_read(tmp_path):
    """硬契约：老账本缺后加列时，SELECT * + 缺列取默认不能整表读失败。"""
    from framework.database import migrations as M

    db = Database({'type': 'sqlite', 'path': str(tmp_path / 'legacy.db')})
    try:
        db.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT, checksum TEXT, "
            "applied_at TEXT, duration_ms INTEGER, success INTEGER, "
            "origin TEXT, note TEXT)")
        db.execute("INSERT INTO schema_migrations (version, name, success) "
                   "VALUES (1, 'legacy.row', 1)")
        rows = M._ledger_rows(db)
        assert 1 in rows
        assert rows[1]['level'] == M.LEVEL_ADDITIVE
        assert rows[1]['operator'] == ''
        assert rows[1]['ticket'] == ''
    finally:
        db.close()


def test_incremental_migration_applies_only_new_version(tmp_path):
    """账本已有旧版时只补应用新版本，旧版不重跑（版本账本语义不变）。

    用动态 ``MIGRATIONS`` 取最后一版，不写死版本号，兼容其它修改线在末尾
    追加新迁移（如安全加固 v10）。
    """
    from framework.database import migrations as M

    db = _init_db(tmp_path, 'upgrade.db')
    try:
        rows = db.query("SELECT * FROM schema_migrations ORDER BY version")
        last = int(rows[-1]['version'])
        db.execute("DELETE FROM schema_migrations WHERE version = %s", (last,))
        n_runs = len(db.query("SELECT * FROM schema_migration_runs"))

        report = M.run_pending(db, MIGRATIONS,
                               M.MigrationContext(source='test'))
        assert report['ok'] is True
        assert [r['version'] for r in report['applied']] == [last], \
            '只应补应用缺失的那一版，不得重跑已应用版本'
        rows2 = db.query("SELECT * FROM schema_migrations ORDER BY version")
        assert [int(r['version']) for r in rows2] == \
            [m.version for m in MIGRATIONS]
        assert all(int(r['success']) == 1 for r in rows2)
        # 流水 append-only：新增执行证据，历史记录不被覆盖
        assert len(db.query("SELECT * FROM schema_migration_runs")) > n_runs
    finally:
        db.close()
