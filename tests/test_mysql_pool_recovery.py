"""回归测试：MySQL 连接池被误关后能自愈，不再让全框架查询挂死。

C2：1.28.16 把 close() 改成安全语义后，shutdown() 仍是真正关池的入口。
插件若误调 shutdown()，池关闭且不会自动重建——后续所有查询都失败，
只能重启进程。现在 shutdown() 把 _pool 置空，_get_conn_mysql 察觉后
惰性重建。
"""
import types

from framework.database.db_conn import DatabaseConnMixin


class _FakePool:
    def __init__(self):
        self.closed = False
        self.connection_calls = 0

    def close(self):
        self.closed = True

    def connection(self):
        self.connection_calls += 1
        return types.SimpleNamespace(cursor=lambda: None, close=lambda: None)


class _Db(DatabaseConnMixin):
    def __init__(self):
        self.db_type = 'mysql'
        self._pool = _FakePool()
        self._pool_wait_timeout = 1
        self._pool_max = 10
        self._init_calls = 0
        self._ping_interval = 999

    def _init_mysql(self):
        self._init_calls += 1
        self._pool = _FakePool()

    def _ping_if_idle(self, conn):
        return True


def test_shutdown_closes_pool_and_marks_uninitialized():
    db = _Db()
    old = db._pool
    db.shutdown()

    assert old.closed is True
    assert db._pool is None


def test_query_after_shutdown_reinitializes_pool():
    db = _Db()
    db.shutdown()
    assert db._pool is None

    conn = db._get_conn_mysql()

    assert db._init_calls == 1, '惰性重建应发生'
    assert db._pool is not None
    assert conn is not None
    assert db._pool.connection_calls == 1


def test_reinit_failure_raises_clear_error():
    db = _Db()
    db.shutdown()

    def broken():
        db._init_calls += 1
        db._pool = None

    db._init_mysql = broken
    try:
        db._get_conn_mysql()
        assert False, '重建失败应抛错'
    except RuntimeError as e:
        assert '重建失败' in str(e)
