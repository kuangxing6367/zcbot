# -*- coding: utf-8 -*-
"""
数据库迁移引擎：版本账本 + 变更分级 + 授权闸门 + 执行证据链 + 执行前备份

设计依据不是某个迁移工具，而是**变更管控的通行规范**：

- SOC 2 CC8.1（变更须经授权、测试、批准、可追溯）、
- SOX ITGC（DDL/DML 变更需有变更单、申请人与批准人分离、完整执行记录）、
- ITIL 4 变更使能（标准变更预授权 / 正常变更按风险分级授权 / 紧急变更事后复核）、
- 零停机 schema 演进（expand-contract、兼容中间态、可回退）。

落到实现上是四件事：

1. **变更分级**：每条迁移声明 ``level`` = additive / rewrite / destructive。
   分级不是装饰，它直接决定「能不能在启动时自动执行」。
2. **授权闸门**：destructive 默认不允许自动执行，必须由人在 CLI/接口上
   带变更单号显式批准；申请与执行因此天然分离（启动进程不是批准人）。
3. **证据链**：``schema_migration_runs`` 是 append-only 的执行流水（谁、
   何时、哪一版、什么级别、变更单号、原因、耗时、成功与否、备份文件），
   ``schema_migrations`` 只记「当前状态」。审计要的是前者，排障要的是后者。
4. **可回退**：rewrite / destructive 执行前自动备份（SQLite 用 VACUUM INTO
   在线快照），备份路径写进流水；MySQL 记录 binlog 位点作为恢复点。

与主流工具的关系：账本 / 版本单调 / 校验和 / 只前进 对齐 Flyway、Liquibase、
Alembic、Rails；分级授权与执行流水是它们要么放在商业版（Flyway Teams）、
要么外挂到变更平台的，这里内建在引擎里。

边界：
- 本模块只负责「账本 + 调度 + 证据」，不认识任何具体表结构。
- 建表语句（CREATE TABLE IF NOT EXISTS）不属于迁移，仍在 schema.py。

为什么不把账本表写进 sql/init.sql：
账本是**工具自己维护的元数据**（Flyway 的 flyway_schema_history、Rails 的
schema_migrations 同理），不属于业务 schema。写进 init.sql 会让
``missing_required_tables`` 把它当成核心表，一旦缺失就触发整轮「建表 +
种子数据」流程，语义错位。
"""
import hashlib
import inspect
import logging
import os
import shutil
import textwrap
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger('zcbot')

LEDGER_TABLE = 'schema_migrations'
RUNS_TABLE = 'schema_migration_runs'

# 账本里 origin 的取值：
#   run      —— 引擎实际执行过
#   baseline —— 运维手工认定「这个库的 schema 已经等于该版本」（不执行，只记账）
ORIGIN_RUN = 'run'
ORIGIN_BASELINE = 'baseline'

# 变更级别（对应 ITIL 的风险分级，而不是变更类型本身）：
#   additive    —— 加表/加列/加索引/放宽约束：旧代码仍能跑，可自动执行
#   rewrite     —— 重建表/改类型/批量回填：不丢数据，但会锁表或产生长事务
#   destructive —— 删表/删列/删行/收紧语义：不可逆，必须人工授权
LEVEL_ADDITIVE = 'additive'
LEVEL_REWRITE = 'rewrite'
LEVEL_DESTRUCTIVE = 'destructive'
LEVELS = (LEVEL_ADDITIVE, LEVEL_REWRITE, LEVEL_DESTRUCTIVE)
_LEVEL_RANK = {LEVEL_ADDITIVE: 0, LEVEL_REWRITE: 1, LEVEL_DESTRUCTIVE: 2}

_LEDGER_SQLITE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    checksum    VARCHAR(64)  DEFAULT '',
    applied_at  VARCHAR(32)  DEFAULT NULL,
    duration_ms INTEGER      DEFAULT 0,
    success     INTEGER      DEFAULT 1,
    origin      VARCHAR(16)  DEFAULT 'run',
    note        VARCHAR(255) DEFAULT '',
    level       VARCHAR(16)  DEFAULT 'additive',
    operator    VARCHAR(64)  DEFAULT '',
    ticket      VARCHAR(64)  DEFAULT '',
    reason      VARCHAR(255) DEFAULT ''
)
"""

# 不用 AUTOINCREMENT：版本号由注册表给定，主键本身就要显式写入
_LEDGER_MYSQL = """
CREATE TABLE IF NOT EXISTS `schema_migrations` (
    `version`     INT          NOT NULL PRIMARY KEY,
    `name`        VARCHAR(128) NOT NULL,
    `checksum`    VARCHAR(64)  DEFAULT '',
    `applied_at`  VARCHAR(32)  DEFAULT NULL,
    `duration_ms` INT          DEFAULT 0,
    `success`     TINYINT      DEFAULT 1,
    `origin`      VARCHAR(16)  DEFAULT 'run',
    `note`        VARCHAR(255) DEFAULT '',
    `level`       VARCHAR(16)  DEFAULT 'additive',
    `operator`    VARCHAR(64)  DEFAULT '',
    `ticket`      VARCHAR(64)  DEFAULT '',
    `reason`      VARCHAR(255) DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 执行流水：append-only，只增不改。审计取样查这张表。
_RUNS_SQLITE = """
CREATE TABLE IF NOT EXISTS schema_migration_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER      DEFAULT 0,
    name        VARCHAR(128) DEFAULT '',
    level       VARCHAR(16)  DEFAULT 'additive',
    started_at  VARCHAR(32)  DEFAULT NULL,
    duration_ms INTEGER      DEFAULT 0,
    success     INTEGER      DEFAULT 0,
    source      VARCHAR(16)  DEFAULT '',
    operator    VARCHAR(64)  DEFAULT '',
    ticket      VARCHAR(64)  DEFAULT '',
    reason      VARCHAR(255) DEFAULT '',
    error       VARCHAR(500) DEFAULT '',
    backup      VARCHAR(255) DEFAULT '',
    db_type     VARCHAR(16)  DEFAULT '',
    app_version VARCHAR(32)  DEFAULT ''
)
"""

_RUNS_MYSQL = """
CREATE TABLE IF NOT EXISTS `schema_migration_runs` (
    `id`          INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    `version`     INT          DEFAULT 0,
    `name`        VARCHAR(128) DEFAULT '',
    `level`       VARCHAR(16)  DEFAULT 'additive',
    `started_at`  VARCHAR(32)  DEFAULT NULL,
    `duration_ms` INT          DEFAULT 0,
    `success`     TINYINT      DEFAULT 0,
    `source`      VARCHAR(16)  DEFAULT '',
    `operator`    VARCHAR(64)  DEFAULT '',
    `ticket`      VARCHAR(64)  DEFAULT '',
    `reason`      VARCHAR(255) DEFAULT '',
    `error`       VARCHAR(500) DEFAULT '',
    `backup`      VARCHAR(255) DEFAULT '',
    `db_type`     VARCHAR(16)  DEFAULT '',
    `app_version` VARCHAR(32)  DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 账本后续加的列（老库自动补列，避免又走一遍「查列是否存在」的手工迁移）
_LEDGER_EXTRA_COLUMNS = (
    ('level', "VARCHAR(16) DEFAULT 'additive'"),
    ('operator', "VARCHAR(64) DEFAULT ''"),
    ('ticket', "VARCHAR(64) DEFAULT ''"),
    ('reason', "VARCHAR(255) DEFAULT ''"),
)

_MYSQL_LOCK_NAME = 'zcbot_schema_migrations'

# 同进程内的串行保护（SQLite 无命名锁，多进程共用同一个 sqlite 文件极少见）
_PROCESS_LOCK = threading.Lock()


def _now() -> str:
    """UTC ISO 8601（审计要可读、可比、无时区歧义）"""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@dataclass(frozen=True)
class Migration:
    """一条迁移定义

    version   : 单调递增的整数版本号，全局唯一
    name      : 短名（写进账本，便于人读，改动视为新迁移）
    apply     : ``fn(database) -> None``，必须幂等；失败抛异常即可
    description: 给人看的说明（状态接口/CLI 展示）
    level     : 变更级别，见 LEVEL_* 常量；默认 additive（不需要授权）
    """
    version: int
    name: str
    apply: Callable
    description: str = ''
    level: str = LEVEL_ADDITIVE

    def __post_init__(self):
        if self.level not in _LEVEL_RANK:
            raise ValueError(
                f"迁移 {self.name} 的 level={self.level!r} 非法，"
                f"只能是 {', '.join(LEVELS)}")


@dataclass
class MigrationContext:
    """执行上下文：谁、从哪、凭什么执行（审计三要素）

    source            : boot / cli / api / test
    operator          : 操作人标识（CLI 取 --operator 或系统用户名，API 取管理员名）
    ticket            : 变更单号（destructive 必填，SOX 的「变更单追溯」）
    reason            : 变更原因（人读）
    allow_destructive : 是否允许执行 destructive 迁移（默认否）
    backup            : 执行 rewrite/destructive 前是否备份（默认是）
    keep_backups      : SQLite 保留最近几份备份（默认 5）
    """
    source: str = 'unknown'
    operator: str = ''
    ticket: str = ''
    reason: str = ''
    allow_destructive: bool = False
    first_run: bool = False
    backup: bool = True
    keep_backups: int = 5
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, source: str, config: Optional[dict] = None,
                    **overrides) -> 'MigrationContext':
        """按 database.migrations 配置段构造（缺项取安全默认值）"""
        cfg = (config or {}).get('migrations') or {}
        if not isinstance(cfg, dict):
            cfg = {}
        kwargs = {
            'source': source,
            'operator': str(cfg.get('operator') or ''),
            'ticket': str(cfg.get('ticket') or ''),
            'reason': str(cfg.get('reason') or ''),
            'allow_destructive': bool(cfg.get('auto_apply_destructive', False)),
            'backup': bool(cfg.get('backup_before_rewrite', True)),
            'keep_backups': int(cfg.get('keep_backups', 5) or 5),
        }
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    def describe(self) -> str:
        who = self.operator or '(未记录)'
        ticket = f" 变更单 {self.ticket}" if self.ticket else ''
        return f"source={self.source} operator={who}{ticket}"


# ── 校验和 ────────────────────────────────────────────────────

_checksum_cache: Dict[int, str] = {}


def checksum_of(migration: Migration) -> str:
    """迁移体源码的 sha256（去掉公共缩进与行尾空白后计算）

    取源码而不是手工声明的字符串：手工声明等于「改代码忘了改校验和」，
    失效点正好在它要防的那件事上。SQL/文件型迁移（Flyway）哈希文件内容，
    这里等价地哈希函数体。
    """
    key = id(migration.apply)
    cached = _checksum_cache.get(key)
    if cached is not None:
        return cached
    try:
        src = inspect.getsource(migration.apply)
    except (OSError, TypeError):
        # 冻结分发（.pyc-only / 动态生成）取不到源码：不做校验，不误报
        _checksum_cache[key] = ''
        return ''
    norm = '\n'.join(
        line.rstrip() for line in textwrap.dedent(src).strip().splitlines())
    digest = hashlib.sha256(norm.encode('utf-8')).hexdigest()
    _checksum_cache[key] = digest
    return digest


# ── 账本 / 流水表 ─────────────────────────────────────────────

def _ensure_columns(database, table: str, columns) -> None:
    """给已有表补列（幂等）。列已存在或方言不支持时静默跳过。"""
    for name, ddl_type in columns:
        try:
            if database.table_has_column(table, name):
                continue
            database.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
            logger.info("数据库迁移: 账本表 %s 补充列 %s", table, name)
        except Exception as e:  # noqa: BLE001  并发补列/权限不足：不致命
            logger.debug("数据库迁移: 补充列 %s.%s 失败（可忽略）: %s", table, name, e)


def ensure_ledger(database) -> bool:
    """建账本表 + 补新列（幂等）。失败返回 False —— 退化为「不记账、照旧执行」。"""
    is_mysql = getattr(database, 'db_type', '') == 'mysql'
    ddl = _LEDGER_MYSQL if is_mysql else _LEDGER_SQLITE
    try:
        if not database.table_exists(LEDGER_TABLE):
            database.execute(ddl)
            logger.info("数据库迁移: 建立迁移账本表 %s", LEDGER_TABLE)
        _ensure_columns(database, LEDGER_TABLE, _LEDGER_EXTRA_COLUMNS)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "数据库迁移: 账本表 %s 不可用（%s），本次退化为不记账执行，"
            "迁移仍会逐条幂等执行但无法记录版本", LEDGER_TABLE, e)
        return False


def ensure_runs(database) -> bool:
    """建执行流水表（幂等）。失败只降级为「没有证据链」，不影响迁移本身。"""
    ddl = _RUNS_MYSQL if getattr(database, 'db_type', '') == 'mysql' else _RUNS_SQLITE
    try:
        if not database.table_exists(RUNS_TABLE):
            database.execute(ddl)
            logger.info("数据库迁移: 建立执行流水表 %s", RUNS_TABLE)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("数据库迁移: 流水表 %s 不可用（%s），本次执行不留审计记录",
                       RUNS_TABLE, e)
        return False


def _ledger_rows(database) -> Dict[int, dict]:
    """读取账本：version -> row（失败返回空表，调用方按「全未应用」处理）

    用 ``SELECT *`` 而不是列清单：老版本的账本表没有 level/operator 等列，
    显式列名会让整表读取失败，进而被误判成「一版都没应用」（危险：会重跑
    迁移）。缺列按默认值补齐即可。
    """
    try:
        rows = database.query(f"SELECT * FROM {LEDGER_TABLE} ORDER BY version")
    except Exception as e:  # noqa: BLE001
        logger.warning("数据库迁移: 读取账本失败: %s", e)
        return {}
    out = {}
    for r in rows or []:
        try:
            out[int(r['version'])] = {
                'version': int(r['version']),
                'name': r.get('name') or '',
                'checksum': r.get('checksum') or '',
                'applied_at': r.get('applied_at') or '',
                'duration_ms': int(r.get('duration_ms') or 0),
                'success': bool(int(r.get('success') or 0)),
                'origin': r.get('origin') or ORIGIN_RUN,
                'note': r.get('note') or '',
                'level': (r.get('level') or LEVEL_ADDITIVE),
                'operator': r.get('operator') or '',
                'ticket': r.get('ticket') or '',
                'reason': r.get('reason') or '',
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _write_record(database, migration: Migration, checksum: str, duration_ms: int,
                  success: bool, note: str = '', origin: str = ORIGIN_RUN,
                  context: Optional[MigrationContext] = None) -> None:
    """写/覆盖账本里的「当前状态」（同版本重试时覆盖上一次的失败记录）"""
    ctx = context or MigrationContext()
    now = str(int(time.time()))
    vals = (migration.name, checksum, now, int(duration_ms), 1 if success else 0,
            origin, (note or '')[:255], migration.level,
            (ctx.operator or '')[:64], (ctx.ticket or '')[:64],
            (ctx.reason or '')[:255])
    existing = database.query_one(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (migration.version,))
    if existing:
        database.execute(
            "UPDATE schema_migrations SET name = %s, checksum = %s, applied_at = %s, "
            "duration_ms = %s, success = %s, origin = %s, note = %s, level = %s, "
            "operator = %s, ticket = %s, reason = %s WHERE version = %s",
            vals + (migration.version,))
    else:
        database.execute(
            "INSERT INTO schema_migrations "
            "(name, checksum, applied_at, duration_ms, success, origin, note, level, "
            "operator, ticket, reason, version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            vals + (migration.version,))


def _record_run(database, migration: Optional[Migration], context: MigrationContext,
                started_at: str, duration_ms: int, success: bool,
                error: str = '', backup: str = '') -> None:
    """写一条执行流水（append-only）。失败只告警，绝不影响迁移结果。"""
    try:
        version = int(migration.version) if migration else 0
        app_version = ''
        try:
            from framework import __version__ as app_version  # noqa: F401
        except Exception:  # noqa: BLE001
            app_version = ''
        database.execute(
            "INSERT INTO schema_migration_runs "
            "(version, name, level, started_at, duration_ms, success, source, "
            "operator, ticket, reason, error, backup, db_type, app_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (version,
             (migration.name if migration else '(engine)')[:128],
             (migration.level if migration else ''),
             started_at, int(duration_ms), 1 if success else 0,
             (context.source or '')[:16], (context.operator or '')[:64],
             (context.ticket or '')[:64], (context.reason or '')[:255],
             (error or '')[:500], (backup or '')[:255],
             getattr(database, 'db_type', '')[:16], (app_version or '')[:32]))
    except Exception as e:  # noqa: BLE001
        logger.debug("数据库迁移: 写执行流水失败（不影响迁移）: %s", e)


def runs(database, limit: int = 100) -> List[dict]:
    """最近 N 条执行流水（新的在前），供接口与 CLI 取证"""
    try:
        if not database.table_exists(RUNS_TABLE):
            return []
        rows = database.query(
            "SELECT id, version, name, level, started_at, duration_ms, success, "
            "source, operator, ticket, reason, error, backup, db_type, app_version "
            f"FROM {RUNS_TABLE} ORDER BY id DESC LIMIT {int(limit)}")
    except Exception as e:  # noqa: BLE001
        logger.debug("数据库迁移: 读取执行流水失败: %s", e)
        return []
    out = []
    for r in rows or []:
        out.append({
            'id': int(r.get('id') or 0),
            'version': int(r.get('version') or 0),
            'name': r.get('name') or '',
            'level': r.get('level') or '',
            'started_at': r.get('started_at') or '',
            'duration_ms': int(r.get('duration_ms') or 0),
            'success': bool(int(r.get('success') or 0)),
            'source': r.get('source') or '',
            'operator': r.get('operator') or '',
            'ticket': r.get('ticket') or '',
            'reason': r.get('reason') or '',
            'error': r.get('error') or '',
            'backup': r.get('backup') or '',
            'db_type': r.get('db_type') or '',
            'app_version': r.get('app_version') or '',
        })
    return out


# ── 执行前备份（可回退承诺的物化） ───────────────────────────

_BACKUP_PREFIX = 'zcbot-schema-'


def backup_snapshot(database, tag: str = '', keep: int = 5) -> dict:
    """执行 rewrite / destructive 前的备份。

    SQLite：``VACUUM INTO`` 在线快照（不需要停写、不受 WAL 影响），落在
    库文件同级的 ``backups/`` 目录，按 ``keep`` 轮转。
    MySQL：不在这里 dump（会长时间锁表且体积不可控），改为记录 binlog 位点
    作为恢复点 —— 前提是实例开启了 binlog；拿不到位点就明确告警让运维
    自己确认备份，不假装有备份。

    返回 ``{'ok', 'kind', 'path'|'position', 'note'}``。
    """
    db_type = getattr(database, 'db_type', '')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    if db_type == 'mysql':
        position = ''
        try:
            row = database.query_one("SHOW MASTER STATUS")
            if row:
                position = f"{row.get('File', '')}:{row.get('Position', '')}"
        except Exception as e:  # noqa: BLE001
            return {'ok': False, 'kind': 'binlog', 'position': '',
                    'note': f"无法读取 binlog 位点（{e}）；请自行确认实例备份后再继续"}
        if not position:
            return {'ok': False, 'kind': 'binlog', 'position': '',
                    'note': '实例未开启 binlog 或无权读取；请自行确认备份后再继续'}
        return {'ok': True, 'kind': 'binlog', 'position': position,
                'note': f'恢复点 binlog {position}'}

    path = ''
    try:
        path = str((getattr(database, 'config', {}) or {}).get('path') or '')
        if not path:
            return {'ok': False, 'kind': 'file', 'path': '',
                    'note': '数据库路径未知，无法快照'}
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if not os.path.exists(path):
            return {'ok': False, 'kind': 'file', 'path': '',
                    'note': f'库文件不存在，跳过快照: {path}'}
        out_dir = os.path.join(os.path.dirname(path), 'backups')
        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(
            out_dir, f"{_BACKUP_PREFIX}{stamp}{('-' + tag) if tag else ''}.db")
        try:
            database.execute("VACUUM INTO %s", (target,))
        except Exception:  # noqa: BLE001  VACUUM INTO 不可用（旧版 SQLite）：退化为文件复制
            shutil.copy2(path, target)
            logger.warning("数据库迁移: VACUUM INTO 不可用，已退化为文件复制备份"
                           "（若库处于 WAL 模式，副本可能缺少未 checkpoint 的事务）")
        _prune_backups(out_dir, keep)
        logger.info("数据库迁移: 已备份到 %s", target)
        return {'ok': True, 'kind': 'file', 'path': target,
                'note': f'备份 {os.path.basename(target)}'}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'kind': 'file', 'path': '',
                'note': f'备份失败（{type(e).__name__}: {e}）'}


def _prune_backups(out_dir: str, keep: int) -> None:
    """只保留最近 keep 份，避免备份把磁盘吃满"""
    if keep is None or keep < 0:
        return
    try:
        files = sorted(
            (os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.startswith(_BACKUP_PREFIX)),
            key=lambda p: os.path.getmtime(p), reverse=True)
    except Exception:  # noqa: BLE001
        return
    for old in files[keep:]:
        try:
            os.remove(old)
        except Exception:  # noqa: BLE001
            pass


# ── 注册表校验 ────────────────────────────────────────────────

def _sorted_registry(registry: Sequence[Migration]) -> List[Migration]:
    """按版本排序并校验：版本必须为正整数且不重复"""
    seen = set()
    for m in registry:
        if not isinstance(m.version, int) or m.version <= 0:
            raise ValueError(f"迁移版本号必须是正整数: {m.name} -> {m.version!r}")
        if m.version in seen:
            raise ValueError(
                f"迁移版本号重复: {m.version}（{m.name}）；"
                "并发开发时两人各加一版常撞号，统一在末尾追加并顺延")
        seen.add(m.version)
        if m.level not in _LEVEL_RANK:
            raise ValueError(
                f"迁移 {m.name} 的 level={m.level!r} 非法，只能是 {', '.join(LEVELS)}")
    return sorted(registry, key=lambda m: m.version)


def latest_version(registry: Sequence[Migration]) -> Optional[int]:
    reg = list(registry)
    return max((m.version for m in reg), default=None)


def next_version(registry: Sequence[Migration]) -> int:
    """下一个可用版本号（新增迁移时用）"""
    return (latest_version(registry) or 0) + 1


# ── 授权判定 ──────────────────────────────────────────────────

def needs_approval(migration: Migration) -> bool:
    """destructive 一律要人工授权；additive / rewrite 不需要"""
    return migration.level == LEVEL_DESTRUCTIVE


def approval_gap(migration: Migration, context: MigrationContext) -> str:
    """返回「为什么不能执行」的原因；空串表示可以执行"""
    if not needs_approval(migration):
        return ''
    if context.first_run:
        # 首次接管（库里没有任何账本记录）：历史迁移一次性收敛，见 run_pending
        return ''
    if not context.allow_destructive:
        return ('destructive 迁移不允许自动执行（ITIL 变更分级 / SOX 职责分离）：'
                '需人工执行 `python -m framework.database.migrate up '
                f'--approve <变更单号> --reason "<原因>"`')
    if not (context.ticket or '').strip():
        return 'destructive 迁移必须提供变更单号（--approve <变更单号>）'
    return ''


# ── MySQL 命名锁 ──────────────────────────────────────────────

def _mysql_lock(database, timeout: int = 30):
    """占用 MySQL 命名锁，返回 (handle, ok, note)

    MySQL 的 GET_LOCK 是**连接级**锁，而 Database 每次执行都会把池连接归还，
    锁会随之释放，所以这里单开一条专用连接持有到迁移结束。
    拿不到锁能力（无驱动/权限不足）不算失败：退化为无锁执行并告警。
    """
    try:
        cfg = getattr(database, 'config', {}) or {}
        conn = database._pymysql.connect(
            host=cfg.get('host', '127.0.0.1'),
            port=int(cfg.get('port', 3306)),
            user=cfg.get('user', 'root'),
            password=cfg.get('password', ''),
            charset='utf8mb4',
            autocommit=True,
        )
        cur = conn.cursor()
        cur.execute("SELECT GET_LOCK(%s, %s)", (_MYSQL_LOCK_NAME, timeout))
        row = cur.fetchone()
        if not row or int(row[0]) != 1:
            cur.close()
            conn.close()
            return None, False, f"等待命名锁超时（{timeout}s）"
        return (conn, cur), True, ''
    except Exception as e:  # noqa: BLE001
        return None, True, f"命名锁不可用，已退化为无锁执行: {e}"


def _release_mysql_lock(handle) -> None:
    if not handle:
        return
    conn, cur = handle
    try:
        cur.execute("SELECT RELEASE_LOCK(%s)", (_MYSQL_LOCK_NAME,))
    except Exception:  # noqa: BLE001
        pass
    finally:
        for closer in (cur.close, conn.close):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


# ── 主流程 ────────────────────────────────────────────────────

def _empty_report(database, registry) -> dict:
    return {
        'ok': True,
        'db_type': getattr(database, 'db_type', 'unknown'),
        'ledger': False,
        'runs_ledger': False,
        'current': None,
        'latest': latest_version(registry),
        'applied': [],
        'skipped': 0,
        'pending': [],
        'failed': [],
        'blocked': [],
        'drift': [],
        'backup': '',
        'first_run': False,
        'elapsed_ms': 0,
    }


def plan(database, registry: Sequence[Migration],
         context: Optional[MigrationContext] = None) -> dict:
    """干跑（dry run）：列出「将要执行什么、是否需要授权、会不会备份」。

    不执行任何迁移体、不写账本，供上线前评审与 CI 使用。
    """
    ctx = context or MigrationContext(source='plan')
    reg = _sorted_registry(registry)
    ledger = False
    try:
        ledger = database.table_exists(LEDGER_TABLE)
    except Exception:  # noqa: BLE001
        ledger = False
    rows = _ledger_rows(database) if ledger else {}
    done = {v for v, r in rows.items() if r['success']}

    steps = []
    for m in reg:
        if m.version in done:
            continue
        gap = approval_gap(m, ctx)
        steps.append({
            'version': m.version,
            'name': m.name,
            'description': m.description,
            'level': m.level,
            'requires_approval': needs_approval(m),
            'blocked_reason': gap,
            'will_backup': bool(ctx.backup) and m.level != LEVEL_ADDITIVE,
        })
    return {
        'ok': all(not s['blocked_reason'] for s in steps),
        'db_type': getattr(database, 'db_type', 'unknown'),
        'ledger': ledger,
        'current': max(done) if done else None,
        'latest': latest_version(reg),
        'pending_count': len(steps),
        'steps': steps,
        'context': {'source': ctx.source, 'operator': ctx.operator,
                    'ticket': ctx.ticket,
                    'allow_destructive': ctx.allow_destructive,
                    'backup': ctx.backup},
    }


def run_pending(database, registry: Sequence[Migration],
                context: Optional[MigrationContext] = None,
                lock_timeout: int = 30) -> dict:
    """把注册表里尚未成功应用、且已获授权的迁移按序执行，返回执行报告。

    不抛异常（除非注册表本身非法）：单条迁移失败记入 ``failed`` 并**停止**
    后续迁移（后面的版本可能依赖它）；未获授权的 destructive 迁移记入
    ``blocked`` 并停止，由调用方决定告警级别与退出码。
    """
    ctx = context or MigrationContext(source='unknown')
    reg = _sorted_registry(registry)
    report = _empty_report(database, reg)
    started = time.time()

    report['ledger'] = ensure_ledger(database)
    report['runs_ledger'] = ensure_runs(database) if report['ledger'] else False
    rows = _ledger_rows(database) if report['ledger'] else {}
    done = {v for v, r in rows.items() if r['success']}

    # 首次接管：账本里一条记录都没有（新库，或升级上来的老库）。
    # 此时不存在「上一个已批准的版本」可以保护，历史迁移又是一贯幂等执行的，
    # 若把它们拦在授权闸门外，安装/升级会卡在半途（连 additive 都跑不完）。
    # 故首次接管允许一次性收敛，但明确告警并写入流水备注；此后的 destructive
    # 迁移一律走人工授权。等价于 Flyway 的 baselineOnMigrate。
    first_run = not rows
    if first_run:
        ctx = replace(ctx, allow_destructive=True, first_run=True,
                      reason=ctx.reason or '首次接管，历史迁移一次性收敛')
        report['first_run'] = True
        if any(m.level == LEVEL_DESTRUCTIVE for m in reg):
            logger.warning(
                "数据库迁移: 首次接管该库（无迁移账本），历史迁移将一次性执行收敛"
                "（含 destructive 级）：%s。此后新增的 destructive 迁移必须人工授权。",
                ', '.join('v%d %s' % (m.version, m.name) for m in reg
                          if m.level == LEVEL_DESTRUCTIVE))

    # 校验和漂移：已应用的迁移被改动 —— 报错、不重跑（不可变原则）
    for m in reg:
        row = rows.get(m.version)
        if not row or not row['success'] or row['origin'] == ORIGIN_BASELINE:
            continue
        recorded = row['checksum']
        actual = checksum_of(m)
        if recorded and actual and recorded != actual:
            report['drift'].append({
                'version': m.version, 'name': m.name,
                'recorded': recorded[:12], 'actual': actual[:12],
            })
            logger.error(
                "数据库迁移: v%d %s 的内容与账本校验和不一致（已应用 %s / 当前 %s）。"
                "已应用的迁移必须不可变：若确需调整，请新增一版迁移，不要改旧版。",
                m.version, m.name, recorded[:12], actual[:12])

    report['current'] = max(done) if done else None
    report['pending'] = [
        {'version': m.version, 'name': m.name, 'description': m.description,
         'level': m.level}
        for m in reg if m.version not in done]
    if not report['pending']:
        report['skipped'] = len(reg)
        report['elapsed_ms'] = int((time.time() - started) * 1000)
        return report

    # 闸门在拿锁之前先判一次：拒绝得快，也不白占锁
    gate = next((m for m in reg
                 if m.version not in done and approval_gap(m, ctx)), None)
    if gate is not None:
        reason = approval_gap(gate, ctx)
        report['ok'] = False
        report['blocked'].append({
            'version': gate.version, 'name': gate.name,
            'level': gate.level, 'reason': reason})
        _record_run(database, gate, ctx, _now(), 0, False,
                    error=f'blocked: {reason}')
        logger.error("数据库迁移被授权闸门拦下: v%d %s（%s）— %s",
                     gate.version, gate.name, gate.level, reason)
        report['elapsed_ms'] = int((time.time() - started) * 1000)
        return report

    lock_handle, lock_ok, lock_note = None, True, ''
    with _PROCESS_LOCK:
        if getattr(database, 'db_type', '') == 'mysql':
            lock_handle, lock_ok, lock_note = _mysql_lock(database, lock_timeout)
        try:
            if not lock_ok:
                # 另一个节点正在迁移：本节点不抢，但要明确告警（schema 可能仍是旧的）
                report['ok'] = False
                report['failed'].append({
                    'version': 0, 'name': '(lock)', 'error': lock_note})
                logger.error("数据库迁移: %s，本次跳过待应用迁移 %s",
                             lock_note, [p['version'] for p in report['pending']])
                return report
            if lock_note:
                logger.debug("数据库迁移: %s", lock_note)

            # 拿到锁后重读账本：可能别的进程刚跑完
            if report['ledger']:
                rows = _ledger_rows(database)
                done = {v for v, r in rows.items() if r['success']}
            report['pending'] = [
                {'version': m.version, 'name': m.name, 'description': m.description,
                 'level': m.level}
                for m in reg if m.version not in done]

            backed_up = False
            for m in reg:
                if m.version in done:
                    report['skipped'] += 1
                    continue
                gap = approval_gap(m, ctx)
                if gap:
                    report['ok'] = False
                    report['blocked'].append({
                        'version': m.version, 'name': m.name,
                        'level': m.level, 'reason': gap})
                    _record_run(database, m, ctx, _now(), 0, False,
                                error=f'blocked: {gap}')
                    logger.error("数据库迁移被授权闸门拦下: v%d %s — %s",
                                 m.version, m.name, gap)
                    break

                # 有损/重写类变更先留退路（同一轮只备一次，够用且省磁盘）
                if ctx.backup and m.level != LEVEL_ADDITIVE and not backed_up:
                    snap = backup_snapshot(database, tag=f'v{m.version}',
                                           keep=ctx.keep_backups)
                    backed_up = True
                    report['backup'] = snap.get('path') or snap.get('position') or ''
                    if snap.get('ok'):
                        logger.info("数据库迁移: v%d 执行前备份完成 — %s",
                                    m.version, snap.get('note'))
                    else:
                        logger.error("数据库迁移: v%d 执行前备份未完成 — %s",
                                     m.version, snap.get('note'))

                csum = checksum_of(m) if report['ledger'] else ''
                started_at = _now()
                t0 = time.time()
                backup_ref = report['backup'] if m.level != LEVEL_ADDITIVE else ''
                try:
                    m.apply(database)
                except Exception as e:  # noqa: BLE001
                    cost = int((time.time() - t0) * 1000)
                    err = f"{type(e).__name__}: {e}"
                    report['ok'] = False
                    report['failed'].append(
                        {'version': m.version, 'name': m.name, 'error': err[:300]})
                    logger.error(
                        "数据库迁移失败: v%d %s（%dms）— %s；后续迁移已停止，"
                        "下次启动会自动重试（迁移必须幂等）",
                        m.version, m.name, cost, err)
                    if report['ledger']:
                        try:
                            _write_record(database, m, csum, cost, False, err,
                                          context=ctx)
                        except Exception as we:  # noqa: BLE001
                            logger.warning("数据库迁移: 失败记录写账本失败: %s", we)
                    _record_run(database, m, ctx, started_at, cost, False,
                                error=err, backup=backup_ref)
                    break
                cost = int((time.time() - t0) * 1000)
                report['applied'].append(
                    {'version': m.version, 'name': m.name,
                     'level': m.level, 'duration_ms': cost})
                if report['ledger']:
                    try:
                        _write_record(database, m, csum, cost, True, context=ctx)
                    except Exception as we:  # noqa: BLE001
                        logger.warning("数据库迁移: 账本写入失败: %s", we)
                _record_run(database, m, ctx, started_at, cost, True,
                            backup=backup_ref)
                logger.info("数据库迁移: v%d %s 已应用（%dms，%s）",
                            m.version, m.name, cost, m.level)
        finally:
            _release_mysql_lock(lock_handle)
            if report['ledger']:
                rows = _ledger_rows(database)
                done = {v for v, r in rows.items() if r['success']}
                report['current'] = max(done) if done else None
            report['elapsed_ms'] = int((time.time() - started) * 1000)

    return report


def status(database, registry: Sequence[Migration],
           context: Optional[MigrationContext] = None,
           run_limit: int = 20) -> dict:
    """只读状态：当前版本 / 待应用（含级别与授权要求）/ 漂移 / 历史 / 流水"""
    ctx = context or MigrationContext(source='status')
    reg = _sorted_registry(registry)
    latest = latest_version(reg)
    ledger = False
    try:
        ledger = database.table_exists(LEDGER_TABLE)
    except Exception:  # noqa: BLE001
        ledger = False

    rows = _ledger_rows(database) if ledger else {}
    done = {v for v, r in rows.items() if r['success']}
    # 与 run_pending 保持一致：无账本记录的库属于「首次接管」，历史迁移会一次性收敛
    if not rows:
        ctx = replace(ctx, allow_destructive=True, first_run=True)
    drift = []
    for m in reg:
        row = rows.get(m.version)
        if not row or not row['success'] or row['origin'] == ORIGIN_BASELINE:
            continue
        recorded, actual = row['checksum'], checksum_of(m)
        if recorded and actual and recorded != actual:
            drift.append({'version': m.version, 'name': m.name,
                          'recorded': recorded[:12], 'actual': actual[:12]})

    pending = []
    for m in reg:
        if m.version in done:
            continue
        gap = approval_gap(m, ctx)
        pending.append({
            'version': m.version, 'name': m.name,
            'description': m.description, 'level': m.level,
            'requires_approval': needs_approval(m),
            'blocked_reason': gap,
        })
    # 账本里 level 可能来自旧版本的默认值；展示时以注册表为准（不改历史行）
    level_by_version = {m.version: m.level for m in reg}
    history = [dict(rows[v], level=level_by_version.get(v, rows[v]['level']))
               for v in sorted(rows, reverse=True)]
    blocked = [p for p in pending if p['blocked_reason']]

    return {
        'ok': not drift and not blocked,
        'ledger': ledger,
        'db_type': getattr(database, 'db_type', 'unknown'),
        'current': max(done) if done else None,
        'latest': latest,
        'applied_count': len(done),
        'total': len(reg),
        'pending': pending,
        'blocked': blocked,
        'drift': drift,
        'history': history,
        'runs': runs(database, run_limit) if ledger else [],
    }


def verify(database, registry: Sequence[Migration],
           context: Optional[MigrationContext] = None) -> dict:
    """变更后自检（post-implementation review）：结构是否真的到位。

    三查：账本版本 vs 代码版本、必需的框架表是否齐全、是否还有未授权迁移。
    返回 ``{'ok', 'problems': [...]}``，可直接进 CI 或启动自检。
    """
    ctx = context or MigrationContext(source='verify')
    st = status(database, registry, ctx, run_limit=0)
    problems = []
    if st['latest'] and (st['current'] or 0) < (st['latest'] or 0):
        problems.append(
            f"结构落后于代码：账本 v{st['current']} < 代码 v{st['latest']}")
    if st['drift']:
        problems.append(f"校验和漂移 {len(st['drift'])} 处")
    if st['blocked']:
        problems.append("未授权迁移 %d 条：%s" % (
            len(st['blocked']),
            ', '.join('v%d' % p['version'] for p in st['blocked'])))
    try:
        from framework.database.init_db import missing_required_tables
        missing = missing_required_tables(database, include_extension=True)
        if missing:
            problems.append(f"缺失表 {len(missing)} 张：{', '.join(missing[:5])}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"结构自检失败：{type(e).__name__}: {e}")
    return {'ok': not problems, 'problems': problems,
            'current': st['current'], 'latest': st['latest']}


def stamp(database, registry: Sequence[Migration], version: int,
          context: Optional[MigrationContext] = None) -> dict:
    """基线认定：把 1..version 记为已应用（不执行迁移体）

    用途只有一个 —— 接管一个「schema 早已是目标状态、但没有账本」的老库，
    避免首次启动把历史迁移重跑一遍。前提是运维确认该库结构确实等于该版本，
    否则会把缺陷永久记账掩盖掉。这条操作本身也进执行流水（origin=baseline）。
    """
    ctx = context or MigrationContext(source='baseline')
    reg = _sorted_registry(registry)
    if latest_version(reg) is not None and version > latest_version(reg):
        raise ValueError(f"基线版本 {version} 超过最新版本 {latest_version(reg)}")
    if not ensure_ledger(database):
        raise RuntimeError(f"账本表 {LEDGER_TABLE} 不可用，无法记录基线")
    ensure_runs(database)
    stamped = []
    for m in reg:
        if m.version > version:
            break
        _write_record(database, m, checksum_of(m), 0, True,
                      note=f'baseline to v{version}', origin=ORIGIN_BASELINE,
                      context=ctx)
        _record_run(database, m, ctx, _now(), 0, True,
                    error='', backup='')
        stamped.append(m.version)
    logger.info("数据库迁移: 已基线认定到 v%s（记录 %d 版，未执行迁移体）",
                version, len(stamped))
    return {'ok': True, 'baseline': version, 'stamped': stamped}
