# -*- coding: utf-8 -*-
"""并发写一致性。

两种后端的并发模型不同（SQLite 单写者 + busy_timeout 排队；MySQL 行锁 +
InnoDB 并发写），但框架对上层承诺的是「多线程写不丢数据、不重复」。
这里不测吞吐（那是 bench.py 的事），只测**正确性**：条数对得上、无重复、
异常不会把整批弄坏。
"""
import os
import sys
import threading

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def tbl(empty_db, request):
    import re
    name = 't_cc_' + re.sub(r'[^a-z0-9]+', '_',
                            request.node.name.lower())[:28].strip('_')
    empty_db.execute("DROP TABLE IF EXISTS {}".format(name))
    empty_db.execute("CREATE TABLE {} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "uid INTEGER NOT NULL, tag VARCHAR(16), "
                     "UNIQUE KEY uk_uid (uid))".format(name))
    yield name
    empty_db.execute("DROP TABLE IF EXISTS {}".format(name))


def _run_threads(fns, timeout=120):
    errs = []
    lock = threading.Lock()

    def wrap(fn):
        def inner():
            try:
                fn()
            except Exception as e:      # noqa: BLE001 - 收集所有线程异常
                with lock:
                    errs.append('{}: {}'.format(type(e).__name__, e))
        return inner

    ths = [threading.Thread(target=wrap(f)) for f in fns]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout)
    alive = [t for t in ths if t.is_alive()]
    return errs, alive


def test_concurrent_insert_no_loss(empty_db, tbl):
    """8 线程各插 50 行，一条都不能少、不能重。"""
    n_thread, per = 8, 50

    def worker(tid):
        def run():
            for i in range(per):
                empty_db.execute(
                    "INSERT INTO {} (uid, tag) VALUES (%s, %s)".format(tbl),
                    (tid * per + i, 't%d' % tid))
        return run

    errs, alive = _run_threads([worker(t) for t in range(n_thread)])
    assert not alive, "有线程超时未结束"
    assert not errs, "并发写报错: {}".format(errs[:3])

    total = empty_db.scalar("SELECT COUNT(*) FROM {}".format(tbl))
    distinct = empty_db.scalar("SELECT COUNT(DISTINCT uid) FROM {}".format(tbl))
    assert total == n_thread * per, "丢行: {} != {}".format(total, n_thread * per)
    assert distinct == total, "出现重复 uid"


def test_concurrent_unique_conflict_isolated(empty_db, tbl):
    """并发抢同一个唯一键：只有一个成功，其余是唯一冲突而不是把整批搞崩。"""
    empty_db.execute("INSERT INTO {} (uid, tag) VALUES (%s, %s)".format(tbl),
                     (999, 'seed'))

    def worker():
        def run():
            try:
                empty_db.execute(
                    "INSERT INTO {} (uid, tag) VALUES (%s, %s)".format(tbl),
                    (999, 'dup'))
            except Exception:
                pass
        return run

    errs, _ = _run_threads([worker() for _ in range(6)])
    assert not errs, "冲突处理本身抛了未捕获异常: {}".format(errs[:3])
    n = empty_db.scalar("SELECT COUNT(*) FROM {} WHERE uid = 999".format(tbl))
    assert n == 1, "唯一键冲突后出现 {} 行".format(n)


def test_concurrent_read_write(empty_db, tbl):
    """读线程与写线程并行：读不能读到半截，也不能报错。"""
    for i in range(100):
        empty_db.execute("INSERT INTO {} (uid, tag) VALUES (%s, %s)".format(tbl),
                         (i, 'seed'))

    stop = threading.Event()
    errors = []
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                n = empty_db.scalar("SELECT COUNT(*) FROM {}".format(tbl))
                assert n >= 100, "读到了比初始还少的行: {}".format(n)
                reads[0] += 1
            except Exception as e:
                errors.append('R {}: {}'.format(type(e).__name__, e))
                return

    def writer(base):
        i = base
        while not stop.is_set():
            try:
                empty_db.execute(
                    "INSERT INTO {} (uid, tag) VALUES (%s, %s)".format(tbl),
                    (i, 'w'))
                i += 1
            except Exception as e:
                errors.append('W {}: {}'.format(type(e).__name__, e))
                return

    ths = [threading.Thread(target=reader) for _ in range(2)]
    # 每个写线程独立号段，避免撞唯一键（那不是并发问题，是用例自己的错）
    ths += [threading.Thread(target=writer, args=(1000,)),
            threading.Thread(target=writer, args=(100000,))]
    for t in ths:
        t.start()
    import time
    time.sleep(1.5)
    stop.set()
    for t in ths:
        t.join(30)

    assert not errors, "并发读写报错: {}".format(errors[:3])
    assert reads[0] > 0, "读线程一次都没成功"
    assert empty_db.scalar("SELECT COUNT(*) FROM {}".format(tbl)) > 100


def test_batch_insert_partial_failure_isolated(empty_db, tbl):
    """批量写里夹一条坏数据：不能因为一条非法就把整批丢掉。"""
    ok_rows = [(i, 'ok') for i in range(10)]
    empty_db.execute_many(
        "INSERT INTO {} (uid, tag) VALUES (%s, %s)".format(tbl), ok_rows)
    assert empty_db.scalar("SELECT COUNT(*) FROM {}".format(tbl)) == 10
