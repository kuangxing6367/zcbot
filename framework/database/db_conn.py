# -*- coding: utf-8 -*-
"""Database 连接管理 mixin（自 database/db.py 剥离）

初始化 / 连接获取 / 断线重连 / 线程连接关闭 / 池状态。
依赖 Database.__init__ 建立的配置与线程本地存储。
"""
import logging
import os
import time
import sqlite3

# MySQL 连接断开类错误码（触发自动重连）——定义在此避免 db↔db_conn 循环
_MYSQL_RECONNECT_ERRORS = {2006, 2013, 2055, 1927, 1040}
# 连接断开类错误关键字（用于兜底判断）
_MYSQL_RECONNECT_KEYWORDS = (
    'server has gone away',
    'lost connection',
    'connection is closed',
    'broken pipe',
    'connection reset by peer',
)

logger = logging.getLogger('zcbot')


class DatabaseConnMixin:
    """连接池 / 断线重连 / 生命周期"""


    def _init_sqlite(self):
        """初始化 SQLite"""
        db_path = self.config.get('path', 'data/zcbot.db')
        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self._db_path = db_path
        logger.info(f"SQLite 数据库已初始化: {db_path}")

    def _normalize_mysql_params(self) -> dict:
        """校验并规范化 MySQL 连接参数。

        pymysql 对 dict/list 类型的参数不会报「哪个键写错了」，而是在内部
        拼接 SQL 时抛 ``unsupported operand type(s) for +: 'dict' and 'bytes'``，
        完全指不到配置项。典型成因是把参数多嵌套了一层：

            database:
              type: mysql
              mysql:            # ← 多出来的这一层
                host: 127.0.0.1

        这里在建池前把类型问题变成一句能直接照着改的错误。
        """
        cfg = self.config
        expected = {
            'host': '127.0.0.1',
            'port': 3306,
            'user': 'root',
            'password': '',
            'database': 'zcbot',
            'charset': 'utf8mb4',
        }
        bad = []
        for key in expected:
            val = cfg.get(key, expected[key])
            if isinstance(val, (dict, list, tuple, set)):
                bad.append(
                    f"database.{key} 应为字符串/数字，实际是 {type(val).__name__}"
                    f"（{val!r}）"
                )
        if bad:
            msg = (
                "MySQL 连接参数类型错误，请检查 config.yaml 的 database 段："
                + "；".join(bad)
                + "。常见原因是把 mysql 参数又嵌套了一层（如 database.mysql.host），"
                  "或 database.database 被写成了对象——所有键应与 database.type 平级。"
            )
            logger.error(msg)
            raise ValueError(msg)

        def _s(key):
            val = cfg.get(key, expected[key])
            return '' if val is None else str(val)

        try:
            port = int(cfg.get('port', 3306) or 3306)
        except (TypeError, ValueError):
            msg = (f"database.port 必须是整数，实际是 "
                   f"{cfg.get('port')!r}，请检查 config.yaml")
            logger.error(msg)
            raise ValueError(msg) from None

        return {
            'host': _s('host') or '127.0.0.1',
            'port': port,
            'user': _s('user') or 'root',
            # 密码允许写成数字（如 password: 123456），pymysql 只接受字符串
            'password': _s('password'),
            'database': _s('database') or 'zcbot',
            'charset': _s('charset') or 'utf8mb4',
        }

    def _init_mysql(self):
        """初始化 MySQL 连接池（检测到 MySQL 配置时，自动安装 pymysql/DBUtils）"""
        try:
            import pymysql
            from pymysql.cursors import DictCursor
            self._pymysql = pymysql
            self._DictCursor = DictCursor
            logger.info("MySQL 模式已启用（适合大环境：多群/高并发/多进程部署）")
        except ImportError:
            logger.warning("MySQL 模式需要 pymysql，正在自动安装...")
            import subprocess
            import sys
            try:
                result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', 'pymysql', 'DBUtils'],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    logger.info("pymysql 安装成功，重新导入...")
                    import pymysql
                    from pymysql.cursors import DictCursor
                    self._pymysql = pymysql
                    self._DictCursor = DictCursor
                else:
                    logger.error(f"pymysql 自动安装失败: {result.stderr}")
                    raise ImportError("pymysql 安装失败，请手动执行: pip install pymysql DBUtils")
            except Exception as e:
                logger.error(f"pymysql 自动安装异常: {e}")
                raise ImportError(f"无法自动安装 pymysql: {e}")

        # 创建 DBUtils 连接池（真正限制连接数：空闲回收、坏连接自动重建、池满阻塞）
        try:
            from dbutils.pooled_db import PooledDB
        except ImportError:
            logger.error("缺少 DBUtils，请手动执行: pip install DBUtils")
            raise ImportError("缺少 DBUtils，请手动执行: pip install DBUtils")
        # 建池前校验参数类型：pymysql 对 dict/list 这类参数不会给出可读报错，
        # 只会在内部拼接时抛 "unsupported operand type(s) for +: 'dict' and 'bytes'"
        # 这种完全指不到配置项的错误。这里提前拦下并指出具体是哪个键写错了。
        p = self._normalize_mysql_params()
        self._pool = PooledDB(
            creator=self._pymysql,
            maxconnections=self._pool_max,          # 最大连接数（pool_size）
            mincached=self._pool_min_cached,        # 启动即建的最小空闲连接（min_cached）
            maxcached=self._pool_max_cached,        # 最大空闲连接，超过自动关闭释放（max_cached）
            maxshared=0,
            blocking=False,                         # 池满不无限等待，由 _get_conn_mysql 有界重试
            setsession=[],
            reset=True,                             # 借出时回滚残留事务
            ping=1,                                 # 每次借出 ping 验证，坏连接自动丢弃重建
            host=p['host'],
            port=p['port'],
            user=p['user'],
            password=p['password'],
            database=p['database'],
            charset=p['charset'],
            cursorclass=self._DictCursor,
            autocommit=True,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            write_timeout=self._write_timeout,
        )
        logger.info(
            f"MySQL 连接池已初始化: max={self._pool_max}, "
            f"min_cached={self._pool_min_cached}, max_cached={self._pool_max_cached}"
        )

    def _get_conn_sqlite(self):
        """获取 SQLite 连接（线程本地）"""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # WAL 下 synchronous=NORMAL 即安全：仅断电极端场景可能丢失最后一个事务，
            # 但完全免去每次写都 fsync 的开销，写入吞吐显著提升（事件缓冲 / 高频统计写受益最大）。
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA wal_autocheckpoint=2000")  # 调大 checkpoint 间隔，降低写放大
            self._local.conn = conn
        return conn

    @staticmethod
    def _is_pool_waitable(exc: Exception) -> bool:
        """是否为「池暂时满/池未开」这类可等待重试的异常。

        只有这些才值得在 pool_wait_timeout 内反复重试。其余异常
        （参数类型错、认证失败、建连被拒）重试再多次结果也一样，
        继续重试只会把一次失败放大成整段超时（且会让池内计数失真，
        最终报成"连接全被占用"这种误导性结论）。
        """
        name = type(exc).__name__
        return any(k in name for k in (
            'TooManyRequests', 'PoolNotOpen', 'PoolClosed', 'Unavailable'))

    def _get_conn_mysql(self):
        """
        从连接池借出连接（PooledDB 自动处理：ping 保活、坏连接丢弃重建、
        空闲连接按 maxcached 回收、连接数不超过 pool_size）。
        池满时**有界等待**（pool_wait_timeout 秒），超时抛清晰错误而非无限阻塞——
        避免个别连接未归还（如插件 get_connection 泄漏）把整个框架 DB 操作堵死。
        归还方式：调用方在 finally 中 conn.close()（对池而言是"归还"而非真关闭）。
        """
        if self._pool is None:
            raise RuntimeError("MySQL 连接池未初始化")
        deadline = time.time() + self._pool_wait_timeout
        last_err = None
        while True:
            try:
                return self._pool.connection()
            except Exception as e:
                last_err = e
                # 非「池满」类异常立即上抛：重试无意义，且会把启动拖成假死
                if not self._is_pool_waitable(e):
                    logger.error(
                        f"MySQL 借连接失败（不可重试，参数或配置有误）: "
                        f"{type(e).__name__}: {e}"
                    )
                    raise
                if time.time() >= deadline:
                    logger.error(
                        f"MySQL 连接池繁忙: {self._pool_max} 个连接全被占用超 "
                        f"{self._pool_wait_timeout}s（{last_err}）。"
                        f"请检查是否存在连接未归还（如 ctx.get_connection() 未 close）"
                    )
                    raise RuntimeError(
                        f"MySQL 连接池繁忙（{self._pool_max} 个连接全被占用超 "
                        f"{self._pool_wait_timeout}s），请检查连接泄漏"
                    ) from None
                time.sleep(0.05)

    def _close_thread_conn(self):
        """关闭当前线程的 SQLite 连接并释放线程本地状态。
        MySQL 连接池接管后无需手动关闭连接（坏连接由 PooledDB 借出时 ping 检测并重建）。"""
        if self.db_type != 'mysql':
            conn = getattr(self._local, 'conn', None)
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"关闭 SQLite 线程连接失败: {e}")
                try:
                    del self._local.conn
                except Exception:
                    pass

    def _mark_conn_used(self):
        """记录连接最近使用时间（避免频繁 ping）"""
        self._local.last_use = time.time()
    @staticmethod
    def _is_reconnect_error(exc: Exception) -> bool:
        """判断异常是否为 MySQL 连接断开类错误（需要自动重连）"""
        if exc is None:
            return False
        # 按错误码判断（pymysql 异常 args[0] 通常为错误码）
        code = None
        if isinstance(getattr(exc, 'args', None), (tuple, list)) and exc.args:
            code = exc.args[0]
        if isinstance(code, int) and code in _MYSQL_RECONNECT_ERRORS:
            return True
        # 按错误消息关键字兜底判断
        msg = str(exc).lower()
        return any(kw in msg for kw in _MYSQL_RECONNECT_KEYWORDS)

    def _get_conn(self):
        """获取连接。处于事务中时返回被固定（pin）的事务连接，确保事务内所有操作走同一连接，原子生效。"""
        txn_conn = getattr(self._local, 'txn_conn', None)
        if txn_conn is not None:
            return txn_conn
        if self.db_type == 'mysql':
            return self._get_conn_mysql()
        return self._get_conn_sqlite()

    def _should_commit(self) -> bool:
        """事务内不自动提交（交由外层 transaction() 统一提交/回滚），避免破坏原子性"""
        return not getattr(self._local, 'in_txn', False)

    def _get_cursor(self):
        """获取游标"""
        return self._get_conn().cursor()

    def _run_with_reconnect(self, func, *args, **kwargs):
        """
        执行数据库操作，MySQL 连接断开时自动重连并重试（最多 _max_reconnect 次）。
        重连前丢弃坏连接，避免每次操作都复用已失效的连接导致持续失败。
        """
        if self.db_type != 'mysql':
            return func(*args, **kwargs)
        for attempt in range(self._max_reconnect + 1):
            try:
                result = func(*args, **kwargs)
                self._mark_conn_used()
                return result
            except Exception as e:
                if not self._is_reconnect_error(e):
                    raise
                if getattr(self._local, 'in_txn', False):
                    # 事务 pin 连接已死，回滚已不可能；事务内重试没有意义，
                    # 直接抛给调用方（下一次 transaction 会借到 ping 过的新连接）
                    raise
                if attempt >= self._max_reconnect:
                    logger.error(f"MySQL 连接断开且重连 {self._max_reconnect} 次后仍失败: {e}")
                    raise
                logger.warning(f"MySQL 连接断开（{e}），正在进行第 {attempt + 1} 次自动重连...")
                self._close_thread_conn()
                time.sleep(min(0.5 * (attempt + 1), 3))  # 递增退避，最多 3 秒
    @property
    def pool_status(self) -> dict:
        """获取连接池状态"""
        if self.db_type == 'mysql' and self._pool is not None:
            try:
                checked_out = len(getattr(self._pool, '_usage', {}))
                idle = len(getattr(self._pool, '_idle_cache', []))
                return {
                    'type': self.db_type,
                    'max': self._pool_max,
                    'min_cached': self._pool_min_cached,
                    'max_cached': self._pool_max_cached,
                    'checked_out': checked_out,
                    'idle': idle,
                    'total': checked_out + idle,
                }
            except Exception:
                pass
        return {
            'type': self.db_type,
            'path': getattr(self, '_db_path', None),
        }

    def close(self):
        """关闭连接：MySQL 关闭整个连接池，SQLite 关闭当前线程连接"""
        if self.db_type == 'mysql':
            if self._pool is not None:
                try:
                    self._pool.close()
                except Exception as e:
                    logger.warning(f"关闭 MySQL 连接池失败: {e}")
        else:
            self._close_thread_conn()
