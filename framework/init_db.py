"""
数据库自动初始化模块
支持 MySQL 5.5~8.0（含 MariaDB 10.x）和 SQLite 全自动建表

兼容策略：
- MySQL <5.5.3  : charset=utf8,       collation=utf8_general_ci,        DATETIME→TIMESTAMP
- MySQL 5.5.3~5.6: charset=utf8mb4,   collation=utf8mb4_general_ci,     DATETIME→TIMESTAMP
- MySQL 5.7+    : charset=utf8mb4,   collation=utf8mb4_unicode_ci
- MySQL 8.0+    : charset=utf8mb4,   collation=utf8mb4_unicode_ci（保持兼容，不用 0900_ai_ci）
- MariaDB 10.x  : 按 5.7 对待
- SQLite        : 复用 db.py 的 SQL 翻译逻辑

调用方式：
- db.py 的 init_db() 启动时自动调用 auto_init_database(database)
- 也可独立运行: python -m framework.init_db
"""
import logging
import os
import re

logger = logging.getLogger('zcbot')

# 框架所需的核心表清单（按建表顺序）
_REQUIRED_TABLES = [
    'users', 'groups_info', 'group_members', 'plugins', 'commands',
    'dynamic_commands', 'tasks', 'admin_users', 'audit_logs',
    'plugin_configs', 'system_config', 'group_plugin_settings',
]


def auto_init_database(database) -> bool:
    """
    自动检测并初始化数据库
    如果核心表不存在，自动执行建表脚本
    返回 True 表示执行了初始化，False 表示已存在无需初始化
    """
    try:
        # 检查核心表是否已存在（admin_users 是最关键的表）
        if database.table_exists('admin_users') and database.table_exists('plugins'):
            logger.debug("数据库表已存在，跳过自动初始化")
            return False

        logger.info("检测到数据库表不存在，开始自动初始化...")
        if database.db_type == 'mysql':
            _init_mysql(database)
        else:
            _init_sqlite(database)
        logger.info("数据库自动初始化完成")
        return True
    except Exception as e:
        logger.error(f"数据库自动初始化失败: {e}")
        # 不 raise，让框架继续启动（部分表可能已建好）
        return False


# ── MySQL 初始化 ──────────────────────────────────────────────

def _get_mysql_version(conn) -> dict:
    """获取 MySQL 版本信息"""
    cursor = conn.cursor()
    cursor.execute("SELECT VERSION()")
    version_str = cursor.fetchone()[0]
    cursor.close()

    m = re.match(r'(\d+)\.(\d+)\.(\d+)', str(version_str))
    if not m:
        return {'major': 5, 'minor': 7, 'patch': 0, 'is_mariadb': False, 'str': version_str}

    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    is_mariadb = 'mariadb' in version_str.lower()

    # MariaDB 10.x 对应 MySQL 5.7 级别的功能
    if is_mariadb and major >= 10:
        major, minor = 5, 7

    return {
        'major': major, 'minor': minor, 'patch': patch,
        'is_mariadb': is_mariadb, 'str': version_str,
    }


def _get_charset_collation(ver: dict) -> tuple:
    """根据 MySQL 版本返回 (charset, collation)"""
    major, minor = ver['major'], ver['minor']
    if major < 5 or (major == 5 and minor < 5):
        # <5.5: 只能用 utf8
        return ('utf8', 'utf8_general_ci')
    elif major == 5 and minor == 5:
        # 5.5.x: 支持 utf8mb4 但 collation 只有 general_ci
        return ('utf8mb4', 'utf8mb4_general_ci')
    elif major == 5 and minor == 6:
        # 5.6.x: utf8mb4_unicode_ci 可能有问题，用 general_ci 更稳
        return ('utf8mb4', 'utf8mb4_general_ci')
    else:
        # 5.7+ / 8.0+: 完整支持 unicode_ci
        return ('utf8mb4', 'utf8mb4_unicode_ci')


def _adapt_ddl_for_mysql(ddl: str, ver: dict) -> str:
    """根据 MySQL 版本适配 DDL 语句"""
    major, minor = ver['major'], ver['minor']

    # 5.5/5.6: DATETIME 不支持 DEFAULT CURRENT_TIMESTAMP 和 ON UPDATE CURRENT_TIMESTAMP
    # 改为 TIMESTAMP（5.5 的 TIMESTAMP 支持这两个特性）
    if major == 5 and minor <= 6:
        ddl = ddl.replace(
            'DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
            'TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'
        )
        ddl = ddl.replace(
            'DATETIME DEFAULT CURRENT_TIMESTAMP',
            'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
        )

    return ddl


def _init_mysql(database):
    """MySQL 模式自动建表"""
    config = database.config
    db_name = config.get('database', 'zcbot')

    # 直连 MySQL 服务器（不指定 database），用于创建数据库
    conn = database._pymysql.connect(
        host=config.get('host', '127.0.0.1'),
        port=int(config.get('port', 3306)),
        user=config.get('user', 'root'),
        password=config.get('password', ''),
        charset='utf8mb4',
        autocommit=True,
    )

    try:
        ver = _get_mysql_version(conn)
        charset, collation = _get_charset_collation(ver)
        logger.info(
            f"MySQL 版本: {ver['str']} (MariaDB={ver['is_mariadb']}), "
            f"使用 charset={charset}, collation={collation}"
        )

        cursor = conn.cursor()
        # 创建数据库
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
            f"DEFAULT CHARACTER SET {charset} COLLATE {collation}"
        )
        cursor.execute(f"USE `{db_name}`")
        cursor.close()

        # 读取并执行建表 SQL
        sql_file = _find_sql_file('init.sql')
        if not sql_file:
            logger.warning("未找到 sql/init.sql，跳过 MySQL 自动建表")
            return

        with open(sql_file, 'r', encoding='utf-8') as f:
            sql_content = f.read()

        # 按分号分割并执行
        statements = _split_sql_statements(sql_content)
        cursor = conn.cursor()
        executed = 0
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt or stmt.startswith('--'):
                continue
            # 跳过 CREATE DATABASE / USE 语句（已手动执行）
            upper = stmt.upper()
            if upper.startswith('CREATE DATABASE') or upper.startswith('USE '):
                continue
            # 版本适配
            stmt = _adapt_ddl_for_mysql(stmt, ver)
            try:
                cursor.execute(stmt)
                executed += 1
            except Exception as e:
                # IF NOT EXISTS 已保证不重复建表，此处记录但不中断
                logger.debug(f"SQL 执行跳过: {str(e)[:80]}")
        cursor.close()
        logger.info(f"MySQL 建表完成，执行 {executed} 条语句")

    finally:
        conn.close()


# ── SQLite 初始化 ─────────────────────────────────────────────

def _init_sqlite(database):
    """SQLite 模式自动建表（复用 db.py 的 SQL 翻译）"""
    from framework.db import _translate_sql_for_sqlite

    sql_file = _find_sql_file('init.sql')
    if not sql_file:
        logger.warning("未找到 sql/init.sql，跳过 SQLite 自动建表")
        return

    with open(sql_file, 'r', encoding='utf-8') as f:
        sql_content = f.read()

    statements = _split_sql_statements(sql_content)
    executed = 0
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt or stmt.startswith('--'):
            continue
        # 跳过 MySQL 专有语句
        upper = stmt.upper()
        if upper.startswith('CREATE DATABASE') or upper.startswith('USE '):
            continue
        # 翻译为 SQLite 兼容语法
        stmt = _translate_sql_for_sqlite(stmt)
        try:
            database.execute(stmt)
            executed += 1
        except Exception as e:
            logger.debug(f"SQLite 建表跳过: {str(e)[:80]}")
    logger.info(f"SQLite 建表完成，执行 {executed} 条语句")


# ── 工具函数 ──────────────────────────────────────────────────

def _find_sql_file(filename: str) -> str:
    """查找 SQL 文件（支持 sql/ 目录和项目根目录）"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base_dir, 'sql', filename),
        os.path.join(base_dir, filename),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _split_sql_statements(sql: str) -> list:
    """
    按分号分割 SQL 语句
    智能跳过字符串内的分号和注释行
    """
    statements = []
    current = []
    in_string = False
    quote_char = None
    in_comment = False

    i = 0
    while i < len(sql):
        char = sql[i]

        # 处理单行注释
        if not in_string and char == '-' and i + 1 < len(sql) and sql[i + 1] == '-':
            # 跳过到行尾
            while i < len(sql) and sql[i] != '\n':
                current.append(sql[i])
                i += 1
            continue

        # 处理字符串
        if char in ("'", '"') and not in_comment:
            if not in_string:
                in_string = True
                quote_char = char
            elif quote_char == char:
                in_string = False
                quote_char = None

        current.append(char)

        # 语句结束
        if char == ';' and not in_string:
            statements.append(''.join(current))
            current = []

        i += 1

    if current:
        stmt = ''.join(current).strip()
        if stmt:
            statements.append(stmt)

    return statements


# ── 独立运行入口 ──────────────────────────────────────────────

def _main():
    """独立运行: python -m framework.init_db [config.yaml]"""
    import sys
    from framework.config import load_config
    from framework.db import init_db, _parse_sqlite_type

    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)

    print(f"数据库配置: {config.get('database', {})}")
    db_config = _parse_sqlite_type(config.get('database', {}))

    print(f"正在初始化 {db_config.get('type', 'sqlite')} 数据库...")
    db = init_db(config.get('database', {}))

    # 强制重新检查并初始化
    result = auto_init_database(db)
    if result:
        print("数据库初始化成功")
    else:
        print("数据库表已存在，无需初始化")

    db.close()


if __name__ == '__main__':
    _main()
