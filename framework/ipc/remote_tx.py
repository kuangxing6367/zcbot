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
from framework.database.db import _replace_now, _set_autocommit

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
                _set_autocommit(conn, False)
                entry = {'conn': conn, 'db_type': 'mysql'}
            self._tx_map[tx_id] = entry
        logger.debug(f"事务开启: {tx_id}")
        return tx_id

    def run(self, tx_id, op, **kw):
        """在指定事务连接上执行一个数据库操作（不提交），返回与普通 db.* 一致。

        参数以关键字传入（与 RemoteDatabase._call 转发的 kwargs 对齐）：
        - SQL 类：`sql` / `params`（`execute_many` 用 `params_list`）
        - 元数据类：`table_name` / `column_name`（本方法自行构造 SQL）
        - `pool_status`：无参（连接池状态不参与事务，回落到真实 db）

        旧实现固定签名 `(tx_id, op, sql, params=None)` 接不住元数据 / 批量类操作
        （会抛 TypeError），且 op 分派未覆盖 table_* / pool_status（抛「未知事务操作」）；
        此处一并修正。
        """
        entry = self._tx_map.get(tx_id)
        if entry is None:
            raise RuntimeError(f"未知/已结束的事务: {tx_id}")
        conn = entry['conn']
        is_sqlite = entry['db_type'] == 'sqlite'

        # ---- 元数据 / 池状态：不依赖传入 SQL，按 op 自建 ----
        if op == 'table_exists':
            sql = ("SELECT name FROM sqlite_master WHERE type='table' AND name=?" if is_sqlite
                   else "SHOW TABLES LIKE %s")
            return self._fetch_one(conn, sql, (kw.get('table_name'),), is_sqlite) is not None
        if op == 'table_info':
            sql = (f"PRAGMA table_info({kw.get('table_name')})" if is_sqlite
                   else f"SHOW COLUMNS FROM {kw.get('table_name')}")
            return self._fetch_all(conn, sql, None, is_sqlite)
        if op == 'table_has_column':
            cols = self.run(tx_id, 'table_info', table_name=kw.get('table_name'))
            key = 'name' if is_sqlite else 'Field'
            return any((c.get(key) if isinstance(c, dict) else None) == kw.get('column_name')
                       for c in cols)
        if op == 'pool_status':
            try:
                return self._db.pool_status
            except Exception:
                return {}

        # ---- SQL 类操作 ----
        sql = kw.get('sql')
        params = kw.get('params')
        if is_sqlite:
            if sql and 'NOW()' in sql.upper():
                sql, params = _replace_now(sql, params)
            if sql:
                sql = _translate_sql_for_sqlite(sql)
        elif sql:
            sql = _translate_sql_for_mysql(sql)

        if op == 'query':
            return self._fetch_all(conn, sql, params, is_sqlite)
        if op == 'query_one':
            return self._fetch_one(conn, sql, params, is_sqlite)
        if op == 'execute':
            cur = conn.execute(sql, params or ())
            rc = cur.rowcount
            cur.close()
            return rc
        if op == 'execute_many':
            cur = conn.executemany(sql, kw.get('params_list') or params or [])
            rc = cur.rowcount
            cur.close()
            return rc
        if op == 'insert':
            cur = conn.execute(sql, params or ())
            li = cur.lastrowid
            cur.close()
            return li
        if op == 'scalar':
            row = self.run(tx_id, 'query_one', sql=sql, params=params)
            if not row:
                return None
            return next(iter(row.values()), None) if isinstance(row, dict) else row[0]
        if op == 'exists':
            return self.run(tx_id, 'query_one', sql=sql, params=params) is not None
        if op == 'count':
            v = self.run(tx_id, 'scalar', sql=sql, params=params)
            return int(v) if v is not None else 0
        raise RuntimeError(f"未知事务操作: {op}")

    @staticmethod
    def _fetch_all(conn, sql, params, is_sqlite):
        cur = conn.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows] if is_sqlite else rows

    @staticmethod
    def _fetch_one(conn, sql, params, is_sqlite):
        cur = conn.execute(sql, params or ())
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        return dict(row) if is_sqlite else row

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
