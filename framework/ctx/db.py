# -*- coding: utf-8 -*-
"""PluginContext 同步/异步数据库操作与连接池（自 ctx.py 剥离的 mixin）"""
import asyncio
import logging

logger = logging.getLogger('zcbot')

class PluginDatabaseMixin:
    """同步/异步数据库操作与连接池"""

    # 以下方法依赖 PluginContext.__init__ 建立的实例字段

    # ---- 数据库操作（连接池） ----

    def db_query(self, sql: str, params: tuple = None) -> list:
        """查询数据库，返回 list[dict]"""
        return self._db.query(sql, params)

    def db_query_one(self, sql: str, params: tuple = None) -> dict:
        """查询单条，返回 dict 或 None"""
        return self._db.query_one(sql, params)

    def db_execute(self, sql: str, params: tuple = None) -> int:
        """执行插入/更新/删除，返回受影响行数"""
        return self._db.execute(sql, params)

    def db_execute_many(self, sql: str, params_list: list) -> int:
        """批量执行，返回受影响行数"""
        return self._db.execute_many(sql, params_list)

    def db_insert(self, sql: str, params: tuple = None) -> int:
        """插入并返回自增 ID"""
        return self._db.insert(sql, params)

    def create_table(self, ddl: str):
        """
        插件建表统一入口（自动适配方言，无需判断数据库类型）
        - SQLite：自动翻译 MySQL 风格 DDL（ENUM→TEXT、AUTO_INCREMENT→AUTOINCREMENT、INDEX 移除等）
        - MySQL：自动将长列（TEXT / VARCHAR>191）索引改写为前缀索引 `col`(191)，避免错误 1170/1064
        """
        try:
            self._db.execute(ddl)
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 建表失败: {e}")
            raise

    def db_connection(self):
        """
        获取一个数据库连接（高级用法）
        池模式下调用方 close() 会自动归还连接到池中
        适用于事务或多条连续操作：

            conn = ctx.db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("INSERT ...")
                cursor.execute("UPDATE ...")
                conn.commit()
            except:
                conn.rollback()
            finally:
                cursor.close()
                conn.close()  # 归还到池
        """
        return self._db.get_connection()

    # ---- 异步数据库操作（不阻塞事件循环，async handler 推荐使用）----

    async def _db_thread(self, func, *args):
        """在数据库专用线程池执行（与默认线程池隔离；DB 繁忙时不影响消息处理）"""
        ex = getattr(self._framework, '_db_executor', None)
        if ex is not None:
            return await asyncio.get_running_loop().run_in_executor(ex, func, *args)
        return await asyncio.to_thread(func, *args)

    async def db_query_async(self, sql: str, params: tuple = None) -> list:
        """异步查询数据库，返回 list[dict]"""
        return await self._db_thread(self._db.query, sql, params)

    async def db_query_one_async(self, sql: str, params: tuple = None) -> dict:
        """异步查询单条，返回 dict 或 None"""
        return await self._db_thread(self._db.query_one, sql, params)

    async def db_execute_async(self, sql: str, params: tuple = None) -> int:
        """异步执行插入/更新/删除，返回受影响行数"""
        return await self._db_thread(self._db.execute, sql, params)

    async def db_execute_many_async(self, sql: str, params_list: list) -> int:
        """异步批量执行，返回受影响行数"""
        return await self._db_thread(self._db.execute_many, sql, params_list)

    async def db_insert_async(self, sql: str, params: tuple = None) -> int:
        """异步插入并返回自增 ID"""
        return await self._db_thread(self._db.insert, sql, params)
    @property
    def db_pool_status(self) -> dict:
        """获取连接池状态信息"""
        return self._db.pool_status
