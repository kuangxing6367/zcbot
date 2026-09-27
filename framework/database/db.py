"""
数据库操作模块
支持 SQLite（默认）和 MySQL（可选），自动适配。

设计原则：
- 默认使用 SQLite —— 零配置开箱即用，但仅适合小环境与开发环境（单写多读）；
  多群、高并发、多进程等大环境请配置 database.type: mysql（见 docs/advanced/database.md）
- 自动处理 %s → ? 占位符转换（插件无需修改 SQL 方言）
- 自动处理 DDL 语法差异（ENGINE=、COMMENT、AUTO_INCREMENT 等）
- 自动处理 NOW() → datetime 参数转换

模块拆分（史山剥离）：
- dialect.py  SQL 方言翻译（纯函数）
- schema.py   自动建表 + 运行时迁移
- db.py       连接管理（Database）+ 查询/执行/事务 + 全局单例
"""
import logging
import time
from contextlib import contextmanager
from threading import local

from .dialect import (
    _replace_now,
    _translate_sql_for_mysql,
    _translate_sql_for_sqlite,
)
from .schema import _auto_create_tables  # noqa: F401  兼容旧导入路径 framework.database.db._auto_create_tables

logger = logging.getLogger('zcbot')

# MySQL 连接断开类错误码（触发自动重连）——定义在 db_conn，此处 re-export 兼容旧导入
from framework.database.db_conn import (  # noqa: F401
    _MYSQL_RECONNECT_ERRORS,
    _MYSQL_RECONNECT_KEYWORDS,
)


# ── 数据库引擎 ──────────────────────────────────────────────────────

from .db_conn import DatabaseConnMixin
from .file_store import FileStore
from .storage import create_storage  # noqa: F401  存储抽象层工厂（file/debug/sqlite/mysql）

class Database(DatabaseConnMixin):
    """
    数据库连接管理器
    支持 SQLite（默认，小环境/开发环境）和 MySQL（可选，大环境）
    """

    def __init__(self, config: dict):
        self.config = config
        self.db_type = config.get('type', 'sqlite').lower()
        self._local = local()
        self._lock = __import__('threading').Lock()
        # MySQL 连接保活/重连配置
        self._ping_interval = float(config.get('ping_interval', 5.0))   # 空闲多久 ping 一次检测连接是否存活
        self._connect_timeout = float(config.get('connect_timeout', 10))  # 建立连接超时（秒）
        self._read_timeout = float(config.get('read_timeout', 30))     # 读超时（秒），避免断连后无限卡住
        self._write_timeout = float(config.get('write_timeout', 30))   # 写超时（秒）
        self._max_reconnect = int(config.get('max_reconnect', 3))      # 单次操作最大自动重连次数
        # MySQL 连接池参数（DBUtils PooledDB，替代每线程一连接方案）
        self._pool_max = int(config.get('pool_size', 10) or 10)           # 最大连接数
        self._pool_min_cached = int(config.get('min_cached', 0) or 0)     # 最小空闲连接
        self._pool_max_cached = int(config.get('max_cached', 0) or 0)     # 最大空闲连接
        self._pool_wait_timeout = float(config.get('pool_wait_timeout', 30) or 30)  # 池满等待超时（秒）
        if self._pool_max_cached <= 0:
            # 未配置时默认与最大连接数一致（避免默认 0 导致空闲连接被全部回收、频繁新建）
            self._pool_max_cached = self._pool_max
        self._pool = None

        if self.db_type == 'mysql':
            self._init_mysql()
        else:
            # 启动警告：SQLite 模式下配置了 MySQL 字段，字段将被忽略
            mysql_only = ['host', 'port', 'user', 'password']
            configured = [k for k in mysql_only if config.get(k) not in (None, '')]
            if configured:
                logger.warning(
                    f"检测到 database.type = sqlite，但配置了 MySQL 字段（{', '.join(configured)}），"
                    f"这些字段将被忽略。如果要用 MySQL，请将 type 改为 mysql。"
                )
            self._init_sqlite()
            logger.info(
                "SQLite 模式：单文件零配置，仅适合小环境/开发环境（单写多读）；"
                "多群、高并发、多进程等大环境请切换 database.type: mysql"
                "（见 docs/advanced/database.md）"
            )

    # ── 方言翻译统一入口（消解散落的 if db_type 分支）────────────

    def _translate_sql(self, sql: str) -> str:
        """按当前方言翻译 SQL（查询类语句）"""
        if self.db_type == 'sqlite':
            return _translate_sql_for_sqlite(sql)
        return _translate_sql_for_mysql(sql)

    def _translate_write_sql(self, sql: str, params=None):
        """写语句预处理：SQLite 下先展开 NOW()（保持 %s 与参数交错顺序），再做方言翻译"""
        if self.db_type == 'sqlite' and 'NOW()' in sql.upper():
            sql, params = _replace_now(sql, params)
        return self._translate_sql(sql), params

    def _translate_write_many(self, sql: str, params_list: list):
        """批量写语句预处理：SQLite 下逐行展开 NOW()，再做方言翻译"""
        if self.db_type == 'sqlite' and 'NOW()' in sql.upper():
            new_sql, _ = _replace_now(sql, params_list[0] if params_list else None)
            params_list = [_replace_now(sql, p)[1] for p in params_list]
            sql = new_sql
        return self._translate_sql(sql), params_list

    def _exec(self, cursor, sql: str, params=None):
        """执行 sql，自动处理 params 为 None 的情况"""
        if params is not None:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

    # ── 公开 API ──────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = None) -> list:
        """查询多条记录，返回 list[dict]"""
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                self._exec(cursor, self._translate_sql(sql), params)
                rows = cursor.fetchall()
                if self.db_type == 'sqlite':
                    return [dict(r) for r in rows]
                return rows
            finally:
                cursor.close()
                if self.db_type == 'mysql':
                    conn.close()  # 归还连接池（而非真关闭）
        return self._run_with_reconnect(_do, sql, params)

    def query_one(self, sql: str, params: tuple = None) -> dict:
        """查询单条记录，返回 dict 或 None"""
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                self._exec(cursor, self._translate_sql(sql), params)
                row = cursor.fetchone()
                if row is None:
                    return None
                if self.db_type == 'sqlite':
                    return dict(row)
                return row
            finally:
                cursor.close()
                if self.db_type == 'mysql':
                    conn.close()  # 归还连接池（而非真关闭）
        return self._run_with_reconnect(_do, sql, params)

    def _write(self, sql: str, params=None, *, many: bool = False,
               return_id: bool = False):
        """
        写操作公共管线：方言预处理 → 执行 → 条件提交 → 异常回滚 → 游标/池连接释放。
        execute / execute_many / insert 共用，消除三段复制粘贴。
        :param many:      True 走 executemany（params 为 list[tuple]）
        :param return_id: True 返回 lastrowid（insert），否则返回 rowcount
        """
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                if many:
                    sql, params = self._translate_write_many(sql, params)
                    cursor.executemany(sql, params)
                else:
                    sql, params = self._translate_write_sql(sql, params)
                    self._exec(cursor, sql, params)
                if self._should_commit():
                    conn.commit()
                return cursor.lastrowid if return_id else cursor.rowcount
            except Exception:
                if not getattr(self._local, 'in_txn', False):
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise
            finally:
                cursor.close()
                if self.db_type == 'mysql':
                    conn.close()  # 归还连接池（而非真关闭）
        return self._run_with_reconnect(_do, sql, params)

    def execute(self, sql: str, params: tuple = None) -> int:
        """执行插入/更新/删除，返回受影响行数"""
        return self._write(sql, params)

    def execute_many(self, sql: str, params_list: list) -> int:
        """批量执行，返回受影响行数"""
        return self._write(sql, params_list, many=True)

    def insert(self, sql: str, params: tuple = None) -> int:
        """插入并返回自增 ID"""
        return self._write(sql, params, return_id=True)

    def get_connection(self):
        """获取原始连接（高级用法）"""
        return self._get_conn()

    def scalar(self, sql: str, params: tuple = None):
        """
        取单行单列的值。
        SELECT COUNT(*) / MAX(id) 等聚合、单值查询的快捷方式。
        无结果返回 None。
        """
        row = self.query_one(sql, params)
        if not row:
            return None
        if isinstance(row, dict):
            # 取第一个值（无论列名是什么）
            return next(iter(row.values()), None)
        return row[0]

    def exists(self, sql: str, params: tuple = None) -> bool:
        """判断查询是否有结果（EXISTS 快捷方式）"""
        return self.query_one(sql, params) is not None

    def count(self, sql: str, params: tuple = None) -> int:
        """执行 COUNT 查询并返回整数结果（无结果返回 0）"""
        v = self.scalar(sql, params)
        return int(v) if v is not None else 0
    @contextmanager
    def transaction(self, conn=None):
        """
        事务上下文管理器：
            with db.transaction():
                db.execute(...)
                db.insert(...)
        块内 db.execute / db.insert / db.execute_many 会固定在同一连接上执行，
        正常退出统一提交，块内抛异常自动回滚，保证原子性。
        入参 conn 可选：传入原始连接时，在该连接上手动控制事务（配合 get_connection 使用）。
        """
        if conn is None:
            conn = self._get_conn()
            borrowed = self.db_type == 'mysql'  # 池借出的连接需归还
        else:
            borrowed = False
        # 固定事务连接：事务内所有 db.* 调用走同一连接
        self._local.txn_conn = conn
        self._local.in_txn = True
        if self.db_type == 'mysql':
            conn.autocommit(False)
        else:
            conn.execute('BEGIN')
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._local.in_txn = False
            self._local.txn_conn = None
            if self.db_type == 'mysql':
                conn.autocommit(True)
                if borrowed:
                    conn.close()  # 归还连接池
            # SQLite 线程本地连接不关闭，交给后续复用

    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在"""
        if self.db_type == 'sqlite':
            row = self.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            return row is not None
        else:
            row = self.query_one(
                "SHOW TABLES LIKE %s",
                (table_name,)
            )
            return row is not None

    def table_info(self, table_name: str) -> list:
        """获取表结构信息"""
        if self.db_type == 'sqlite':
            return self.query(f"PRAGMA table_info({table_name})")
        else:
            return self.query(f"SHOW COLUMNS FROM {table_name}")

    def table_has_column(self, table_name: str, column_name: str) -> bool:
        """检查表是否有指定列"""
        cols = self.table_info(table_name)
        if self.db_type == 'sqlite':
            return any(r['name'] == column_name for r in cols)
        else:
            return any(r['Field'] == column_name for r in cols)


def _parse_sqlite_type(config: dict) -> dict:
    """
    解析 SQLite 数据库配置
    支持简写：database: path 或 database: {type: sqlite, path: xxx}
    """
    if isinstance(config, str):
        return {'type': 'sqlite', 'path': config}
    if isinstance(config, dict):
        cfg = dict(config)
        cfg.setdefault('type', 'sqlite')
        if cfg['type'] == 'sqlite':
            cfg.setdefault('path', 'data/zcbot.db')
        return cfg
    return {'type': 'sqlite', 'path': 'data/zcbot.db'}


def init_db(config: dict):
    """
    初始化数据库（全局单例）。

    存储抽象层：framework/database/storage.py 按 database.type 选择后端。
    - sqlite / mysql → Database（真实数据库，含自动建表与迁移）
    - file           → FileStore（降级文件存储）
    - debug          → SqlSimEngine（调试模式 / 低性能模式：本地模拟 SQL）

    硬依赖解除：真实数据库初始化失败（缺驱动、连接失败、迁移失败等）时，
    不阻塞框架启动 —— 降级为 data/db/ 下的 JSON/YAML 文件存储
    （最垃计划，见 framework/database/file_store.py）。
    """
    global db

    # 解析配置
    db_config = _parse_sqlite_type(config)
    dtype = db_config.get('type', 'sqlite').lower()

    # 非真实数据库后端（file 降级存储 / debug 调试模拟）直接经抽象层创建
    if dtype in ('file', 'debug'):
        db = create_storage(db_config)
        logger.info(f"存储后端启动：{type(db).__name__}（database.type={dtype}）")
        return db

    try:
        db = create_storage(db_config)
        # 自动检测并初始化数据库表（MySQL 5.5~8.0 / SQLite 全兼容）
        from framework.database.init_db import auto_init_database
        auto_init_database(db)

        # 创建框架扩展表 + 迁移（兼容旧版升级）
        _auto_create_tables(db)
    except Exception as e:
        logger.error(
            f"数据库初始化失败（{type(e).__name__}: {e}），"
            f"降级为 JSON/YAML 文件存储（最垃计划），框架以最低限度功能继续启动"
        )
        db = FileStore(db_config)
    return db



# ── 建表与迁移：唯一真源在 schema.py（由顶部 import 提供，勿在此重定义）─────
