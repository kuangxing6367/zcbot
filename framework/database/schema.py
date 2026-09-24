"""
框架 schema：自动建表 + 运行时迁移（SQLite / MySQL 双方言）

只管「表结构」：框架扩展表的 CREATE TABLE 与历史版本的列/索引补丁。
连接与执行在 db.Database；SQL 方言翻译在 dialect.py。
"""
import logging
import time

logger = logging.getLogger('zcbot')


def _auto_create_tables(database):
    """自动创建框架所需的扩展表"""
    # 列类型统一用 VARCHAR（SQLite 宽松类型同样兼容）：
    # - TEXT 列不能作为 MySQL 索引键（缺 key length）
    # - TEXT 列不能带 DEFAULT（MySQL 报错）
    tables = {
        'group_plugin_settings': """
            CREATE TABLE IF NOT EXISTS group_plugin_settings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    INTEGER NOT NULL,
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

        # ── 权限系统（LuckPerms 风格）─────────────────────────────
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
                user_id     BIGINT       NOT NULL,
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
                    "COMMENT '权限节点要求(LuckPerms风格), 空=不限制'"
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
