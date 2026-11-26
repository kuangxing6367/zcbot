"""底层破坏性测试：数据库连接与线程生命周期。

xfail(strict=True) 的语义见 test_adv_hook_registry.py 顶部说明。
"""
import os
import threading

import pytest

from framework.database.db import Database


def _make_db(tmp_path):
    db = Database({'type': 'sqlite', 'path': str(tmp_path / 'adv.db')})
    db.execute('CREATE TABLE IF NOT EXISTS adv_probe (id INTEGER)')
    return db


@pytest.mark.xfail(
    strict=True,
    reason="缺陷 B1：短命线程创建的 SQLite 连接只在 shutdown() 时回收，运行期无限累积；见 FINDINGS.md",
)
def test_dead_thread_sqlite_conns_are_reclaimed_at_runtime(tmp_path):
    """每来一个短命线程查一次库，连接登记表就多一条，永远不掉。

    框架里凡是用 threading.Thread 做一次 DB 查询（插件常见写法），
    每跑一次就泄漏一个 sqlite 文件句柄；长时间运行会撞到 fd 上限，
    而在 Windows 上还会导致库文件删不掉。日志里没有任何提示。
    """
    db = _make_db(tmp_path)
    baseline = len(db._sqlite_conns)

    def one_query():
        db.query_one('SELECT 1 AS x')

    threads = [threading.Thread(target=one_query) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 调用运行期唯一公开的清理入口，看看能不能回收
    db.close()

    assert len(db._sqlite_conns) <= baseline + 1, (
        f'12 个线程结束后仍登记 {len(db._sqlite_conns)} 条连接（基线 {baseline}），'
        '运行期没有任何回收路径'
    )
    db.shutdown()


def test_close_in_one_thread_does_not_break_other_threads(tmp_path):
    """正向守卫（1.28.16 修复点）：close() 只释放本线程连接。

    这正是 db.close() 拖垮全框架那个缺陷的反面：一个线程调用 close()，
    另一个线程正在用的连接必须还能正常查询。
    """
    db = _make_db(tmp_path)
    seen = {}

    def closer():
        db.close()

    def reader():
        try:
            seen['row'] = db.query_one('SELECT 1 AS x')
        except Exception as e:
            seen['err'] = e

    t1 = threading.Thread(target=closer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert 'err' not in seen, f'另一个线程的连接被误关: {seen.get("err")}'
    assert seen['row']['x'] == 1
    db.shutdown()


def test_shutdown_then_query_self_heals_sqlite(tmp_path):
    """正向守卫：SQLite 下即使误调 shutdown()，后续查询也应自动重连。"""
    db = _make_db(tmp_path)
    db.shutdown()
    assert db.query_one('SELECT 1 AS x')['x'] == 1
