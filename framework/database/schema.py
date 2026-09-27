"""
框架 schema：自动建表 + 运行时迁移（SQLite / MySQL 双方言）

只管「表结构」：框架扩展表的 CREATE TABLE 与历史版本的列/索引补丁。
连接与执行在 db.Database；SQL 方言翻译在 dialect.py。
"""
import logging
import time

logger = logging.getLogger('zcbot')

# 身份 ID 列白名单：这些列在多种平台（QQ 官方 openid 等）下是字符串，
# 绝对不能建成 BIGINT/INTEGER。建表/迁移时统一约束为 VARCHAR(64)。
# 覆盖 init.sql 核心表 + 本模块扩展表两处建表路径。
_IDENTITY_ID_COLUMNS = {
    'users': ('user_id',),
    'groups_info': ('group_id',),
    'group_members': ('group_id', 'user_id'),
    'perm_user_nodes': ('user_id',),
    'group_plugin_settings': ('group_id',),
}
_IDENTITY_COLUMN_TYPE = 'VARCHAR(64)'


def _migrate_identity_id_columns(database):
    """把身份 ID 列统一迁移为 VARCHAR(64)

    openid 是字符串（QQ 官方机器人等平台），若建表时被建成 BIGINT/INTEGER，
    写入 openid（或超长数字 QQ 号）会 datatype mismatch 或溢出。这里对
    白名单内的表做幂等校验：类型不是 VARCHAR(64) 则修正（MySQL MODIFY、
    SQLite 宽松类型无强制，无需改动但做一致性确认）。
    """
    for table, columns in _IDENTITY_ID_COLUMNS.items():
        if not database.table_exists(table):
            continue
        for col in columns:
            try:
                cols = database.table_info(table)
                info = None
                if database.db_type == 'sqlite':
                    info = next((r for r in cols if r.get('name') == col), None)
                else:
                    info = next((r for r in cols if r.get('Field') == col), None)
                if not info:
                    continue
                cur_type = (info.get('type') or '').upper() if database.db_type == 'sqlite' \
                    else (info.get('Type') or '').upper()
                if cur_type.startswith(('VARCHAR', 'TEXT', 'CHAR')):
                    continue  # 已符合
                # 修正类型：MySQL MODIFY；SQLite 重建表（BIGINT 声明会把
                # 19 位以上数字 openid 静默转成浮点丢精度，不能只告警）
                if database.db_type == 'mysql':
                    database.execute(
                        f"ALTER TABLE `{table}` MODIFY COLUMN `{col}` "
                        f"{_IDENTITY_COLUMN_TYPE} NOT NULL"
                    )
                    logger.info(
                        f"数据库迁移: {table}.{col} {cur_type} → {_IDENTITY_COLUMN_TYPE}"
                    )
                else:
                    _rebuild_sqlite_text_column(database, table, col)
            except Exception as e:
                logger.warning(f"数据库迁移 {table}.{col} 类型约束失败: {e}")


def _auto_create_tables(database):
    """自动创建框架所需的扩展表"""
    # 列类型统一用 VARCHAR（SQLite 宽松类型同样兼容）：
    # - TEXT 列不能作为 MySQL 索引键（缺 key length）
    # - TEXT 列不能带 DEFAULT（MySQL 报错）
    tables = {
        'group_plugin_settings': """
            CREATE TABLE IF NOT EXISTS group_plugin_settings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    VARCHAR(64) NOT NULL,
                plugin_name VARCHAR(64) NOT NULL,
                enabled     INTEGER DEFAULT 1,
                updated_at  VARCHAR(32),
                UNIQUE(group_id, plugin_name)
            )
        """,
        # IP 黑名单表（蜜罐自动拉黑 + 手动拉黑）
        'ip_blacklist': """
            CREATE TABLE IF NOT EXISTS ip_blacklist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip          VARCHAR(64) NOT NULL,
                reason      VARCHAR(255),
                source      VARCHAR(32) DEFAULT 'manual',
                expires_at  VARCHAR(32),
                created_at  VARCHAR(32),
                updated_at  VARCHAR(32),
                UNIQUE(ip)
            )
        """,

        # ── 权限系统（节点式）─────────────────────────────
        # node = 'group.xxx' 表示继承/加入 xxx 组；context_* 为 NULL = 全局生效
        # 时间字段统一用 VARCHAR(32) 存 unix 时间戳字符串（SQLite/MySQL 一致）
        # 注意：SQLite 不支持 CREATE TABLE 内联 INDEX，索引由 _migrate_perm_tables 补建
        'perm_groups': """
            CREATE TABLE IF NOT EXISTS perm_groups (
                name         VARCHAR(64)  NOT NULL PRIMARY KEY,
                display_name VARCHAR(100) DEFAULT NULL,
                weight       INTEGER      DEFAULT 0,
                prefix       VARCHAR(64)  DEFAULT NULL,
                suffix       VARCHAR(64)  DEFAULT NULL,
                is_default   INTEGER      DEFAULT 0,
                created_at   VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_group_nodes': """
            CREATE TABLE IF NOT EXISTS perm_group_nodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name  VARCHAR(64)  NOT NULL,
                node        VARCHAR(191) NOT NULL,
                value       INTEGER      DEFAULT 1,
                context_key VARCHAR(32)  DEFAULT NULL,
                context_val VARCHAR(64)  DEFAULT NULL,
                expire_at   VARCHAR(32)  DEFAULT NULL,
                created_at  VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_user_nodes': """
            CREATE TABLE IF NOT EXISTS perm_user_nodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     VARCHAR(64)  NOT NULL,
                node        VARCHAR(191) NOT NULL,
                value       INTEGER      DEFAULT 1,
                context_key VARCHAR(32)  DEFAULT NULL,
                context_val VARCHAR(64)  DEFAULT NULL,
                expire_at   VARCHAR(32)  DEFAULT NULL,
                created_at  VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_tracks': """
            CREATE TABLE IF NOT EXISTS perm_tracks (
                name         VARCHAR(64)  NOT NULL PRIMARY KEY,
                display_name VARCHAR(100) DEFAULT NULL,
                groups_order VARCHAR(500) NOT NULL,
                created_at   VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_audit': """
            CREATE TABLE IF NOT EXISTS perm_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                operator    VARCHAR(100) DEFAULT NULL,
                action      VARCHAR(32)  DEFAULT NULL,
                target_type VARCHAR(16)  DEFAULT NULL,
                target      VARCHAR(100) DEFAULT NULL,
                node        VARCHAR(191) DEFAULT NULL,
                value       INTEGER      DEFAULT NULL,
                context     VARCHAR(120) DEFAULT NULL,
                detail      VARCHAR(500) DEFAULT NULL,
                created_at  VARCHAR(32)  DEFAULT NULL
            )
        """,

        # ── 接口令牌（API Key）──────────────────────────────────
        # 与用户会话 token 解耦：不随登录/登出轮换，可长期有效，专供外部程序调 REST API
        # token 长度不固定（>=40 字符），故不放进 _verify_token 的 2048 长度校验分支
        # expires_at / last_used_at / created_at 统一存 unix 时间戳字符串（SQLite/MySQL 一致）
        'api_tokens': """
            CREATE TABLE IF NOT EXISTS api_tokens (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                token        VARCHAR(512) NOT NULL,
                name         VARCHAR(100) NOT NULL,
                role         VARCHAR(20)  DEFAULT 'admin',
                created_by   VARCHAR(100) DEFAULT NULL,
                created_at   VARCHAR(32)  DEFAULT NULL,
                expires_at   VARCHAR(32)  DEFAULT NULL,
                last_used_at VARCHAR(32)  DEFAULT NULL,
                is_active    INTEGER      DEFAULT 1,
                UNIQUE(token)
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

    # 迁移：commands 表补唯一索引（ON DUPLICATE KEY UPDATE 的去重依据）
    _migrate_commands_unique_index(database)

    # 迁移：身份 ID 列统一为 VARCHAR(64)（开放平台 openid 是字符串）
    _migrate_identity_id_columns(database)

    # 迁移：给 users 表追加 role 列
    _migrate_users_table(database)

    # 迁移：给 commands 表追加 require_perm 列（权限组节点要求）
    _migrate_commands_require_perm(database)

    # 迁移：给 admin_users 表追加 token 列（token 认证）
    _migrate_admin_users_table(database)

    # 迁移：dynamic_commands.match_type ENUM 增加 contains（更开放的匹配方式）
    _migrate_dynamic_commands_table(database)

    # 迁移：dynamic_commands 表增加 handler 列（关键词 handler 回调）
    _migrate_dynamic_commands_handler(database)

    # 迁移：权限系统表补索引 + 播种默认组（表本体已在上方 tables 字典建好）
    _migrate_perm_tables(database)


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


def _rebuild_sqlite_text_column(database, table, column):
    """SQLite 幂等重建表：把指定身份列改为 TEXT 声明（保留数据与约束）

    SQLite 的 BIGINT/INTEGER 声明列是 NUMERIC 亲和类型：写入 19 位以上数字
    openid 会被静默转成 REAL 浮点丢精度（如 12345678901234567890 →
    1.2345678901234567e+19）。必须重建表为 TEXT 声明才能按原文存放字符串。
    重建流程（事务内，失败回滚）：
      1. 读取原表 CREATE 语句中的列定义；
      2. 构造新表（目标列改 TEXT，其余原样）；
      3. 拷贝数据 → 删旧表 → 新表改名 → 重建索引与 UNIQUE 约束。
    """
    # 仅处理确实需要改的类型（幂等：已是 TEXT/VARCHAR 直接返回）
    col_defs = database.table_info(table)
    if not col_defs:
        return
    cur = next((r.get('type') or '').upper() for r in col_defs if r.get('name') == column)
    if cur.startswith(('VARCHAR', 'TEXT', 'CHAR')):
        return

    # 0) 备份原表索引 DDL（重命名/删表后索引随之消失，需在事务外先取到 SQL）
    idx_rows = database.query(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name=? AND sql IS NOT NULL", (table,))

    # 1) 重命名旧表
    old = f"{table}__idtype_old"
    new = f"{table}__idtype_new"
    database.execute(f"ALTER TABLE {table} RENAME TO {old}")

    # 2) 构造新表 DDL：仅替换目标列类型为 TEXT，其余列定义与约束原样保留
    lines = []
    for r in col_defs:
        name = r['name']
        typ = r['type']
        notnull = ' NOT NULL' if r.get('notnull') else ''
        dflt = r.get('dflt_value')
        pk = r.get('pk')
        # 目标列 → TEXT；主键列保持 INTEGER PRIMARY KEY AUTOINCREMENT（自增语义）
        if name == column:
            lines.append(f"    {name} TEXT{notnull}")
        elif pk:
            lines.append(f"    {name} INTEGER PRIMARY KEY AUTOINCREMENT")
        else:
            default_sql = ''
            if dflt is not None:
                if isinstance(dflt, str) and dflt.startswith("'") and dflt.endswith("'"):
                    default_sql = f" DEFAULT {dflt}"
                else:
                    default_sql = f" DEFAULT {dflt}"
            lines.append(f"    {name} {typ}{notnull}{default_sql}")
    # 原表可能有表级约束（UNIQUE/CHECK/PRIMARY KEY 等），从 sqlite_master 的
    # CREATE 语句中正则提取（SQLite 存储的 SQL 是单行拼接，不能用 splitlines）
    row = database.query_one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (old,))
    src_sql = (row or {}).get('sql') or ''
    import re as _re
    for m in _re.finditer(r'\b(UNIQUE\s*\([^)]*\)|CHECK\s*\([^)]*\)|PRIMARY\s+KEY\s*\([^)]*\))',
                          src_sql, _re.IGNORECASE):
        lines.append(f"    {m.group(0)}")
    new_ddl = f"CREATE TABLE {new} (\n" + ",\n".join(lines) + "\n)"

    try:
        database.execute(new_ddl)
        cols = ", ".join(r['name'] for r in col_defs)
        database.execute(f"INSERT INTO {new} ({cols}) SELECT {cols} FROM {old}")
        database.execute(f"DROP TABLE {old}")
        database.execute(f"ALTER TABLE {new} RENAME TO {table}")
        # 重建普通索引（UNIQUE 隐式索引随新表 DDL 的约束自动重建）
        for ir in idx_rows:
            try:
                database.execute(ir['sql'])
            except Exception:
                pass
        logger.info(
            f"数据库迁移: {table}.{column} 声明 {cur} → TEXT（重建表保留数据）")
    except Exception:
        # 回滚：删除新表，恢复旧表名
        try:
            database.execute(f"DROP TABLE IF EXISTS {new}")
        except Exception:
            pass
        try:
            database.execute(f"ALTER TABLE {old} RENAME TO {table}")
        except Exception:
            pass
        raise


def _migrate_commands_unique_index(database):
    """commands 表补唯一索引 uk_plugin_handler（ON DUPLICATE KEY UPDATE 的去重依据）

    历史库建表时 commands 只有普通 INDEX，没有 UNIQUE 约束，
    loader._sync_commands 的 INSERT ... ON DUPLICATE KEY UPDATE 永远触发 INSERT 分支
    （SQLite 方言翻译后的 ON CONFLICT DO UPDATE 同样无冲突可触发），
    心跳刷新逐次堆重复行。迁移两件事：
    1. 按 (plugin_name, handler) 清洗历史重复行（保留 id 最小的一条）；
    2. 幂等补建唯一约束：MySQL ALTER TABLE ADD UNIQUE KEY，
       SQLite CREATE UNIQUE INDEX IF NOT EXISTS（方言层原样放行）。
    顺序不可颠倒：MySQL 在存在重复数据时 ALTER 加唯一键会报 Duplicate entry。
    """
    try:
        if not database.table_exists('commands'):
            return
        # 1) 清洗历史重复：同一 (plugin_name, handler) 只保留 id 最小的一条
        if database.db_type == 'sqlite':
            database.execute(
                "DELETE FROM commands WHERE id NOT IN "
                "(SELECT MIN(id) FROM commands GROUP BY plugin_name, handler)"
            )
        else:
            database.execute(
                "DELETE c1 FROM commands c1 "
                "JOIN commands c2 ON c1.plugin_name = c2.plugin_name "
                "AND c1.handler = c2.handler AND c1.id > c2.id"
            )
        # 2) 幂等补建唯一索引
        if database.db_type == 'sqlite':
            database.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uk_plugin_handler "
                "ON commands (plugin_name, handler)"
            )
        else:
            row = database.query_one(
                "SELECT COUNT(*) AS cnt FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'commands' "
                "AND index_name = 'uk_plugin_handler'"
            )
            if not (row or {}).get('cnt'):
                database.execute(
                    "ALTER TABLE commands ADD UNIQUE KEY uk_plugin_handler "
                    "(plugin_name, handler)"
                )
        logger.info("数据库迁移: commands 表补唯一索引 uk_plugin_handler")
    except Exception as e:
        logger.warning(f"数据库迁移 commands 唯一索引失败: {e}")


def _migrate_commands_require_perm(database):
    """迁移 commands 表添加 require_perm 列（权限节点要求，与 require_level 并存）"""
    try:
        if database.table_exists('commands') and \
           not database.table_has_column('commands', 'require_perm'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_perm TEXT DEFAULT ''"
                )
            else:
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_perm VARCHAR(255) DEFAULT '' "
                    "COMMENT '权限节点要求(节点式), 空=不限制'"
                )
            logger.info("数据库迁移: commands 表添加 require_perm 列")
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


def _migrate_dynamic_commands_table(database):
    """迁移 dynamic_commands 表：match_type ENUM 增加 contains（更开放的匹配方式）

    SQLite 无需迁移（ENUM 已翻译为 TEXT，无取值约束）；
    MySQL 旧库 ENUM 只有 exact/prefix/regex，需要 ALTER 加入 contains。
    """
    try:
        if not database.table_exists('dynamic_commands'):
            return
        if database.db_type != 'mysql':
            return
        row = database.query_one(
            "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'dynamic_commands' AND COLUMN_NAME = 'match_type'"
        )
        col_type = (row or {}).get('COLUMN_TYPE', '') or ''
        if 'contains' in col_type:
            return
        database.execute(
            "ALTER TABLE dynamic_commands MODIFY COLUMN match_type "
            "ENUM('exact','prefix','contains','regex') DEFAULT 'exact' "
            "COMMENT '匹配方式'"
        )
        logger.info("数据库迁移: dynamic_commands.match_type ENUM 增加 contains")
    except Exception:
        pass


def _migrate_perm_tables(database):
    """权限系统（perm_*）建表后处理：补索引 + 播种默认组

    表本体由 _auto_create_tables 创建，这里补两件事：
    1. 索引：SQLite 不支持 CREATE TABLE 内联 INDEX，只能单独建；
       MySQL 由 sql/init.sql 建（重复执行会报错，故静默忽略）
    2. 默认组 default：全员自动拥有，weight 最低。用先查后插保证幂等
       （INSERT IGNORE 在两库语义不同，不采用）
    """
    index_ddls = (
        "CREATE INDEX IF NOT EXISTS idx_pun_user ON perm_user_nodes (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_pun_node ON perm_user_nodes (node)",
        "CREATE INDEX IF NOT EXISTS idx_pgn_group ON perm_group_nodes (group_name)",
        "CREATE INDEX IF NOT EXISTS idx_pgn_node ON perm_group_nodes (node)",
        "CREATE INDEX IF NOT EXISTS idx_pg_weight ON perm_groups (weight)",
        "CREATE INDEX IF NOT EXISTS idx_pa_target ON perm_audit (target_type, target)",
        "CREATE INDEX IF NOT EXISTS idx_pa_created ON perm_audit (created_at)",
    )
    for ddl in index_ddls:
        try:
            database.execute(ddl)
        except Exception:
            pass

    try:
        if not database.table_exists('perm_groups'):
            return
        row = database.query_one(
            "SELECT name FROM perm_groups WHERE name = %s", ('default',))
        if not row:
            database.execute(
                "INSERT INTO perm_groups "
                "(name, display_name, weight, prefix, suffix, is_default, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ('default', '默认组', 0, None, None, 1, str(int(time.time())))
            )
            logger.info("数据库迁移: 播种默认权限组 default")
    except Exception:
        pass


def _migrate_dynamic_commands_handler(database):
    """迁移 dynamic_commands 表：增加 handler 列（关键词 handler 回调 plugin:func）"""
    try:
        if not database.table_exists('dynamic_commands'):
            return
        if database.table_has_column('dynamic_commands', 'handler'):
            return
        if database.db_type == 'sqlite':
            database.execute(
                "ALTER TABLE dynamic_commands ADD COLUMN handler TEXT DEFAULT ''")
        else:
            database.execute(
                "ALTER TABLE dynamic_commands ADD COLUMN handler VARCHAR(100) DEFAULT '' "
                "COMMENT '关键词handler回调 plugin:func'")
        logger.info("数据库迁移: dynamic_commands 表添加 handler 列")
    except Exception:
        pass
