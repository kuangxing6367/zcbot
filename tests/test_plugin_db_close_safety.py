"""插件误调 db.close() 不能把框架共享连接池关掉。

`Database` 实例全进程只有一份（`framework.db`），插件通过 `ctx._db` 拿到的是同一个
对象。插件里写完 SQL 顺手 `db.close()` 是很常见的写法；如果 close() 真的去关
MySQL 连接池，后续所有查询都会失败且池不会自动重建，只能重启进程。
"""
import os
import tempfile

from framework.database.db import Database


def _sqlite_db(tmp):
    return Database({'type': 'sqlite', 'path': os.path.join(tmp, 'close.db'), 'name': 't'})


def test_close_keeps_sqlite_queryable():
    """SQLite：close() 只释放本线程连接，之后的查询自动重连"""
    tmp = tempfile.mkdtemp()
    db = _sqlite_db(tmp)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t (v) VALUES (%s)", ('a',))
    assert len(db.query("SELECT * FROM t")) == 1

    db.close()

    # 关键：close 之后还能继续用（否则等同于插件把框架的库关没了）
    rows = db.query("SELECT * FROM t")
    assert len(rows) == 1 and rows[0]['v'] == 'a'
    db.shutdown()


def test_close_does_not_touch_mysql_pool():
    """MySQL：close() 绝不关闭连接池；只有 shutdown() 才关"""
    calls = []

    class _Pool:
        def close(self):
            calls.append('pool.close')

    db = _sqlite_db(tempfile.mkdtemp())
    # 只替换方言与池句柄，模拟 MySQL 形态（不真连库）
    db.db_type = 'mysql'
    db._pool = _Pool()

    db.close()
    assert calls == [], 'close() 不应关闭共享连接池'

    db.shutdown()
    assert calls == ['pool.close'], 'shutdown() 才应真正关闭连接池'


def test_plugin_context_close_is_safe():
    """端到端：插件在自己的路径里调 ctx._db.close()，框架查询照旧可用"""
    tmp = tempfile.mkdtemp()
    db = _sqlite_db(tmp)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t (v) VALUES (%s)", ('x',))

    # 模拟插件拿到 ctx._db 后的典型写法
    ctx_db = db
    ctx_db.query("SELECT * FROM t")
    ctx_db.close()

    assert db.query("SELECT * FROM t")[0]['v'] == 'x'
    db.shutdown()


if __name__ == '__main__':
    for fn in (test_close_keeps_sqlite_queryable,
               test_close_does_not_touch_mysql_pool,
               test_plugin_context_close_is_safe):
        fn()
        print('ok', fn.__name__)
