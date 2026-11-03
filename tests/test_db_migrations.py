# -*- coding: utf-8 -*-
"""数据库迁移引擎（版本账本 / 校验和 / 失败重试 / 基线）回归测试

验证的是账本语义本身，不依赖具体的框架表结构：

  · 全新库：注册表全部应用、账本建立、current == latest
  · 二次运行：已应用的一律不重跑（用计数器验证，而不是看返回条数）
  · 失败：写 success=0、ok=False、后续迁移停止；修复后重试成功
  · 校验和漂移：已应用的迁移被改动 → 报 drift 且绝不重跑
  · 基线：stamp 只记账不执行
  · 版本号重复/非法 → 显式报错（不静默取最后一个）
  · 账本表不属于 init.sql 的核心表集合（否则会触发整轮建表流程）

运行：python tests/test_db_migrations.py   或   pytest tests/test_db_migrations.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database import migrations as M
from framework.database.db import Database

_FAILED = []


def check(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        _FAILED.append(msg)


def _new_db():
    """临时目录 SQLite（CI 铁律：不写项目目录、不依赖工作目录）"""
    tmp = tempfile.mkdtemp(prefix='zcbot_mig_')
    return Database({'type': 'sqlite', 'path': os.path.join(tmp, 'test.db')})


# ── 测试用迁移体（必须在模块级定义，checksum 要读源码） ──────────

_CALLS = []


def _m1(db):
    _CALLS.append('v1')
    db.execute("CREATE TABLE IF NOT EXISTS t_demo (id INTEGER PRIMARY KEY, a TEXT)")


def _m2(db):
    _CALLS.append('v2')
    if not db.table_has_column('t_demo', 'b'):
        db.execute("ALTER TABLE t_demo ADD COLUMN b TEXT DEFAULT ''")


def _m3(db):
    _CALLS.append('v3')
    return None


def _m1_changed(db):
    """与 _m1 同版本但源码不同 —— 用于制造校验和漂移"""
    _CALLS.append('v1-changed')
    db.execute("CREATE TABLE IF NOT EXISTS t_demo (id INTEGER PRIMARY KEY, a TEXT, c TEXT)")


def _boom(db):
    _CALLS.append('boom')
    raise RuntimeError('模拟迁移失败')


def _registry():
    return [
        M.Migration(1, 'demo.create', _m1, '建演示表'),
        M.Migration(2, 'demo.add_b', _m2, '加列 b'),
        M.Migration(3, 'demo.noop', _m3, '空操作'),
    ]


# ── 用例 ──────────────────────────────────────────────────────

def test_fresh_database_applies_all():
    db = _new_db()
    _CALLS.clear()
    report = M.run_pending(db, _registry())
    check(report['ok'] is True, '全新库：迁移全部成功')
    check(report['ledger'] is True, '全新库：账本表建立成功')
    check(len(report['applied']) == 3, '全新库：应用 3 版')
    check(report['current'] == 3 and report['latest'] == 3, '全新库：current == latest == 3')
    check(_CALLS == ['v1', 'v2', 'v3'], '全新库：按版本顺序执行')
    check(db.table_exists(M.LEDGER_TABLE), '全新库：schema_migrations 表存在')
    st = M.status(db, _registry())
    check(st['applied_count'] == 3 and not st['pending'], 'status：3 版已应用、无待应用')
    check(len(st['history']) == 3, 'status：历史 3 条')


def test_second_run_is_noop():
    db = _new_db()
    _CALLS.clear()
    M.run_pending(db, _registry())
    _CALLS.clear()
    report = M.run_pending(db, _registry())
    check(report['ok'] is True, '二次运行：成功')
    check(report['applied'] == [], '二次运行：没有重复应用')
    check(_CALLS == [], '二次运行：迁移体一次都没被调用（不靠幂等兜底）')
    check(report['skipped'] == 3, '二次运行：3 版均跳过')
    check(report['current'] == 3, '二次运行：current 仍为 3')


def test_failure_records_and_stops():
    db = _new_db()
    _CALLS.clear()
    reg = [M.Migration(1, 'demo.create', _m1),
           M.Migration(2, 'demo.boom', _boom),
           M.Migration(3, 'demo.noop', _m3)]
    report = M.run_pending(db, reg)
    check(report['ok'] is False, '失败：ok=False')
    check([f['version'] for f in report['failed']] == [2], '失败：记录失败版本 v2')
    check('v3' not in _CALLS, '失败：后续迁移不执行（停止而非跳过）')
    check(report['current'] == 1, '失败：current 停在 v1')
    row = db.query_one(
        "SELECT success, note FROM schema_migrations WHERE version = 2")
    check(row is not None and int(row['success']) == 0, '失败：账本写 success=0')
    check('模拟迁移失败' in (row['note'] or ''), '失败：账本记录错误摘要')
    st = M.status(db, reg)
    check(st['current'] == 1 and [p['version'] for p in st['pending']] == [2, 3],
          'status：暴露 current=1 与待应用 [2,3]')


def test_failure_is_retried():
    db = _new_db()
    reg_bad = [M.Migration(1, 'demo.create', _m1), M.Migration(2, 'demo.boom', _boom)]
    M.run_pending(db, reg_bad)
    # 同一版本换实现（模拟修复后重启）
    reg_ok = [M.Migration(1, 'demo.create', _m1), M.Migration(2, 'demo.add_b', _m2)]
    _CALLS.clear()
    report = M.run_pending(db, reg_ok)
    check(report['ok'] is True, '重试：修复后成功')
    check(report['applied'] and report['applied'][0]['version'] == 2, '重试：v2 重新执行')
    row = db.query_one(
        "SELECT success FROM schema_migrations WHERE version = 2")
    check(int(row['success']) == 1, '重试：账本翻成 success=1')
    check(len(db.query("SELECT * FROM schema_migrations")) == 2, '重试：账本无重复行（覆盖写）')


def test_checksum_drift_detected_and_not_rerun():
    db = _new_db()
    M.run_pending(db, _registry())
    _CALLS.clear()
    drifted = [M.Migration(1, 'demo.create', _m1_changed),
               M.Migration(2, 'demo.add_b', _m2),
               M.Migration(3, 'demo.noop', _m3)]
    report = M.run_pending(db, drifted)
    check(report['ok'] is True, '漂移：不阻塞启动（只报不改）')
    check([d['version'] for d in report['drift']] == [1], '漂移：检出 v1')
    check(_CALLS == [], '漂移：已应用的迁移绝不重跑')
    st = M.status(db, drifted)
    check(st['ok'] is False, '漂移：status.ok=False 供监控告警')
    check(len(st['drift']) == 1, '漂移：status 暴露 1 处')


def test_baseline_stamps_without_running():
    db = _new_db()
    _CALLS.clear()
    result = M.stamp(db, _registry(), 2)
    check(result['stamped'] == [1, 2], '基线：记到 v2')
    check(_CALLS == [], '基线：不执行任何迁移体')
    st = M.status(db, _registry())
    check(st['current'] == 2, '基线：current=2')
    check([p['version'] for p in st['pending']] == [3], '基线：只剩 v3 待应用')
    # 基线行不参与漂移判定（接管老库时源码对不上是常态）
    st2 = M.status(db, [M.Migration(1, 'demo.create', _m1_changed),
                        M.Migration(2, 'demo.add_b', _m2),
                        M.Migration(3, 'demo.noop', _m3)])
    check(st2['drift'] == [], '基线：baseline 行不计漂移')
    report = M.run_pending(db, _registry())
    check([a['version'] for a in report['applied']] == [3], '基线：后续照常增量应用 v3')


def test_registry_validation():
    db = _new_db()
    try:
        M.run_pending(db, [M.Migration(1, 'a', _m1), M.Migration(1, 'b', _m2)])
        check(False, '注册表：重复版本号必须报错')
    except ValueError as e:
        check('重复' in str(e), '注册表：重复版本号报错')
    try:
        M.run_pending(db, [M.Migration(0, 'zero', _m1)])
        check(False, '注册表：版本号必须为正')
    except ValueError:
        check(True, '注册表：版本号必须为正')


def test_next_version_helper():
    check(M.next_version([]) == 1, 'next_version：空注册表从 1 开始')
    check(M.next_version(_registry()) == 4, 'next_version：接在最大版本之后')


def test_ledger_not_in_core_tables():
    from framework.database.init_db import _REQUIRED_TABLES
    check(M.LEDGER_TABLE not in _REQUIRED_TABLES,
          '账本表不在 init.sql 核心表清单内（避免触发整轮建表）')


def test_framework_registry_shape():
    from framework.database.schema import MIGRATIONS
    versions = [m.version for m in MIGRATIONS]
    check(versions == list(range(1, len(versions) + 1)),
          '框架注册表：版本号从 1 连续递增（历史版本不可重排）')
    check(all(m.name for m in MIGRATIONS), '框架注册表：每版都有名称')
    check(all(M.checksum_of(m) for m in MIGRATIONS), '框架注册表：每版都能算出校验和')


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        print(f"\n[{fn.__name__}]")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            _FAILED.append(f'{fn.__name__}: {e}')
    print(f"\n{'=' * 56}")
    if _FAILED:
        print(f"FAILED {len(_FAILED)}:")
        for m in _FAILED:
            print(f"  - {m}")
        return 1
    print(f"ALL PASS ({len(tests)} tests)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
