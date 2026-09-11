# -*- coding: utf-8 -*-
"""
RemoteTxManager —— 核心进程（进程1）侧的跨进程事务管理器

宿主进程（进程2）的 RemoteDatabase.transaction() 会把事务内的每个 db 操作
转发为 `tx.run(tx_id, op, sql, params)`，由本管理器在核心进程的真实数据库上
于「同一事务连接」内串行执行，正常提交、异常回滚，保证原子性。

- 事务连接不依赖线程局部状态（RPC handler 可能在不同线程调度），
  因此用 tx_id → conn 显式映射。
- SQLite：开独立连接（check_same_thread=False）+ BEGIN，commit/rollback 后关闭。
- MySQL：从连接池借出并 autocommit(False)，结束归还。
- 方言翻译/NOW() 处理复用 framework/database/db.py 的既有逻辑，保证与普通 db.* 一致。
"""
import itertools
import logging
import threading

from framework.database.db import _translate_sql_for_sqlite, _translate_sql_for_mysql
from framework.database.db import _replace_now

logger = logging.getLogger('zcbot')


class RemoteTxManager:
    """核心侧事务管理器：begin / run / commit / rollback"""

    def __init__(self, db):
        self._db = db                      # 核心真实 Database
        self._tx_map = {}                  # tx_id -> {'conn':..., 'db_type':...}
        self._id_seq = itertools.count(1)
        self._lock = threading.Lock()

    def begin(self) -> str:
        """开启事务，返回 tx_id"""
        db = self._db
        tx_id = f"tx{next(self._id_seq)}"
        with self._lock:
            if db.db_type == 'sqlite':
                import sqlite3
                conn = sqlite3.connect(
                    db._db_path, check_same_thread=False,
                    detect_types=sqlite3.PARSE_DECLTYPES)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute('BEGIN')
                entry = {'conn': conn, 'db_type': 'sqlite'}
            else:
                conn = db.get_connection()
                conn.autocommit(False)
                entry = {'conn': conn, 'db_type': 'mysql'}
            self._tx_map[tx_id] = entry
        logger.debug(f"事务开启: {tx_id}")
        return tx_id

    def run(self, tx_id, op, sql, params=None):
        """在指定事务连接上执行一个数据库操作（不提交），返回与普通 db.* 一致"""
        entry = self._tx_map.get(tx_id)
        if entry is None:
            raise RuntimeError(f"未知/已结束的事务: {tx_id}")
        conn = entry['conn']
        is_sqlite = entry['db_type'] == 'sqlite'

        if is_sqlite:
            if 'NOW()' in sql.upper():
                sql, params = _replace_now(sql, params)
            sql = _translate_sql_for_sqlite(sql)
        else:
            sql = _translate_sql_for_mysql(sql)

        if op == 'query':
            cur = conn.execute(sql, params or ())
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows] if is_sqlite else rows
        if op == 'query_one':
            cur = conn.execute(sql, params or ())
            row = cur.fetchone()
            cur.close()
            if row is None:
                return None
            return dict(row) if is_sqlite else row
        if op == 'execute':
            cur = conn.execute(sql, params or ())
            rc = cur.rowcount
            cur.close()
            return rc
        if op == 'execute_many':
            cur = conn.executemany(sql, params or [])
            rc = cur.rowcount
            cur.close()
            return rc
        if op == 'insert':
            cur = conn.execute(sql, params or ())
            li = cur.lastrowid
            cur.close()
            return li
        if op == 'scalar':
            row = self.run(tx_id, 'query_one', sql, params)
            if not row:
                return None
            return next(iter(row.values()), None) if isinstance(row, dict) else row[0]
        if op == 'exists':
            return self.run(tx_id, 'query_one', sql, params) is not None
        if op == 'count':
            v = self.run(tx_id, 'scalar', sql, params)
            return int(v) if v is not None else 0
        raise RuntimeError(f"未知事务操作: {op}")

    def commit(self, tx_id):
        entry = self._tx_map.pop(tx_id, None)
        if entry is None:
            raise RuntimeError(f"未知/已结束的事务: {tx_id}")
        try:
            entry['conn'].commit()
        finally:
            self._cleanup(entry)
        logger.debug(f"事务提交: {tx_id}")

    def rollback(self, tx_id):
        entry = self._tx_map.pop(tx_id, None)
        if entry is None:
            raise RuntimeError(f"未知/已结束的事务: {tx_id}")
        try:
            entry['conn'].rollback()
        finally:
            self._cleanup(entry)
        logger.debug(f"事务回滚: {tx_id}")

    def _cleanup(self, entry):
        try:
            entry['conn'].close()
        except Exception:
            pass
