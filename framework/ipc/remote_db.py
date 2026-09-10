# -*- coding: utf-8 -*-
"""
RemoteDatabase —— 宿主侧数据库代理（进程2 插件经此 RPC 到进程1 的真实 Database）

接口与 framework/db.py 的 Database 对齐，插件/框架代码无需改动：
每个方法把调用转发为 IPC RPC `db.<name>`，由核心进程在真实数据库上执行。

说明：
- 同步方法（query/execute 等）会阻塞当前线程等待核心返回；建议 async handler
  使用 ctx 的 db_*_async 系列（它们经框架 _db_executor 线程池跑，不阻塞事件循环）。
- transaction()：跨进程事务已支持。事务内所有 db.* 调用自动携带当前 tx_id，
  由核心进程在「同一事务连接」上执行，正常提交、异常回滚（RemoteTxManager）。
- get_connection()：真实连接对象无法跨进程序列化，不支持，抛清晰异常。
"""
import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger('zcbot')


class RemoteDatabase:
    """代理核心进程的 Database（接口对齐 framework/db.py 的 Database）"""

    def __init__(self, client):
        self._client = client
        self.db_type = 'remote'
        self._local = threading.local()   # 事务内记录当前 tx_id

    def _call(self, name, **params):
        """RPC 到核心；若处于事务中，自动改走 tx.run 保持同一事务连接"""
        tx_id = getattr(self._local, 'tx_id', None)
        if tx_id is not None:
            return self._client.call('tx.run', {
                'tx_id': tx_id, 'op': name, **params})
        return self._client.call(f'db.{name}', params)

    # ---- 查询 ----

    def query(self, sql, params=None):
        return self._call('query', sql=sql, params=params)

    def query_one(self, sql, params=None):
        return self._call('query_one', sql=sql, params=params)

    def execute(self, sql, params=None):
        return self._call('execute', sql=sql, params=params)

    def execute_many(self, sql, params_list=None):
        return self._call('execute_many', sql=sql, params_list=params_list or [])

    def insert(self, sql, params=None):
        return self._call('insert', sql=sql, params=params)

    def scalar(self, sql, params=None):
        return self._call('scalar', sql=sql, params=params)

    def exists(self, sql, params=None):
        return self._call('exists', sql=sql, params=params)

    def count(self, sql, params=None):
        return self._call('count', sql=sql, params=params)

    # ---- 元数据 ----

    def table_exists(self, table_name):
        return self._call('table_exists', table_name=table_name)

    def table_info(self, table_name):
        return self._call('table_info', table_name=table_name)

    def table_has_column(self, table_name, column_name):
        return self._call('table_has_column', table_name=table_name,
                          column_name=column_name)

    def create_table(self, ddl):
        # DDL 由核心进程的真实 Database 做方言适配，此处直接转发
        return self._call('execute', sql=ddl, params=None)

    @property
    def pool_status(self) -> dict:
        return self._call('pool_status')

    # ---- 事务 / 连接（Phase 2 补全）----

    def get_connection(self):
        raise NotImplementedError(
            "双进程模式下不支持 db.get_connection()（真实连接无法跨进程）；"
            "请改用 db.transaction() 或 ctx.db_query/execute 系列")

    @contextmanager
    def transaction(self, conn=None):
        """跨进程事务：块内 db.* 调用自动落在核心的同一事务连接上。

        用法与单进程一致：
            with db.transaction():
                db.insert(...)
                db.execute(...)
        正常退出提交，块内抛异常回滚。
        """
        if conn is not None:
            raise NotImplementedError(
                "双进程事务不支持传入外部连接（跨进程无法序列化连接对象）")
        tx_id = self._client.call('tx.begin')
        self._local.tx_id = tx_id
        try:
            yield
            self._client.call('tx.commit', {'tx_id': tx_id})
        except Exception:
            try:
                self._client.call('tx.rollback', {'tx_id': tx_id})
            except Exception:
                pass
            raise
        finally:
            self._local.tx_id = None

    def close(self):
        # 连接由核心进程持有，宿主侧无需关闭
        pass
