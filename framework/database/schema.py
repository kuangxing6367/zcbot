"""
框架 schema：自动建表 + 版本化迁移（SQLite / MySQL 双方言）

只管「表结构」：框架扩展表的 CREATE TABLE 与历史版本的列/索引补丁。
迁移的账本与调度在 migrations.py；连接与执行在 db.Database；SQL 方言翻译在 dialect.py。

新增字段的正确做法（三处同步，缺一不可）：
  1. 新装的库：改 sql/init.sql + sql/init_mysql55.sql 的建表语句；
  2. 已在跑的库：在下方 MIGRATIONS 末尾追加一版迁移（next_version()）；
  3. 迁移必须幂等（先查列/索引是否存在再改），中断可重入；
  4. 必须声明 level：additive（加表加列，可开机自跑）/ rewrite（重建表、
     改类型，会锁表）/ destructive（删表删列删数据，必须人工授权后执行）。
  已发布的迁移一律不可改 —— 账本里有校验和，改动会在启动时报错。
"""
import logging
import time

from framework.database.migrations import (
    LEVEL_ADDITIVE,
    LEVEL_DESTRUCTIVE,
    LEVEL_REWRITE,
    Migration,
    MigrationContext,
)
from framework.database.migrations import run_pending as run_migrations

logger = logging.getLogger('zcbot')


def migrations_context(database, source='boot', **overrides) -> MigrationContext:
    """按 ``database.migrations`` 配置段构造执行上下文。

    配置项（都可不写，默认即安全值）::

        database:
          migrations:
            auto_apply_destructive: false   # destructive 是否允许开机自动执行
            backup_before_rewrite: true     # rewrite/destructive 前自动备份
            keep_backups: 5                 # SQLite 备份保留份数
            operator: ""                    # 记录到流水的操作人

    启动场景恒为 source='boot' 且默认不允许 destructive —— 开机流程不能
    兼任「批准人」，这是职责分离的边界。
    """
    cfg = getattr(database, 'config', None)
    if not isinstance(cfg, dict):
        cfg = {}
    return MigrationContext.from_config(source, cfg, **overrides)

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
                # 不吞：账本要记 success=0，下次启动重试（幂等保证可重入）
                logger.warning(f"数据库迁移 {table}.{col} 类型约束失败: {e}")
                raise


def _auto_create_tables(database, context=None):
    """创建框架扩展表并应用版本化迁移。

    ``context`` 为 None 时按 ``database.migrations`` 配置构造 boot 上下文
    （开机流程，destructive 默认被拦）；CLI/接口显式传入自己的上下文，
    以免「带变更单号执行」被开机策略覆盖。

    返回迁移执行报告（无待应用时也返回，供 CLI/测试读取）；引擎自身异常时
    返回 None —— 迁移失败不抛出，避免被 db.init_db 的兜底判成「数据库不可用」
    而降级到本地模拟引擎。
    """
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
        # token 存的是 'sha256:'+64hex 摘要（v11 起），**不是明文**；明文只在创建时返回一次
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

        # ── 多平台接入：适配器注册表（drive）─────────────────────
        # 每个适配器（按连接标识 adapter_key，如 onebot / qq_official / telegram）
        # 首次加载时自动登记一个稳定 uuid；重启复用，不漂移。
        # user/group 命名空间隔离（multi_platform.namespace）即基于此 uuid 把身份键改写为
        # `<uuid>@<id>`（用户概念中的 user-<uuid> / group-<uuid>）。
        'drive': """
            CREATE TABLE IF NOT EXISTS drive (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                adapter_key VARCHAR(64)  NOT NULL,
                uuid        VARCHAR(64)  NOT NULL,
                kind        VARCHAR(64)  DEFAULT '',
                self_id     VARCHAR(64)  DEFAULT '',
                created_at  VARCHAR(32)  DEFAULT NULL,
                last_seen   VARCHAR(32)  DEFAULT NULL,
                enabled     INTEGER      DEFAULT 1,
                UNIQUE(adapter_key)
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

    # 版本化迁移：账本 schema_migrations 记录已应用版本，每版只执行一次。
    # 迁移失败不阻塞启动（可用性优先），但必须可见：error 日志 + 账本 success=0
    # + 下次启动自动重试。
    #
    # 启动不是「批准人」：destructive 迁移（删表/删列/删数据）默认被授权闸门
    # 拦下，必须由人在命令行带变更单号执行。这是 SOX 职责分离的落地点 ——
    # 自动化的启动流程不能同时充当授权者。
    report = None
    try:
        ctx = context or migrations_context(database, source='boot')
        report = run_migrations(database, MIGRATIONS, ctx)
    except Exception as e:  # noqa: BLE001  注册表非法等：引擎自身故障
        logger.error("数据库迁移引擎异常，已跳过迁移: %s", e)
    else:
        if report.get('blocked'):
            b = report['blocked'][0]
            logger.error(
                "数据库迁移待授权：v%s %s（%s）未执行 —— %s。"
                "结构落后于代码，授权后执行 CLI 或重启生效。",
                b.get('version'), b.get('name'), b.get('level'), b.get('reason'))
        elif not report['ok']:
            first = (report['failed'] or [{}])[0]
            logger.error(
                "数据库迁移未全部完成：当前版本 v%s（最新 v%s），失败 %d 条"
                "（首个：v%s %s → %s）。结构可能落后于代码，请修复后重启。",
                report['current'], report['latest'], len(report['failed']),
                first.get('version'), first.get('name'), first.get('error'))
        elif report['applied']:
            logger.info(
                "数据库迁移完成：应用 %d 版（v%s → v%s），耗时 %dms",
                len(report['applied']),
                report['applied'][0]['version'], report['current'],
                report['elapsed_ms'])

    # 收尾自检：扩展表同样是"缺了要能重建"，但建表失败不能只留一行 debug。
    # init.sql 范围之外的表（如 group_plugin_settings）只在这里建，故必须复验。
    try:
        from framework.database.init_db import missing_required_tables
        missing = missing_required_tables(database, include_extension=True)
    except Exception as e:
        logger.warning(f"扩展表结构自检失败（不影响启动）: {e}")
    else:
        if missing:
            logger.error(
                "扩展表结构不完整：仍缺失 %d 张表 %s。相关功能写库会失败，"
                "请检查数据库建表权限与 DDL 方言兼容性。",
                len(missing), ", ".join(missing))
    return report


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
    except Exception as e:
        logger.warning("数据库迁移 commands.require_level 失败: %s", e)
        raise


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
    # IF NOT EXISTS：重建流程失败重入时（上一次异常后 __idtype_new 可能残留），
    # 无 IF NOT EXISTS 会直接报 "table already exists" 掩盖真实原因。
    new_ddl = f"CREATE TABLE IF NOT EXISTS {new} (\n" + ",\n".join(lines) + "\n)"

    try:
        # 先清理上一次重建失败可能残留的 __idtype_new：不清就直接 CREATE 会因
        # 表已存在而失败；若残留表结构恰好兼容，还会把上一次的脏行留在新表里。
        database.execute(f"DROP TABLE IF EXISTS {new}")
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
        raise


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
    except Exception as e:
        logger.warning("数据库迁移 commands.require_perm 失败: %s", e)
        raise


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
    except Exception as e:
        logger.warning("数据库迁移 users.role 失败: %s", e)
        raise


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
    except Exception as e:
        logger.warning("数据库迁移 admin_users.token 失败: %s", e)
        raise


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
    except Exception as e:
        logger.warning("数据库迁移 dynamic_commands.match_type 失败: %s", e)
        raise


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
        except Exception as e:  # noqa: BLE001
            # MySQL 无 CREATE INDEX IF NOT EXISTS：重复建索引报 Duplicate key name，
            # 属预期，不影响迁移结果（索引已存在）。
            logger.debug("补索引跳过（可能已存在）: %s", e)

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
    except Exception as e:
        logger.warning("数据库迁移 播种默认权限组失败（全员默认权限会缺失）: %s", e)
        raise


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
    except Exception as e:
        logger.warning("数据库迁移 dynamic_commands.handler 失败: %s", e)
        raise


def _migrate_admin_users_token_hash(database):
    """v10：admin_users 增加 token_hash 列，并清空历史明文 token（安全加固）。

    此前登录把 2048 字符明文 token 写进 admin_users.token，_verify_token 又用
    WHERE token = %s 明文查库 —— DB 泄露即可直接冒用管理员会话。本版把会话
    令牌改造为「明文只返回客户端、库里只存 sha256 哈希」：

      1. 加可空列 token_hash（定长 64 hex，适合建索引与常量时间比对）；
      2. 补 token_hash 索引（2048 字符明文列无法高效索引，哈希列可以）；
      3. 清空 token 列残留的明文，**不回填哈希** —— 存量会话一律作废，
         升级后所有管理员必须重新登录（明确取舍，不静默保留可用明文会话）。

    不删 token 列：expand-contract 的「先扩展」阶段保留旧列（值恒为 NULL），
    删列属 destructive、需人工授权，留待后续 contract 版。

    全部 additive：不删行、不删列、不改类型、不重建表；加列 / 加索引 / 清值
    分别做幂等判断，中断重入可收敛。
    """
    if not database.table_exists('admin_users'):
        return

    # 1) 加 token_hash 列
    if not database.table_has_column('admin_users', 'token_hash'):
        if database.db_type == 'sqlite':
            database.execute(
                "ALTER TABLE admin_users ADD COLUMN token_hash VARCHAR(64) DEFAULT NULL")
        else:
            database.execute(
                "ALTER TABLE admin_users ADD COLUMN token_hash VARCHAR(64) DEFAULT NULL "
                "COMMENT '会话令牌sha256哈希(64 hex), 明文不落库'")
        logger.info("数据库迁移: admin_users 表添加 token_hash 列")

    # 2) 补 token_hash 索引（定长 64，等值查询可走索引）
    if database.db_type == 'sqlite':
        database.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_token_hash "
            "ON admin_users (token_hash)")
    else:
        row = database.query_one(
            "SELECT COUNT(*) AS cnt FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'admin_users' "
            "AND index_name = 'idx_admin_token_hash'")
        if not (row or {}).get('cnt'):
            database.execute(
                "ALTER TABLE admin_users ADD INDEX idx_admin_token_hash (token_hash)")

    # 3) 清空历史明文 token：不回填哈希，存量会话作废、强制重新登录
    if database.table_has_column('admin_users', 'token'):
        row = database.query_one(
            "SELECT COUNT(*) AS cnt FROM admin_users WHERE token IS NOT NULL")
        if (row or {}).get('cnt'):
            database.execute(
                "UPDATE admin_users SET token = NULL WHERE token IS NOT NULL")
            logger.warning(
                "数据库迁移: 已清空 %s 条历史明文会话 token（不回填哈希）—— "
                "存量会话全部作废，升级后需重新登录", row['cnt'])


def _migrate_api_tokens_hash(database):
    """v11：api_tokens.token 里的明文 API Key 原地换成带前缀的 sha256 摘要。

    此前创建接口令牌把 `secrets.token_hex(32)` 明文写进 api_tokens.token，
    `_verify_token` 又用 `WHERE token = %s` 直接拿客户端提交值等值查库 ——
    DB 泄露即可直接冒用 API Key（含 role=super 的令牌）。

    这里**不加列、不删列、不改索引**：token VARCHAR(512) NOT NULL UNIQUE
    本来就装得下 71 字符的 `'sha256:' + 64hex`，UNIQUE 顺带保证不同 Key 的
    摘要不会撞车。前缀让本迁移幂等（已迁移的行不会被二次哈希），也让校验
    侧能一眼区分摘要与历史明文。

    与会话 token 的 v10 取舍不同：v10 选择「清列、存量会话强制重新登录」；
    这里存量 API Key **不失效** —— 外部程序持有原始 token，服务端按同一算法
    算摘要比对即可，无需所有调用方轮换密钥。

    纯 DML + 逐行 UPDATE，中断重入可收敛；不改表结构，故为 additive。
    """
    if not database.table_exists('api_tokens'):
        return
    from framework.token_hash import API_KEY_HASH_PREFIX, hash_api_key

    try:
        rows = database.query(
            "SELECT id, token FROM api_tokens "
            "WHERE token IS NOT NULL AND token != ''") or []
    except Exception as e:
        logger.warning("数据库迁移: 读取 api_tokens 失败，跳过本版: %s", e)
        raise

    migrated = 0
    for r in rows:
        stored = str(r.get('token') or '')
        if stored.startswith(API_KEY_HASH_PREFIX):
            continue
        try:
            database.execute(
                "UPDATE api_tokens SET token = %s WHERE id = %s",
                (hash_api_key(stored), r['id']))
            migrated += 1
        except Exception as e:
            # 单条失败不吞：抛出让账本记 success=0，下次启动重跑收敛
            logger.error("数据库迁移: api_tokens id=%s 摘要化失败: %s", r['id'], e)
            raise
    if migrated:
        logger.info("数据库迁移: 已把 %s 条明文 API Key 收敛为 sha256 摘要"
                    "（存量 Key 仍可正常使用）", migrated)


# ── 迁移注册表 ────────────────────────────────────────────────
#
# 铁律：
#   1. 版本号一经发布**永不重排、永不改内容**（账本按版本号 + 校验和记账，
#      改动只会让启动时报 drift，老库也不会重跑）；
#   2. 新迁移一律追加在末尾，版本号 = next_version(MIGRATIONS)；
#   3. 迁移体必须幂等：先判断「要做的事是否已存在」再动手，
#      因为 MySQL 的 DDL 不是事务性的，中断后只能靠重入收敛；
#   4. 一次迁移只做一件事，便于失败后定位与重试。
#
# 历史 9 版由「每次启动无条件执行的幂等补丁」收敛而来：老库首次升级到
# 带账本的版本时会把它们各跑一遍（都是幂等判断，效果相当于空转），
# 然后记账；此后不再重复执行。
MIGRATIONS = (
    Migration(1, 'commands.require_level', _migrate_commands_table,
              'commands 表增加 require_level（命令权限等级）',
              level=LEVEL_ADDITIVE),
    Migration(2, 'commands.unique_index', _migrate_commands_unique_index,
              'commands 表补唯一索引 uk_plugin_handler 并清洗历史重复行',
              level=LEVEL_DESTRUCTIVE),  # 会 DELETE 重复行：不可逆，需人工授权
    Migration(3, 'identity.id_columns', _migrate_identity_id_columns,
              '身份 ID 列统一为 VARCHAR(64)（开放平台 openid 为字符串）',
              level=LEVEL_REWRITE),      # SQLite 走整表重建：锁表但保数据
    Migration(4, 'users.role', _migrate_users_table,
              'users 表增加 role（超管标记）',
              level=LEVEL_ADDITIVE),
    Migration(5, 'commands.require_perm', _migrate_commands_require_perm,
              'commands 表增加 require_perm（权限节点要求）',
              level=LEVEL_ADDITIVE),
    Migration(6, 'admin_users.token', _migrate_admin_users_table,
              'admin_users 表增加 token / token_created_at',
              level=LEVEL_ADDITIVE),
    Migration(7, 'dynamic_commands.match_type', _migrate_dynamic_commands_table,
              "dynamic_commands.match_type ENUM 增加 contains",
              level=LEVEL_ADDITIVE),
    Migration(8, 'dynamic_commands.handler', _migrate_dynamic_commands_handler,
              'dynamic_commands 表增加 handler（关键词回调）',
              level=LEVEL_ADDITIVE),
    Migration(9, 'perm.indexes_default_group', _migrate_perm_tables,
              '权限表补索引并播种默认组 default',
              level=LEVEL_ADDITIVE),
    Migration(10, 'admin_users.token_hash', _migrate_admin_users_token_hash,
              'admin_users 增加 token_hash 并清空历史明文 token（会话令牌改为哈希存储）',
              level=LEVEL_ADDITIVE),
    Migration(11, 'api_tokens.hash', _migrate_api_tokens_hash,
              'api_tokens 明文 API Key 原地收敛为 sha256 摘要（存量 Key 不失效）',
              level=LEVEL_ADDITIVE),
)
