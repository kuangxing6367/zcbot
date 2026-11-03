# -*- coding: utf-8 -*-
"""
数据库迁移引擎：版本化账本 + 校验和 + 按序执行

对齐主流迁移工具（Flyway / Liquibase / Alembic / Rails）的共同契约：

1. **账本表** ``schema_migrations`` 记录「这个库已经应用到哪一版」，
   而不是靠「查列是否存在」反推当前版本；
2. **版本单调递增、按序执行、每版只执行一次**；
3. **校验和**：已应用的迁移若被人改动，启动时报错并暴露在状态里
   （改了也不会重跑 —— 已应用的迁移一律不可变）；
4. **生产只前进**（roll forward）：改错了补一版新的，不回滚旧版；
5. **失败不记账**：迁移抛异常时写 ``success=0``，下次启动自动重试；
6. **迁移体必须幂等**：MySQL 的 DDL 无法事务化，中断只能靠重入收敛。

边界：
- 本模块只负责「账本 + 调度」，不认识任何具体表结构。迁移内容由调用方
  传入注册表（框架的在 ``framework/database/schema.py`` 的 ``MIGRATIONS``，
  插件可自带注册表复用同一套引擎）。
- 建表语句（CREATE TABLE IF NOT EXISTS）不属于迁移，仍在 schema.py，
  先于迁移执行。

为什么不把账本表写进 sql/init.sql：
账本是**工具自己维护的元数据**（Flyway 的 flyway_schema_history、Rails 的
schema_migrations 同理），不属于业务 schema。写进 init.sql 会让
``missing_required_tables`` 把它当成核心表，一旦缺失就触发整轮「建表 +
种子数据」流程，语义错位。
"""
import hashlib
import inspect
import logging
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger('zcbot')

LEDGER_TABLE = 'schema_migrations'

# 账本里 origin 的取值：
#   run      —— 引擎实际执行过
#   baseline —— 运维手工认定「这个库的 schema 已经等于该版本」（不执行，只记账）
ORIGIN_RUN = 'run'
ORIGIN_BASELINE = 'baseline'

_LEDGER_SQLITE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    checksum    VARCHAR(64)  DEFAULT '',
    applied_at  VARCHAR(32)  DEFAULT NULL,
    duration_ms INTEGER      DEFAULT 0,
    success     INTEGER      DEFAULT 1,
    origin      VARCHAR(16)  DEFAULT 'run',
    note        VARCHAR(255) DEFAULT ''
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
    `note`        VARCHAR(255) DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_MYSQL_LOCK_NAME = 'zcbot_schema_migrations'

# 同进程内的串行保护（SQLite 无命名锁，多进程共用同一个 sqlite 文件极少见）
_PROCESS_LOCK = threading.Lock()


@dataclass(frozen=True)
class Migration:
    """一条迁移定义

    version   : 单调递增的整数版本号，全局唯一
    name      : 短名（写进账本，便于人读，改动视为新迁移）
    apply     : ``fn(database) -> None``，必须幂等；失败抛异常即可
    description: 给人看的说明（状态接口/CLI 展示）
    """
    version: int
    name: str
    apply: Callable
    description: str = ''


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


# ── 账本读写 ──────────────────────────────────────────────────

def ensure_ledger(database) -> bool:
    """建账本表（幂等）。失败返回 False —— 引擎随后退化为「不记账、照旧执行」。"""
    ddl = _LEDGER_MYSQL if getattr(database, 'db_type', '') == 'mysql' else _LEDGER_SQLITE
    try:
        if database.table_exists(LEDGER_TABLE):
            return True
        database.execute(ddl)
        logger.info("数据库迁移: 建立迁移账本表 %s", LEDGER_TABLE)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "数据库迁移: 账本表 %s 不可用（%s），本次退化为不记账执行，"
            "迁移仍会逐条幂等执行但无法记录版本", LEDGER_TABLE, e)
        return False


def _ledger_rows(database) -> Dict[int, dict]:
    """读取账本：version -> row（失败返回空表，调用方按「全未应用」处理）"""
    try:
        rows = database.query(
            "SELECT version, name, checksum, applied_at, duration_ms, success, "
            "origin, note FROM schema_migrations ORDER BY version")
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
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _write_record(database, migration: Migration, checksum: str, duration_ms: int,
                  success: bool, note: str = '', origin: str = ORIGIN_RUN) -> None:
    """写/覆盖一条账本记录（同版本重试时覆盖上一次的失败记录）"""
    now = str(int(time.time()))
    existing = database.query_one(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (migration.version,))
    if existing:
        database.execute(
            "UPDATE schema_migrations SET name = %s, checksum = %s, applied_at = %s, "
            "duration_ms = %s, success = %s, origin = %s, note = %s WHERE version = %s",
            (migration.name, checksum, now, int(duration_ms), 1 if success else 0,
             origin, (note or '')[:255], migration.version))
    else:
        database.execute(
            "INSERT INTO schema_migrations "
            "(version, name, checksum, applied_at, duration_ms, success, origin, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (migration.version, migration.name, checksum, now, int(duration_ms),
             1 if success else 0, origin, (note or '')[:255]))


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
    return sorted(registry, key=lambda m: m.version)


def latest_version(registry: Sequence[Migration]) -> Optional[int]:
    reg = list(registry)
    return max((m.version for m in reg), default=None)


def next_version(registry: Sequence[Migration]) -> int:
    """下一个可用版本号（新增迁移时用）"""
    return (latest_version(registry) or 0) + 1


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
        'current': None,
        'latest': latest_version(registry),
        'applied': [],
        'skipped': 0,
        'pending': [],
        'failed': [],
        'drift': [],
        'elapsed_ms': 0,
    }


def run_pending(database, registry: Sequence[Migration],
                lock_timeout: int = 30) -> dict:
    """把注册表里尚未成功应用的迁移按序执行，返回执行报告。

    不抛异常（除非注册表本身非法）：单条迁移失败记入 ``failed`` 并**停止**
    后续迁移（后面的版本可能依赖它），由调用方决定告警级别。
    """
    reg = _sorted_registry(registry)
    report = _empty_report(database, reg)
    started = time.time()

    report['ledger'] = ensure_ledger(database)
    rows = _ledger_rows(database) if report['ledger'] else {}
    done = {v for v, r in rows.items() if r['success']}

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
        {'version': m.version, 'name': m.name, 'description': m.description}
        for m in reg if m.version not in done]
    if not report['pending']:
        report['skipped'] = len(reg)
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
                {'version': m.version, 'name': m.name, 'description': m.description}
                for m in reg if m.version not in done]

            for m in reg:
                if m.version in done:
                    report['skipped'] += 1
                    continue
                csum = checksum_of(m) if report['ledger'] else ''
                t0 = time.time()
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
                            _write_record(database, m, csum, cost, False, err)
                        except Exception as we:  # noqa: BLE001
                            logger.warning("数据库迁移: 失败记录写账本失败: %s", we)
                    break
                cost = int((time.time() - t0) * 1000)
                report['applied'].append(
                    {'version': m.version, 'name': m.name, 'duration_ms': cost})
                if report['ledger']:
                    try:
                        _write_record(database, m, csum, cost, True)
                    except Exception as we:  # noqa: BLE001
                        logger.warning("数据库迁移: 账本写入失败: %s", we)
                logger.info("数据库迁移: v%d %s 已应用（%dms）",
                            m.version, m.name, cost)
        finally:
            _release_mysql_lock(lock_handle)
            if report['ledger']:
                rows = _ledger_rows(database)
                done = {v for v, r in rows.items() if r['success']}
                report['current'] = max(done) if done else None
            report['elapsed_ms'] = int((time.time() - started) * 1000)

    return report


def status(database, registry: Sequence[Migration]) -> dict:
    """只读状态：当前版本 / 待应用 / 校验和漂移 / 历史（供 API 与 CLI）"""
    reg = _sorted_registry(registry)
    latest = latest_version(reg)
    ledger = False
    try:
        ledger = database.table_exists(LEDGER_TABLE)
    except Exception:  # noqa: BLE001
        ledger = False

    rows = _ledger_rows(database) if ledger else {}
    done = {v for v, r in rows.items() if r['success']}
    drift = []
    for m in reg:
        row = rows.get(m.version)
        if not row or not row['success'] or row['origin'] == ORIGIN_BASELINE:
            continue
        recorded, actual = row['checksum'], checksum_of(m)
        if recorded and actual and recorded != actual:
            drift.append({'version': m.version, 'name': m.name,
                          'recorded': recorded[:12], 'actual': actual[:12]})

    pending = [
        {'version': m.version, 'name': m.name, 'description': m.description}
        for m in reg if m.version not in done]
    history = [rows[v] for v in sorted(rows, reverse=True)]

    return {
        'ok': not drift,
        'ledger': ledger,
        'db_type': getattr(database, 'db_type', 'unknown'),
        'current': max(done) if done else None,
        'latest': latest,
        'applied_count': len(done),
        'total': len(reg),
        'pending': pending,
        'drift': drift,
        'history': history,
    }


def stamp(database, registry: Sequence[Migration], version: int) -> dict:
    """基线认定：把 1..version 记为已应用（不执行迁移体）

    用途只有一个 —— 接管一个「schema 早已是目标状态、但没有账本」的老库，
    避免首次启动把历史迁移重跑一遍。前提是运维确认该库结构确实等于该版本，
    否则会把缺陷永久记账掩盖掉。
    """
    reg = _sorted_registry(registry)
    if latest_version(reg) is not None and version > latest_version(reg):
        raise ValueError(f"基线版本 {version} 超过最新版本 {latest_version(reg)}")
    if not ensure_ledger(database):
        raise RuntimeError(f"账本表 {LEDGER_TABLE} 不可用，无法记录基线")
    stamped = []
    for m in reg:
        if m.version > version:
            break
        _write_record(database, m, checksum_of(m), 0, True,
                      note=f'baseline to v{version}', origin=ORIGIN_BASELINE)
        stamped.append(m.version)
    logger.info("数据库迁移: 已基线认定到 v%s（记录 %d 版，未执行迁移体）",
                version, len(stamped))
    return {'ok': True, 'baseline': version, 'stamped': stamped}
