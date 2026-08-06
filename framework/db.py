"""
数据库操作模块
支持 SQLite（默认）和 MySQL（可选），自动适配。

设计原则：
- 默认使用 SQLite，零配置开箱即用
- 配置文件中配置 database.type: mysql 时使用 MySQL
- 自动处理 %s → ? 占位符转换（插件无需修改 SQL 语法）
- 自动处理 DDL 语法差异（ENGINE=、COMMENT、AUTO_INCREMENT 等）
- 自动处理 NOW() → datetime 参数转换
"""
import functools
import json
import logging
import os
import re
import sqlite3
import time
from threading import local

logger = logging.getLogger('zcbot')

# MySQL 连接断开类错误码（触发自动重连）
_MYSQL_RECONNECT_ERRORS = {2006, 2013, 2055, 1927, 1040}
# 连接断开类错误关键字（用于兜底判断）
_MYSQL_RECONNECT_KEYWORDS = (
    'server has gone away',
    'lost connection',
    'connection is closed',
    'broken pipe',
    'connection reset by peer',
)

# ── SQL 适配器 ──────────────────────────────────────────────────────

# 预编译正则，加速替换
_RE_ENGINE = re.compile(r'\s+ENGINE\s*=\s*\S+', re.IGNORECASE)
_RE_CHARSET = re.compile(r'\s+(DEFAULT\s+)?(CHARSET|CHARACTER\s+SET)\s*=\s*\S+', re.IGNORECASE)
_RE_COLLATE = re.compile(r'\s+COLLATE\s*=\s*\S+', re.IGNORECASE)
_RE_COLLATE_INLINE = re.compile(r'\s+COLLATE\s+\S+', re.IGNORECASE)
# COMMENT 'xxx'：用非贪婪匹配引号内容，支持引号内含括号/分号等特殊字符
_RE_COMMENT = re.compile(r"\s+COMMENT\s+'[^']*'", re.IGNORECASE)
# COMMENT='xxx'（MySQL 表级/列级等号写法）
_RE_COMMENT_EQ = re.compile(r"\s+COMMENT\s*=\s*'[^']*'", re.IGNORECASE)
_RE_AUTO_INCREMENT = re.compile(r'\s*AUTO_INCREMENT\b', re.IGNORECASE)
_RE_UNSIGNED = re.compile(r'\s+UNSIGNED\b', re.IGNORECASE)
_RE_FOR_UPDATE = re.compile(r'\s+FOR\s+UPDATE\b', re.IGNORECASE)
# ON UPDATE CURRENT_TIMESTAMP（SQLite 不支持）
_RE_ON_UPDATE = re.compile(r'\s+ON\s+UPDATE\s+[^\s,)]+', re.IGNORECASE)
_RE_AFTER = re.compile(r'\s+AFTER\s+\S+', re.IGNORECASE)
_RE_ON_DUP_KEY = re.compile(
    r'\s+ON\s+DUPLICATE\s+KEY\s+UPDATE\s+(.+?)(?=\s*;|\s*$)',
    re.IGNORECASE | re.DOTALL
)
# ENUM('a','b',...)：支持嵌套引号和逗号，匹配到对应的右括号
_RE_ENUM = re.compile(r'\bENUM\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)', re.IGNORECASE)


def _strip_mysql_ddl_syntax(sql: str) -> str:
    """
    将 MySQL DDL 语法翻译为 SQLite 兼容语法
    只做语法层面的清理，不做逻辑转换
    """
    sql = _RE_ENGINE.sub('', sql)
    sql = _RE_CHARSET.sub('', sql)
    sql = _RE_COLLATE.sub('', sql)
    sql = _RE_COLLATE_INLINE.sub('', sql)
    sql = _RE_COMMENT.sub('', sql)
    sql = _RE_COMMENT_EQ.sub('', sql)
    sql = _RE_AUTO_INCREMENT.sub('', sql)
    sql = _RE_UNSIGNED.sub('', sql)
    sql = _RE_FOR_UPDATE.sub('', sql)
    sql = _RE_ON_UPDATE.sub('', sql)
    sql = _RE_AFTER.sub('', sql)

    # 数据类型转换
    sql = re.sub(r'\bBIGINT\b', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTINYINT\s*\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTINYINT\b', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bVARCHAR\s*\(\d+\)', 'TEXT', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bDATETIME\b', 'TEXT', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTIMESTAMP\b', 'TEXT', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bINT\s*\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(?<!\w)INT(?!\s*\(\d+)(?!\w)', 'INTEGER', sql, flags=re.IGNORECASE)
    # ENUM(...) → TEXT（支持嵌套括号）
    sql = _RE_ENUM.sub('TEXT', sql)

    # UNIQUE KEY uk_name (col) → UNIQUE(col)
    sql = re.sub(
        r'\bUNIQUE\s+KEY\s+\S+\s+\(([^)]+)\)',
        r'UNIQUE(\1)',
        sql, flags=re.IGNORECASE
    )
    # INDEX idx_name (col) → 删除（SQLite DDL 内不建索引）
    sql = re.sub(
        r',?\s*\bINDEX\s+\S+\s*\([^)]+\)',
        '',
        sql, flags=re.IGNORECASE
    )
    # KEY uk_name (col) → 删除
    sql = re.sub(
        r',?\s*\bKEY\s+\S+\s*\([^)]+\)',
        '',
        sql, flags=re.IGNORECASE
    )

    # 清理多余的逗号（在 ) 前面）
    sql = re.sub(r',\s*\)', ')', sql)

    # 清理多余空格
    sql = re.sub(r'\s+', ' ', sql).strip()

    return sql


def _convert_placeholders(sql: str) -> str:
    """将 %s 占位符转换为 ?（SQLite 用）"""
    return sql.replace('%s', '?')


def _is_ddl_or_dml(sql: str) -> bool:
    """判断是否需要语法翻译（跳过前导注释行）DDL + INSERT/UPDATE/DELETE 都需要"""
    for line in sql.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('--'):
            continue
        return line.upper().startswith((
            'CREATE', 'ALTER', 'DROP', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE'
        ))
    return False


def _split_top_level(text: str) -> list:
    """
    按顶层逗号分割（忽略括号内与字符串内的逗号）
    用于解析 IF(cond, a, b) 的三个参数
    """
    parts = []
    depth = 0
    in_str = False
    quote = None
    cur = []
    for c in text:
        if not in_str and c in ("'", '"'):
            in_str = True
            quote = c
            cur.append(c)
            continue
        if in_str:
            cur.append(c)
            if c == quote:
                in_str = False
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        if c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if cur:
        parts.append(''.join(cur).strip())
    return parts


def _if_to_case(sql: str) -> str:
    """
    将 MySQL 的 IF(cond, a, b) 转换为 SQLite 兼容的 CASE WHEN cond THEN a ELSE b END
    支持嵌套括号与字符串字面量，IFNULL 单独用正则处理
    """
    out = []
    i = 0
    n = len(sql)
    in_str = False
    quote = None
    while i < n:
        ch = sql[i]
        if not in_str and ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if in_str:
            out.append(ch)
            if ch == quote:
                in_str = False
            i += 1
            continue
        # 匹配单词边界后的 IF(
        if (sql[i:i + 2].upper() == 'IF'
                and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == '_'))):
            k = i + 2
            while k < n and sql[k] in ' \t\n\r':
                k += 1
            if k < n and sql[k] == '(':
                # 扫描到匹配的右括号
                j = k + 1
                depth = 1
                s_in_str = False
                s_quote = None
                while j < n and depth > 0:
                    c = sql[j]
                    if not s_in_str and c in ("'", '"'):
                        s_in_str = True
                        s_quote = c
                    elif s_in_str:
                        if c == s_quote:
                            s_in_str = False
                    elif c == '(':
                        depth += 1
                    elif c == ')':
                        depth -= 1
                    j += 1
                if depth == 0:
                    body = sql[k + 1:j - 1]
                    parts = _split_top_level(body)
                    if len(parts) == 3:
                        cond, a, b = parts
                        out.append(f"CASE WHEN {cond} THEN {a} ELSE {b} END")
                        i = j
                        continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _translate_mysql_funcs(sql: str) -> str:
    """
    将 MySQL 专有函数转换为 SQLite 兼容语法：
    - IF(cond, a, b) → CASE WHEN cond THEN a ELSE b END
    - IFNULL(a, b)   → COALESCE(a, b)
    - NOW()          → 由 execute/insert 的 _replace_now 处理
    """
    # IFNULL 参数简单，用正则即可
    sql = re.sub(
        r'\bIFNULL\s*\(\s*([^,()]+)\s*,\s*([^,()]+)\s*\)',
        r'COALESCE(\1, \2)',
        sql, flags=re.IGNORECASE
    )
    return _if_to_case(sql)


def _translate_sql_for_sqlite(sql: str) -> str:
    """
    完整翻译 SQL 供 SQLite 使用：
    1. DDL 语法清理
    2. MySQL 专有函数（IF/IFNULL）→ SQLite 兼容
    3. ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE
    4. INSERT IGNORE → INSERT OR IGNORE
    5. NOW() → 由调用方处理参数
    6. %s → ?
    所有需要翻译的 SQL（DDL/DML）都走这个函数，统一入口
    """
    needs_translate = _is_ddl_or_dml(sql)

    if needs_translate:
        sql = _strip_mysql_ddl_syntax(sql)

        # MySQL 专有函数 → SQLite 兼容（IF / IFNULL）
        sql = _translate_mysql_funcs(sql)

        # ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE SET
        if 'ON DUPLICATE KEY' in sql.upper() and 'INSERT' in sql.upper():
            sql = _on_duplicate_to_sqlite(sql)

        # INSERT IGNORE → INSERT OR IGNORE
        sql = re.sub(r'\bINSERT\s+IGNORE\b', 'INSERT OR IGNORE', sql, flags=re.IGNORECASE)

    # %s → ?（所有 SQL 都需要转）
    sql = _convert_placeholders(sql)

    return sql


# ── 数据库引擎 ──────────────────────────────────────────────────────

class Database:
    """
    数据库连接管理器
    支持 SQLite（默认）和 MySQL（可选）
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

    def _init_sqlite(self):
        """初始化 SQLite"""
        db_path = self.config.get('path', 'data/zcbot.db')
        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self._db_path = db_path
        logger.info(f"SQLite 数据库已初始化: {db_path}")

    def _init_mysql(self):
        """初始化 MySQL（检测到 MySQL 配置时，自动安装 pymysql）"""
        try:
            import pymysql
            from pymysql.cursors import DictCursor
            self._pymysql = pymysql
            self._DictCursor = DictCursor
            logger.info("MySQL 模式已启用")
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
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _get_conn_mysql(self):
        """
        获取 MySQL 连接
        自动保活：连接空闲超过 ping_interval 时先 ping 检测，
        连接被服务端断开（wait_timeout/重启/网络中断）则自动重连。
        """
        conn = getattr(self._local, 'conn', None)
        now = time.time()
        last_use = getattr(self._local, 'last_use', 0.0)

        if conn is not None and conn.open:
            # 空闲超过阈值 → ping 检测（ping 失败会抛异常，触发重连）
            if now - last_use >= self._ping_interval:
                try:
                    conn.ping(reconnect=True)
                except Exception as e:
                    logger.warning(f"MySQL 连接已失效，正在自动重连: {e}")
                    self._close_thread_conn()
                    conn = None
            else:
                return conn

        if conn is None:
            try:
                conn = self._pymysql.connect(
                    host=self.config.get('host', '127.0.0.1'),
                    port=int(self.config.get('port', 3306)),
                    user=self.config.get('user', 'root'),
                    password=self.config.get('password', ''),
                    database=self.config.get('database', 'zcbot'),
                    charset=self.config.get('charset', 'utf8mb4'),
                    cursorclass=self._DictCursor,
                    autocommit=True,
                    connect_timeout=self._connect_timeout,
                    read_timeout=self._read_timeout,
                    write_timeout=self._write_timeout,
                )
                self._local.conn = conn
                self._local.last_use = time.time()
                logger.info("MySQL 连接已建立")
            except Exception as e:
                logger.error(f"MySQL 连接建立失败: {e}")
                raise
        return conn

    def _close_thread_conn(self):
        """关闭并丢弃当前线程缓存的连接（下次操作自动重连）"""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

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
        """获取连接"""
        if self.db_type == 'mysql':
            return self._get_conn_mysql()
        return self._get_conn_sqlite()

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
                if attempt >= self._max_reconnect:
                    logger.error(f"MySQL 连接断开且重连 {self._max_reconnect} 次后仍失败: {e}")
                    raise
                logger.warning(f"MySQL 连接断开（{e}），正在进行第 {attempt + 1} 次自动重连...")
                self._close_thread_conn()
                time.sleep(min(0.5 * (attempt + 1), 3))  # 递增退避，最多 3 秒

    # ── 公开 API ──────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = None) -> list:
        """查询多条记录，返回 list[dict]"""
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                if self.db_type == 'sqlite':
                    sql = _translate_sql_for_sqlite(sql)
                self._exec(cursor, sql, params)
                rows = cursor.fetchall()
                if self.db_type == 'sqlite':
                    return [dict(r) for r in rows]
                return rows
            finally:
                cursor.close()
        return self._run_with_reconnect(_do, sql, params)

    def query_one(self, sql: str, params: tuple = None) -> dict:
        """查询单条记录，返回 dict 或 None"""
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                if self.db_type == 'sqlite':
                    sql = _translate_sql_for_sqlite(sql)
                self._exec(cursor, sql, params)
                row = cursor.fetchone()
                if row is None:
                    return None
                if self.db_type == 'sqlite':
                    return dict(row)
                return row
            finally:
                cursor.close()
        return self._run_with_reconnect(_do, sql, params)

    def _exec(self, cursor, sql: str, params=None):
        """执行 sql，自动处理 params 为 None 的情况"""
        if params is not None:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

    def execute(self, sql: str, params: tuple = None) -> int:
        """执行插入/更新/删除，返回受影响行数"""
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                if self.db_type == 'sqlite':
                    # 先处理 NOW()（基于 %s 占位符交错参数），再翻译 %s→? 等
                    if 'NOW()' in sql.upper():
                        sql, params = _replace_now(sql, params)
                    sql = _translate_sql_for_sqlite(sql)
                self._exec(cursor, sql, params)
                conn.commit()
                return cursor.rowcount
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()
        return self._run_with_reconnect(_do, sql, params)

    def execute_many(self, sql: str, params_list: list) -> int:
        """批量执行，返回受影响行数"""
        def _do(sql, params_list):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                if self.db_type == 'sqlite':
                    # 先处理 NOW()（基于 %s 占位符交错参数），再翻译 %s→? 等
                    if 'NOW()' in sql.upper():
                        new_sql, _ = _replace_now(sql, params_list[0] if params_list else None)
                        new_params_list = []
                        for p in params_list:
                            _, now_p = _replace_now(sql, p)
                            new_params_list.append(now_p)
                        sql = new_sql
                        params_list = new_params_list
                    sql = _translate_sql_for_sqlite(sql)
                cursor.executemany(sql, params_list)
                conn.commit()
                return cursor.rowcount
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()
        return self._run_with_reconnect(_do, sql, params_list)

    def insert(self, sql: str, params: tuple = None) -> int:
        """插入并返回自增 ID"""
        def _do(sql, params):
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                if self.db_type == 'sqlite':
                    # 先处理 NOW()（基于 %s 占位符交错参数），再翻译 %s→? 等
                    if 'NOW()' in sql.upper():
                        sql, params = _replace_now(sql, params)
                    sql = _translate_sql_for_sqlite(sql)
                self._exec(cursor, sql, params)
                conn.commit()
                return cursor.lastrowid
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()
        return self._run_with_reconnect(_do, sql, params)

    def get_connection(self):
        """获取原始连接（高级用法）"""
        return self._get_conn()

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

    @property
    def pool_status(self) -> dict:
        """获取连接池状态"""
        return {
            'type': self.db_type,
            'path': getattr(self, '_db_path', None),
        }

    def close(self):
        """关闭连接"""
        self._close_thread_conn()


# ── SQL 辅助函数 ──────────────────────────────────────────────────

def _on_duplicate_to_sqlite(sql: str) -> str:
    """
    将 MySQL 的 INSERT ... ON DUPLICATE KEY UPDATE 转换为
    SQLite 的 INSERT ... ON CONFLICT(...) DO UPDATE SET ...
    """
    # 提取列名（ON DUPLICATE KEY 前的 INSERT 部分）
    # 简化实现：直接替换为 INSERT OR REPLACE（更安全）
    # 对于复杂场景，使用 ON CONFLICT
    # 先尝试从 UNIQUE KEY 提取列名
    # 简化：直接替换 ON DUPLICATE KEY UPDATE 为 ON CONFLICT DO UPDATE
    m = _RE_ON_DUP_KEY.search(sql)
    if not m:
        return sql

    update_clause = m.group(1)
    # 将 VALUES(col) 替换为 EXCLUDED.col
    update_clause = re.sub(r'VALUES\((\w+)\)', r'EXCLUDED.\1', update_clause)
    # 替换为 SQLite 语法
    sql = _RE_ON_DUP_KEY.sub(f' ON CONFLICT DO UPDATE SET {update_clause}', sql)

    return sql


def _replace_now(sql: str, params: tuple = None) -> tuple:
    """
    将 SQL 中的 NOW() 替换为 ?，并按位置插入当前时间参数。
    SQLite 模式专用：NOW() 可能出现在语句中间（如 SET token_created_at = NOW(), ... WHERE id = %s），
    必须按占位符出现顺序与原参数交错插入，否则参数错位。
    """
    import datetime
    if 'NOW()' not in sql.upper():
        return sql, params

    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 从左到右扫描：%s → 原参数，NOW() → 时间参数，交错生成
    tokens = re.split(r'(%s|NOW\(\))', sql, flags=re.IGNORECASE)
    new_sql_parts = []
    new_params = []
    param_iter = iter(params) if params else iter(())

    for tok in tokens:
        if not tok:
            continue
        if tok == '%s':
            try:
                new_params.append(next(param_iter))
            except StopIteration:
                new_params.append(None)
            new_sql_parts.append('?')
        elif tok.upper() == 'NOW()':
            new_params.append(now_str)
            new_sql_parts.append('?')
        else:
            new_sql_parts.append(tok)

    return ''.join(new_sql_parts), tuple(new_params)


# ── 全局单例 ──────────────────────────────────────────────────────

db: Database = None


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
    """初始化数据库（全局单例）"""
    global db

    # 解析配置
    db_config = _parse_sqlite_type(config)
    db = Database(db_config)

    # 自动检测并初始化数据库表（MySQL 5.5~8.0 / SQLite 全兼容）
    from framework.init_db import auto_init_database
    auto_init_database(db)

    # 创建框架扩展表 + 迁移（兼容旧版升级）
    _auto_create_tables(db)
    return db


def _auto_create_tables(database):
    """自动创建框架所需的扩展表"""
    tables = {
        'group_plugin_settings': """
            CREATE TABLE IF NOT EXISTS group_plugin_settings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    INTEGER NOT NULL,
                plugin_name TEXT NOT NULL,
                enabled     INTEGER DEFAULT 1,
                updated_at  TEXT,
                UNIQUE(group_id, plugin_name)
            )
        """,
        # IP 黑名单表（蜜罐自动拉黑 + 手动拉黑）
        'ip_blacklist': """
            CREATE TABLE IF NOT EXISTS ip_blacklist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip          TEXT NOT NULL,
                reason      TEXT,
                source      TEXT DEFAULT 'manual',
                expires_at  TEXT,
                created_at  TEXT,
                updated_at  TEXT,
                UNIQUE(ip)
            )
        """,
    }

    # MySQL 模式下替换 AUTOINCREMENT → AUTO_INCREMENT
    if database.db_type == 'mysql':
        mysql_tables = {}
        for name, ddl in tables.items():
            ddl = ddl.replace('AUTOINCREMENT', 'AUTO_INCREMENT')
            mysql_tables[name] = ddl
        tables = mysql_tables
    for name, ddl in tables.items():
        try:
            database.execute(ddl)
            logger.debug(f"自动建表: {name}")
        except Exception as e:
            logger.warning(f"自动建表失败 [{name}]: {e}")

    # 迁移：给 commands 表追加 require_level 列
    _migrate_commands_table(database)

    # 迁移：给 users 表追加 role 列
    _migrate_users_table(database)

    # 迁移：给 admin_users 表追加 token 列（token 认证）
    _migrate_admin_users_table(database)


def _migrate_commands_table(database):
    """迁移 commands 表添加 require_level 列"""
    try:
        if database.table_exists('commands') and \
           not database.table_has_column('commands', 'require_level'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_level TEXT DEFAULT ''"
                )
            else:
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_level VARCHAR(20) DEFAULT '' "
                    "COMMENT '权限要求: admin=管理员/群主/超管, super=超管'"
                )
            logger.info("数据库迁移: commands 表添加 require_level 列")
    except Exception:
        pass


def _migrate_users_table(database):
    """迁移 users 表添加 role 列"""
    try:
        if database.table_exists('users') and \
           not database.table_has_column('users', 'role'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT DEFAULT ''"
                )
            else:
                database.execute(
                    "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT '' "
                    "COMMENT '权限角色: super=超级管理员, 空=普通用户'"
                )
            logger.info("数据库迁移: users 表添加 role 列")
    except Exception:
        pass


def _migrate_admin_users_table(database):
    """迁移 admin_users 表添加 token 和 token_created_at 列"""
    try:
        if not database.table_exists('admin_users'):
            return

        if not database.table_has_column('admin_users', 'token'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token TEXT DEFAULT NULL"
                )
            else:
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token VARCHAR(2048) DEFAULT NULL "
                    "COMMENT '登录令牌(2048位随机)'"
                )
            logger.info("数据库迁移: admin_users 表添加 token 列")

        if not database.table_has_column('admin_users', 'token_created_at'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token_created_at TEXT DEFAULT NULL"
                )
            else:
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token_created_at DATETIME DEFAULT NULL "
                    "COMMENT '令牌签发时间'"
                )
            logger.info("数据库迁移: admin_users 表添加 token_created_at 列")
    except Exception:
        pass