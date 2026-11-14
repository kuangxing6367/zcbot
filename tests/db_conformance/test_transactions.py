"""事务语义一致性。

关键差异：MySQL 连接池是 autocommit=True，事务必须显式 BEGIN；SQLite 默认
隐式事务。框架用 `with db.transaction()` 统一成「显式事务」。这里断言这套
抽象在两边行为一致：提交生效、异常回滚、回滚后连接仍可用。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def tbl(empty_db, request):
    import re
    name = 't_tx_' + re.sub(r'[^a-z0-9]+', '_',
                            request.node.name.lower())[:28].strip('_')
    empty_db.execute(f"DROP TABLE IF EXISTS {name}")
    empty_db.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "v VARCHAR(32))")
    yield name
    empty_db.execute(f"DROP TABLE IF EXISTS {name}")


def test_commit_persists(empty_db, tbl):
    with empty_db.transaction():
        empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('a',))
        empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('b',))
    assert empty_db.scalar(f"SELECT COUNT(*) FROM {tbl}") == 2


def test_rollback_on_exception(empty_db, tbl):
    with pytest.raises(RuntimeError):
        with empty_db.transaction():
            empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('a',))
            raise RuntimeError('boom')
    assert empty_db.scalar(f"SELECT COUNT(*) FROM {tbl}") == 0


def test_rollback_keeps_connection_usable(empty_db, tbl):
    """回滚之后连接不能处于脏状态，后续写入必须照常工作。"""
    with pytest.raises(RuntimeError):
        with empty_db.transaction():
            empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('a',))
            raise RuntimeError('boom')
    empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('after',))
    assert empty_db.scalar(f"SELECT COUNT(*) FROM {tbl}") == 1


def test_autocommit_outside_transaction(caps, empty_db, tbl):
    """不在 transaction() 里时，单条写应立即生效（不要在池化连接里悬着）。"""
    empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('x',))
    # 另开一条独立连接读，能读到才说明真的提交了
    cfg = dict(empty_db.config)
    from framework.database.db import Database
    other = Database(cfg)
    try:
        n = other.scalar(f"SELECT COUNT(*) FROM {tbl}")
        assert n == 1, "另一条连接读不到已写入的行（未提交）"
    finally:
        other.close()


def test_multiple_transactions_sequential(empty_db, tbl):
    for i in range(5):
        with empty_db.transaction():
            empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('v%d' % i,))
    assert empty_db.scalar(f"SELECT COUNT(*) FROM {tbl}") == 5


def test_failed_transaction_then_success(empty_db, tbl):
    for _ in range(3):
        try:
            with empty_db.transaction():
                empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('bad',))
                raise ValueError('fail')
        except ValueError:
            pass
    with empty_db.transaction():
        empty_db.execute(f"INSERT INTO {tbl} (v) VALUES (%s)", ('good',))
    rows = [r['v'] for r in empty_db.query(f"SELECT v FROM {tbl}")]
    assert rows == ['good'], f"失败事务有残留: {rows}"
