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
- 也可独立运行: python -m framework.database.init_db
"""
import logging
import os
import re

logger = logging.getLogger('zcbot')

# 框架核心表清单（严格等于 sql/init.sql 会建出的表）。
#
# 边界必须与 init.sql 对齐：这张清单是"缺表自愈"的检测范围，若混入
# init.sql 不负责建的表（如 group_plugin_settings 由 schema.py 建），
# 初始化会每次都检测到"缺表"、建不出来、复验仍缺，永远报"未完成"。
_REQUIRED_TABLES = [
    'users', 'groups_info', 'group_members', 'plugins', 'commands',
    'dynamic_commands', 'tasks', 'admin_users', 'audit_logs',
    'plugin_configs', 'system_config', 'ip_blacklist',
    'perm_groups', 'perm_group_nodes', 'perm_user_nodes', 'perm_tracks',
    'perm_audit', 'api_tokens',
]

# 由 schema.py（_auto_create_tables）负责建的扩展表：不在 init.sql 内，
# 因此不参与 init 阶段的缺失检测，但启动后整体自检要一并核对。
_EXTENSION_TABLES = ['group_plugin_settings', 'drive']


def _all_table_names(database, targets) -> set:
    """一次性取回当前库全部表名（批量），兼容只有逐表接口的存储后端。

    ``Database.all_table_names()`` 用一次 SELECT 拿全表名，把启动期「缺表
    自检」的 N 次串行往返压缩成 1 次；SqlSimEngine（debug 模式）/ 测试桩等
    没有批量接口时回退为逐表 ``table_exists``，语义不变（仅往返数不同）。
    """
    batch = getattr(database, 'all_table_names', None)
    if callable(batch):
        return {str(t).lower() for t in batch()}
    return {t for t in targets if database.table_exists(t)}


def missing_required_tables(database, include_extension: bool = False) -> list:
    """返回核心表中当前缺失的表名（按 _REQUIRED_TABLES 顺序）。

    必须有这个自检：历史实现只用 admin_users + plugins 两张表当"是否已初始化"的探针，
    一旦运行中出现部分表丢失（手工 DROP、磁盘回滚、迁移中断），探针仍是"已初始化"
    → 整个初始化被跳过，缺失的表永远不会重建，直到相关功能写库时报错才暴露。

    include_extension=True 时把 schema.py 负责的扩展表也纳入核对（用于启动后
    整体自检；init 阶段的建表只能覆盖 init.sql 内的表，故默认不纳入）。

    检查本身失败（DB 不可达/权限异常）不视作"表缺失"，向上抛出交由调用方按
    "无法确认表结构完整性"处理——否则连接故障会被误判成需要建表。

    表存在性改为一次批量探测 + 内存过滤：检测结论与逐表 table_exists 完全一致，
    但把 18 张表的 18 次串行往返降到 1 次。
    """
    # 【踩坑保佑】v1.8.9 _REQUIRED_TABLES 从未被使用
    # 坑：探针只看两张表，运行中丢表永不重建  →  missing_required_tables
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    targets = list(_REQUIRED_TABLES)
    if include_extension:
        targets += [t for t in _EXTENSION_TABLES if t not in targets]
    try:
        existing = _all_table_names(database, targets)
    except Exception as e:
        raise RuntimeError(f"批量检查表结构完整性时失败: {e}") from e
    return [t for t in targets if t not in existing]


def _ddl_table_name(stmt: str):
    """从 CREATE TABLE 语句提取表名（小写）；非建表语句返回 None。"""
    m = re.match(
        r'\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?([A-Za-z0-9_]+)',
        stmt, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _dml_target_table(stmt: str):
    """从 INSERT/REPLACE 语句提取目标表名（小写）；其它语句返回 None。"""
    m = re.match(
        r'\s*(?:INSERT|REPLACE)\s+(?:IGNORE\s+)?(?:OR\s+\w+\s+)?INTO\s+[`"\[]?([A-Za-z0-9_]+)',
        stmt, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _apply_init_sql(database, transform=None, label="建表", exec_fn=None,
                    sql_filename="init.sql") -> dict:
    """按「只补缺失表」的方式应用初始化 SQL，返回统计而不是靠异常抛出。

    为什么不能无脑全量重跑：init.sql 除 18 条 CREATE TABLE 外还有 3 条种子 INSERT
    （默认权限组 / 默认管理员 / 默认系统配置）。其中 admin_users、system_config 的
    INSERT 没有去重语义，全量重跑会插出重复行。

    规则：
      - 表已存在 → 整条 CREATE 跳过（不执行任何副作用）；
      - 表缺失   → 执行 CREATE，并把它记入本次新建集合；
      - 种子 INSERT → 仅当目标表是本次新建时才执行（表刚建出来必定为空，插一次即正确）。

    失败不再吞成 debug：收集 (表名, 错误摘要) 返回，由调用方按"是否影响可用性"告警。

    exec_fn：实际执行器（MySQL 分支需用已 USE 到目标库的专用 cursor）。
    """
    # 【踩坑保佑】v1.8.9 建表假成功
    # 坑：逐表except压成debug，建0张表也报完成  →  _apply_init_sql
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    run = exec_fn or database.execute
    sql_file = _find_sql_file(sql_filename)
    if not sql_file:
        return {"created": [], "skipped": 0,
                "failed": [("*", f"未找到 sql/{sql_filename}")],
                "no_file": True}

    with open(sql_file, 'r', encoding='utf-8') as f:
        sql_content = f.read()

    created, skipped, failed = [], 0, []
    # 一次性取回现有表名：逐条 CREATE 前的 table_exists 改为内存判断，建表
    # 成功后增量补入，避免「check → CREATE → check」交替触发 N 次串行往返。
    # 无批量接口的后端（debug 模拟 / 测试桩）仍走原来的逐表检查。
    batch = getattr(database, 'all_table_names', None)
    existing = ({str(t).lower() for t in batch()}
                if callable(batch) else None)
    for raw in _split_sql_statements(sql_content):
        stmt = _strip_leading_comments(raw)
        if not stmt:
            continue
        upper = stmt.upper()
        if upper.startswith('CREATE DATABASE') or upper.startswith('USE '):
            continue

        table = _ddl_table_name(stmt)
        if table is not None:
            if existing is not None:
                if table in existing:
                    skipped += 1
                    continue
            else:
                try:
                    if database.table_exists(table):
                        skipped += 1
                        continue
                except Exception as e:
                    logger.debug('[_apply_init_sql] 捕获异常后按降级路径继续: %s', e)
                    failed.append((table, f"存在性检查失败: {e}"))
                    continue
            sql = transform(stmt) if transform else stmt
            try:
                run(sql)
                created.append(table)
                if existing is not None:
                    existing.add(table)
            except Exception as e:
                logger.debug('[_apply_init_sql] 捕获异常后按降级路径继续: %s', e)
                failed.append((table, str(e)[:160]))
            continue

        # 种子数据：仅补「本次新建的表」，避免重复插入历史数据
        target = _dml_target_table(stmt)
        if target is not None and target not in created:
            continue
        sql = transform(stmt) if transform else stmt
        try:
            run(sql)
        except Exception as e:
            logger.debug('[_apply_init_sql] 捕获异常后按降级路径继续: %s', e)
            failed.append((target or "seed", str(e)[:160]))

    # 结果化日志：0 张表建出来必须一眼可见，而不是埋在 info 的"完成"里
    if failed:
        logger.warning(
            "%s：新建 %d 张、跳过 %d 张、失败 %d 条（首条失败：%s → %s）",
            label, len(created), skipped, len(failed), failed[0][0], failed[0][1])
    elif created:
        logger.info("%s：新建 %d 张表（跳过已存在 %d 张）", label, len(created), skipped)
    else:
        logger.debug("%s：无需新建（%d 张表均已存在）", label, skipped)
    return {"created": created, "skipped": skipped, "failed": failed, "no_file": False}


def auto_init_database(database) -> bool:
    """自动检测并初始化数据库：缺哪张表补哪张，建完复验。

    返回 True 表示执行了初始化动作，False 表示核心表本就齐全（无需动作）。

    与历史实现的关键差别：
      1. 用 _REQUIRED_TABLES 全量检测缺失，而不是只看 admin_users + plugins；
      2. 只补缺失表（幂等，不重复执行种子 INSERT）；
      3. 建完**再校验一遍**，仍有缺失就报 error 并列出清单——不再无条件打
         "数据库自动初始化完成"。历史上建 0 张表也报"完成"，使首次部署的
         建表失败（权限/方言/磁盘）完全不可观测。
    """
    try:
        missing = missing_required_tables(database)
    except Exception as e:
        # 自检本身跑不起来（schema 损坏 malformed、连接断开、权限异常）说明
        # 数据库已无法安全使用。此时**必须向上抛**，让 init_db 走明确的降级分支：
        # 若在这里只打一条 error 然后 return False，后续 18 条建表语句会各自失败
        # 一次，把真正的根因埋进几十行 WARN 里（实测就是这种表现）。
        raise RuntimeError(f"数据库结构自检失败: {e}") from e

    if not missing:
        logger.debug("核心表齐全（%d 张），跳过自动初始化", len(_REQUIRED_TABLES))
        # 表齐不等于索引齐：早期版本在 SQLite 上把内联 INDEX 整行删掉了，
        # 存量库至今没有任何二级索引。这里幂等补建（已有则空操作）。
        if getattr(database, 'db_type', '') == 'sqlite':
            try:
                _ensure_sqlite_indexes(database)
            except Exception as e:
                logger.debug("SQLite 索引补建异常（不影响启动）: %s", e)
        return False

    logger.info("检测到 %d 张核心表缺失（%s），开始自动初始化...",
                len(missing), ", ".join(missing))

    try:
        if database.db_type == 'mysql':
            stats = _init_mysql(database)
        elif database.db_type == 'debug':
            stats = _init_debug(database)
        else:
            stats = _init_sqlite(database)
    except Exception as e:
        logger.error("数据库自动初始化异常中断: %s —— 下方仍会复验缺失表", e)
        stats = {"created": [], "skipped": 0, "failed": [("*", str(e)[:160])]}

    # MySQL 分支用原生 cursor 建表（不走 Database.execute），DDL 不会触发
    # 元数据快照的增量维护；且 _apply_init_sql 用本地表名集合判断，也需要把
    # 新建表同步回快照。这里统一重拉一次（一次批量往返），保证后续迁移与
    # 复验看到的是最新结构，而不是建表前的旧快照。
    refresh = getattr(database, 'refresh_schema_snapshot', None)
    if callable(refresh):
        try:
            refresh()
        except Exception as e:
            logger.debug("刷新表结构元数据快照失败（不影响初始化结果）: %s", e)

    # 复验：初始化"跑过"不等于"建成了"
    try:
        still_missing = missing_required_tables(database)
    except Exception as e:
        logger.error("数据库初始化后复验失败（%s）：无法确认表是否补齐", e)
        return True

    if still_missing:
        logger.error(
            "数据库初始化未完成：仍缺失 %d 张表 %s（本次新建 %d 张，失败 %d 条）。"
            "相关功能写库会失败，请检查数据库用户建表权限、磁盘空间与 DDL 方言兼容性。",
            len(still_missing), ", ".join(still_missing),
            len(stats.get("created", [])), len(stats.get("failed", [])))
        return True

    logger.info(
        "数据库自动初始化完成：本次新建 %d 张表（%s），核心表 %d 张已全部就绪",
        len(stats.get("created", [])),
        ", ".join(stats.get("created", [])) or "无",
        len(_REQUIRED_TABLES))
    return True


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


def _init_mysql(database) -> dict:
    """MySQL 模式自动建表：只补缺失表，返回 {created, skipped, failed}。

    连接分两步：先不指定库连上去 CREATE DATABASE，再 USE 目标库做表级初始化。
    表级语句交给 _apply_init_sql 统一按「缺失才建 + 种子只随新建表插入」处理。
    """
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
        try:
            # 创建数据库
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"DEFAULT CHARACTER SET {charset} COLLATE {collation}"
            )
            cursor.execute(f"USE `{db_name}`")
            # MySQL 5.5/5.6 一张表只允许一个 TIMESTAMP 自动初始化列；init.sql
            # 每表最多 3 个 DATETIME DEFAULT CURRENT_TIMESTAMP，经过
            # _adapt_ddl_for_mysql 改写后必然报 Incorrect table definition。
            # 专为 5.5 写的 init_mysql55.sql 用 DATETIME DEFAULT NULL 回避了该限制，
            # 但此前从未被加载过（死文件）。这里真正接入它，且不再做 DDL 改写。
            old_mysql = ver['major'] == 5 and ver['minor'] <= 6
            init_file = 'init_mysql55.sql' if old_mysql else 'init.sql'
            if old_mysql:
                logger.info(
                    "MySQL 5.5/5.6 检测到，改用 sql/init_mysql55.sql 建表"
                    "（避免多 TIMESTAMP 自动列导致的建表失败）")

            from framework.database.dialect import (
                MYSQL_KEY_LIMIT_MODERN,
                MYSQL_KEY_LIMIT_OLD,
                _mysql_prefix_indexes,
            )
            key_limit = MYSQL_KEY_LIMIT_OLD if old_mysql else MYSQL_KEY_LIMIT_MODERN

            def _transform_ddl(stmt):
                """建表前的 DDL 改写。

                两件事必须分开看：
                  1. DATETIME → TIMESTAMP 只对 5.5/5.6 **之前**的 init.sql 有意义；
                     init_mysql55.sql 已用 DATETIME DEFAULT NULL 自行规避，不能再改。
                  2. 索引前缀改写**两个分支都要做**，5.5/5.6 的 767 字节上限更严。
                历史 bug：这里只传了 _adapt_ddl_for_mysql，而 _apply_init_sql 用
                exec_fn=cursor.execute 裸执行、不经过 Database.execute 的方言翻译，
                于是 _mysql_prefix_indexes 在建表路径上从未生效 ——
                init.sql / init_mysql55.sql 里的 dynamic_commands、audit_logs、
                api_tokens 在 5.5/5.6 上直接报 1071 建不出来。
                """
                if not old_mysql:
                    stmt = _adapt_ddl_for_mysql(stmt, ver)
                return _mysql_prefix_indexes(stmt, max_key_bytes=key_limit)

            return _apply_init_sql(
                database,
                transform=_transform_ddl,
                label="MySQL 建表",
                exec_fn=cursor.execute,
                sql_filename=init_file,
            )
        finally:
            cursor.close()
    finally:
        conn.close()


# ── SQLite 初始化 ─────────────────────────────────────────────

def _ensure_sqlite_indexes(database, sql_filename="init.sql") -> dict:
    """为 SQLite 补建 init.sql 里声明的**普通索引**（幂等，可反复执行）。

    背景：SQLite 的 CREATE TABLE 不支持内联 `INDEX idx (col)`，方言层只能把这些
    声明删掉。历史上删完就没了，导致 SQLite 部署**一个二级索引都不存在**，
    所有按非唯一列过滤的查询都是全表扫描。

    这条路必须独立于「只补缺失表」：`_apply_init_sql` 在表已存在时会直接跳过，
    存量部署永远走不到补建逻辑。所以这里用 CREATE INDEX IF NOT EXISTS 无条件跑，
    已有的索引是空操作，缺的补上；大表首次补建会有一次性耗时，属必要成本。
    """
    from framework.database.dialect import build_sqlite_index_ddl, extract_plain_indexes

    sql_file = _find_sql_file(sql_filename)
    if not sql_file:
        return {"created": 0, "failed": []}

    with open(sql_file, 'r', encoding='utf-8') as f:
        sql_content = f.read()

    indexes = extract_plain_indexes(sql_content)
    if not indexes:
        return {"created": 0, "failed": []}

    try:
        existing_tables = {str(t).lower() for t in database.all_table_names()}
    except Exception as _exc:
        logger.debug('[_ensure_sqlite_indexes] 捕获异常后按降级路径继续: %s', _exc)
        existing_tables = set()

    created, failed = 0, []
    for ddl in build_sqlite_index_ddl(indexes):
        # 表不存在就别建（CREATE INDEX 会报 no such table）
        m = re.search(r'ON\s+"([^"]+)"', ddl)
        if m and m.group(1).lower() not in existing_tables:
            continue
        try:
            database.execute(ddl)
            created += 1
        except Exception as e:
            logger.debug('[_ensure_sqlite_indexes] 捕获异常后按降级路径继续: %s', e)
            failed.append((ddl[:60], str(e)[:100]))

    if failed:
        logger.warning("SQLite 索引补建：失败 %d 个（首个：%s → %s）",
                       len(failed), failed[0][0], failed[0][1])
    else:
        logger.debug("SQLite 索引补建：%d 个索引已就绪", created)
    return {"created": created, "failed": failed}


def _init_sqlite(database) -> dict:
    """SQLite 模式自动建表（复用 db.py 的 SQL 翻译）：只补缺失表。"""
    from framework.database.db import _translate_sql_for_sqlite

    stats = _apply_init_sql(
        database, transform=_translate_sql_for_sqlite, label="SQLite 建表")
    # 建表后补建普通索引（见 _ensure_sqlite_indexes 的说明）
    try:
        idx = _ensure_sqlite_indexes(database)
        stats = dict(stats)
        stats['indexes'] = idx.get('created', 0)
    except Exception as e:
        logger.debug("SQLite 索引补建异常（不影响建表结果）: %s", e)
    return stats


def _init_debug(database) -> dict:
    """调试模式（SqlSimEngine）建表：执行原生 init.sql 的 DDL（MySQL 方言）。

    SqlSimEngine 本就是 MySQL 方言模拟器，无需 SQLite 翻译，喂原生 CREATE 即可，
    并据其中的 UNIQUE KEY 记录冲突键，使 INSERT..ON DUPLICATE KEY UPDATE 真正去重
    （否则表由 INSERT 惰性建出、无唯一键，重复同步会累积脏行）。

    种子数据同样只在「对应表本次新建」时插入，避免每次启动重复写管理员/系统配置。
    """
    return _apply_init_sql(database, label="调试模式建表")


# ── 工具函数 ──────────────────────────────────────────────────

def _strip_leading_comments(stmt: str) -> str:
    """剥离语句前导的注释行与空行，返回第一条有效 SQL 行起的内容"""
    lines = stmt.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('--'):
            start = i + 1
        else:
            break
    return '\n'.join(lines[start:]).strip()


def _find_sql_file(filename: str) -> str:
    """查找 SQL 文件（项目根 ./sql/<file> 优先，并向上回溯若干级以防 framework 被 vendored 到不同层级）。

    framework 本体与下游项目（framework/ 作为子目录）的 sql/ 均在项目根目录，
    故需从 framework/database 上溯到 framework 的父目录（即项目根）。
    """
    # 【踩坑保佑】v1.5.0 init_db SQL 路径查找
    # 坑：_find_sql_file只上溯到framework，全链路缺表  →  _find_sql_file
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    here = os.path.dirname(os.path.abspath(__file__))
    search_dirs = [here]
    cur = here
    for _ in range(4):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
        search_dirs.append(cur)
    for d in search_dirs:
        for cand in (os.path.join(d, 'sql', filename), os.path.join(d, filename)):
            if os.path.isfile(cand):
                return cand
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
    """独立运行: python -m framework.database.init_db [config.yaml]"""
    import sys

    from framework.config import load_config
    from framework.database.db import _parse_sqlite_type, init_db

    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)

    print(f"数据库配置: {config.get('database', {})}")
    db_config = _parse_sqlite_type(config.get('database', {}))

    print(f"正在初始化 {db_config.get('type', 'sqlite')} 数据库...")
    db = init_db(config.get('database', {}))

    # 强制重新检查并初始化
    result = auto_init_database(db)
    missing = missing_required_tables(db)
    if missing:
        print(f"数据库初始化未完成，仍缺失 {len(missing)} 张表: {', '.join(missing)}")
        db.close()
        # 独立运行时把失败变成非零退出码，便于部署脚本/CI 直接判定
        sys.exit(1)
    if result:
        print("数据库初始化成功")
    else:
        print("数据库表已存在，无需初始化")

    db.close()


if __name__ == '__main__':
    _main()
