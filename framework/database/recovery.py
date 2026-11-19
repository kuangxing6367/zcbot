"""SQLite 损坏后的应急恢复：先备份，再按组合尝试恢复，最后让框架"崩溃退出"。

A/B/C 三套手段是 SQLite 官方支持的：
- A ``REINDEX`` —— 索引类损坏，纯标准库，代价最小；
- B ``VACUUM INTO`` —— 库还能读时重建整库，纯标准库；
- C ``.recover`` CLI —— 库读不动时打捞，需要外部 ``sqlite3`` 命令。

组合顺序按手写稿：单个 → 两两 → 六种三排列。每一种组合都在原库的副本上做，
前一步的产物接着给后一步用；任何一步校验通过就停下。

恢复结果**永远不能当成数据完整**：SQLite 官方文档明确写了打捞可能丢行、
复活已删除内容、改掉字段类型、破坏约束。所以每次都把结果写进 ``error.log``。

退出走框架**原生的崩溃守卫**，不另造哨兵：本模块写一份崩溃日志（计入
``error.log`` 轮转），然后主动退出进程。守护脚本若把框架拉起，启动时
``should_abort_boot`` 一看崩溃计数还没到 ``max_restarts`` 就正常启动，再撞到
损坏再写再崩，计数累加；到了上限直接进安全模式（假启动），进程活着但不加载
插件、不写库，直到人工核对数据。这样完全复用既有机制，用户看 error.log 就是
处置指引，不用理解新概念。

没有 emoji：这是排障记录，不是社交消息。
"""
import json
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from framework.config import get_config
from framework.paths import home_dir, state_dir

from . import backup as backup_mod

logger = logging.getLogger('zcbot')

ERROR_LOG_NAME = 'error.log'
CLI_ENV = 'ZCBOT_SQLITE3_CLI'
CLI_TIMEOUT_SEC = 300
EXIT_CODE = 1

# A/B/C 与纸上顺序一致：ABC / BAC / CAB / CBA / ACB / BCA
METHODS = ('reindex', 'vacuum_into', 'recover_cli')
PAIRS = (('reindex', 'vacuum_into'), ('reindex', 'recover_cli'),
         ('vacuum_into', 'recover_cli'))
TRIPLES = (('reindex', 'vacuum_into', 'recover_cli'),
           ('vacuum_into', 'reindex', 'recover_cli'),
           ('recover_cli', 'reindex', 'vacuum_into'),
           ('recover_cli', 'vacuum_into', 'reindex'),
           ('reindex', 'recover_cli', 'vacuum_into'),
           ('vacuum_into', 'recover_cli', 'reindex'))

CORRUPT_MARKERS = (
    'malformed', 'not a database', 'database disk image is malformed',
    'file is not a database', 'sqlite__corrupt',
)
CORRUPT_ERRNOS = ('SQLITE_CORRUPT', 'SQLITE_NOTADB', 'SQLITE_CORRUPT_VTAB')

DATA_WARNING = (
    '数据有可能已损坏：恢复只能尽力打捞，可能丢行、把已删除的内容带回来、'
    '改掉字段类型或破坏约束。请自行排查后再继续使用。'
)


def is_corruption(err) -> bool:
    """只认明确的损坏信号，不把「磁盘满」「锁冲突」误判成损坏。"""
    name = str(getattr(err, 'sqlite_errorname', '') or '').upper()
    if any(marker in name for marker in CORRUPT_ERRNOS):
        return True
    msg = str(err).lower()
    return any(marker in msg for marker in CORRUPT_MARKERS)


def _state_dir(home=None) -> str:
    return os.path.join(home, 'data') if home else state_dir()


def write_crash_report(report: dict, home=None, log_dir=None) -> str:
    """把恢复结果写进崩溃日志 error.log，供框架原生崩溃守卫计数与展示。

    复用 ``crash_guard.write_crash_log`` 而不是自己拼一份：计数（error.log 及
    其 .N 轮转副本）、达阈值进安全模式、外部告警都是那套原语既有能力。这里
    只负责把内容写成"人能看懂的处置指引"——用户明确说过，光给一堆 traceback
    会让人困惑，所以第一行就说清发生了什么、数据会怎样、该怎么处理。

    返回写成的 error.log 路径。写失败不能吞掉——调用方要靠它决定后续动作。
    """
    from framework import crash_guard

    crash_cfg = {}
    try:
        crash_cfg = get_config() or {}
    except Exception as e:
        logger.debug('读取崩溃配置失败，按默认 error.log 路径写: %s', e)

    root = home_dir()
    # 若显式指定了日志目录，用绝对路径覆盖配置里的 path，避开 home/root 差异。
    if log_dir:
        crash_cfg = dict(crash_cfg)
        fw = dict(crash_cfg.get('framework') or {})
        crash = dict(fw.get('crash') or {})
        crash['path'] = os.path.join(log_dir, ERROR_LOG_NAME)
        fw['crash'] = crash
        crash_cfg['framework'] = fw

    status = report.get('status', 'unknown')
    ok = bool(report.get('ok'))
    backup = report.get('backup_path') or '无'
    if ok:
        headline = '数据库损坏：已完成应急恢复，但数据可能不完整，请人工核对后再用。'
    else:
        headline = '数据库损坏：应急恢复未能完成，框架将停止服务，请勿无脑重启。'
    detail = '\n'.join([
        headline,
        '  - 恢复结果：%s' % status,
        '  - 备份位置：%s' % backup,
        '  - %s' % DATA_WARNING,
        '  - 处置：核对上面这份备份 -> 确认数据是否需要回滚 -> 删除本 error.log 及'
        '其 .1/.2... 轮转副本后再启动；连续崩溃达到上限时框架会进入安全模式'
        '（进程挂着但不做事），这是刻意设计，不是卡死。',
        '  - 说明：这是框架检测到数据库损坏后主动留的排障记录，不是普通的程序异常。',
    ])
    try:
        path = crash_guard.write_crash_log(
            crash_cfg, root, exc=None,
            extra={'数据库应急恢复': detail,
                   '损坏信号': report.get('note', '') or status,
                   '环境': report.get('env') or environment_info()})
        if not path:
            raise OSError('crash_guard.write_crash_log 未返回落盘路径')
        return path
    except Exception as e:
        logger.error('写崩溃日志 error.log 失败（%s: %s）', type(e).__name__, e)
        raise


def environment_info(db_path=None) -> dict:
    """把排障最常问的三类事实一次性记下来：系统、容器、容量。"""
    cgroup = ''
    for candidate in ('/proc/1/cgroup', '/proc/self/cgroup'):
        try:
            with open(candidate, encoding='utf-8', errors='ignore') as f:
                cgroup = f.read()
            break
        except OSError:
            continue
    in_docker = os.path.exists('/.dockerenv') or 'docker' in cgroup.lower()
    in_lxc = 'lxc' in cgroup.lower()
    info = {
        'system': platform.system() or '未知',
        'platform': platform.platform(),
        'python': sys.version.split()[0],
        'sqlite': sqlite3.sqlite_version,
        'in_lxc': in_lxc,
        'in_docker': in_docker,
        'in_venv': sys.prefix != sys.base_prefix,
        'cli': _find_cli(),
        'home': home_dir(),
    }
    try:
        target = os.path.dirname(os.path.abspath(db_path)) if db_path else state_dir()
        info['free_bytes'] = shutil.disk_usage(target).free
        info['target'] = target
    except OSError as e:
        info['free_bytes'] = -1
        info['free_error'] = str(e)
    if db_path and os.path.isfile(db_path):
        try:
            info['db_bytes'] = os.path.getsize(db_path)
        except OSError:
            info['db_bytes'] = -1
    return info


def _append_error_log(report: dict, home=None, log_dir=None) -> str:
    directory = log_dir or os.path.join(_state_dir(home), 'logs')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, ERROR_LOG_NAME)
    line = {
        'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'status': report.get('status'),
        'ok': report.get('ok'),
        'db_path': report.get('db_path'),
        'backup_path': report.get('backup_path'),
        'attempts': report.get('attempts', []),
        'env': report.get('env'),
        'note': report.get('note'),
        'warning': DATA_WARNING,
    }
    with open(path, 'a', encoding='utf-8', newline='') as f:
        f.write(json.dumps(line, ensure_ascii=False) + '\n')
    return path


def _find_cli() -> str:
    explicit = os.environ.get(CLI_ENV, '').strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else ''
    return shutil.which('sqlite3') or ''


def _verify(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {'ok': False, 'kind': 'missing', 'note': '文件不存在'}
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute('PRAGMA quick_check(1)').fetchone()
        finally:
            con.close()
        value = str(row[0]) if row else ''
        if value.lower() == 'ok':
            return {'ok': True, 'kind': 'quick_check', 'note': 'ok'}
        return {'ok': False, 'kind': 'quick_check', 'note': value[:512]}
    except Exception as e:
        return {'ok': False, 'kind': 'exception',
                'note': '%s: %s' % (type(e).__name__, e)}


def _fresh_path(workdir: str, tag: str) -> str:
    fd, path = tempfile.mkstemp(prefix='recover-%s-' % tag, suffix='.db',
                                dir=workdir)
    os.close(fd)
    os.remove(path)
    return path


def attempt(method: str, db_path: str, workdir: str) -> dict:
    """跑一种手段。成功时返回新库路径，失败时原库不受影响。"""
    result = {'method': method, 'ok': False, 'path': '', 'note': ''}
    if method == 'reindex':
        try:
            con = sqlite3.connect(db_path)
            try:
                con.execute('REINDEX')
                con.commit()
            finally:
                con.close()
            result.update(ok=True, path=db_path, note='REINDEX 完成')
        except Exception as e:
            result['note'] = '%s: %s' % (type(e).__name__, e)
        return result

    if method == 'vacuum_into':
        target = _fresh_path(workdir, 'vacuum')
        try:
            literal = target.replace("'", "''")
            con = sqlite3.connect(db_path)
            try:
                con.execute("VACUUM INTO '%s'" % literal)
            finally:
                con.close()
            result.update(ok=True, path=target, note='VACUUM INTO 完成')
        except Exception as e:
            result['note'] = '%s: %s' % (type(e).__name__, e)
            if os.path.exists(target):
                os.remove(target)
        return result

    if method == 'recover_cli':
        cli = _find_cli()
        if not cli:
            result['note'] = '未找到 sqlite3 命令行，设置 %s 指定路径' % CLI_ENV
            return result
        sql_path = os.path.join(workdir, 'recover-%s.sql' % int(time.time() * 1000))
        target = _fresh_path(workdir, 'cli')
        try:
            with open(sql_path, 'wb') as out:
                dumped = subprocess.run(
                    [cli, db_path, '.recover'], stdout=out,
                    stderr=subprocess.PIPE, timeout=CLI_TIMEOUT_SEC)
            if dumped.returncode != 0:
                raise RuntimeError('sqlite3 .recover 返回 %d: %s'
                                   % (dumped.returncode,
                                      dumped.stderr.decode('utf-8', 'ignore')[:512]))
            with open(sql_path, 'rb') as src:
                built = subprocess.run(
                    [cli, target], stdin=src, stderr=subprocess.PIPE,
                    timeout=CLI_TIMEOUT_SEC)
            if built.returncode != 0:
                raise RuntimeError('重建新库返回 %d: %s'
                                   % (built.returncode,
                                      built.stderr.decode('utf-8', 'ignore')[:512]))
            result.update(ok=True, path=target, note='.recover 导出并重建完成')
        except Exception as e:
            result['note'] = '%s: %s' % (type(e).__name__, e)
            if os.path.exists(target):
                os.remove(target)
        finally:
            try:
                if os.path.exists(sql_path):
                    os.remove(sql_path)
            except OSError:
                pass
        return result

    result['note'] = '未知手段: %s' % method
    return result


def _combinations():
    return [(m,) for m in METHODS] + list(PAIRS) + list(TRIPLES)


class _SnapshotTarget:
    """只给标准备份管线提供它需要的两个属性，不打开任何新连接。"""

    db_type = 'sqlite'

    def __init__(self, path):
        self._db_path = path


def _backup_config() -> dict:
    """取用户备份配置；应急快照强制开，但所有安全阈值照旧生效。"""
    raw = {}
    try:
        raw = (((get_config() or {}).get('database', {}) or {}).get('backup', {})
               or {})
    except Exception as e:
        logger.warning('读取备份配置失败，按默认值做恢复前快照: %s', e)
    cfg = dict(raw) if isinstance(raw, dict) else {}
    # 恢复前必须有快照，所以忽略 enabled；体积上限也不能挡住应急备份，
    # 但空间探测、.part 写盘、读回复核、轮转全部照走——那才是防炸盘的闸门。
    cfg['enabled'] = True
    cfg['allow_large'] = True
    return cfg


def _backup_before_repair(db_path: str, home=None, backup_fn=None) -> dict:
    """恢复前快照必须走标准备份流程，不能裸复制。

    裸 ``shutil.copy2`` 不探可用空间、不写 ``.part``、不读回校验，库多大就
    往盘上写多大；空间不足时把盘写满，比不备份更糟。这里统一交给
    ``backup.run_backup``：弹性空间探测、校验后转正、压缩、轮转一套都过。
    """
    if backup_fn is not None:
        # 测试注入点：保留旧协议，返回值归一成标准报告的形状。
        try:
            result = backup_fn(db_path)
        except Exception as e:
            return {'status': 'failed', 'ok': False, 'path': '',
                    'note': '%s: %s' % (type(e).__name__, e)}
        if isinstance(result, str):
            return {'status': 'ok', 'ok': True, 'path': result}
        if isinstance(result, dict):
            result.setdefault('ok', str(result.get('status', 'failed')) == 'ok')
            result.setdefault('path', '')
            return result
        return {'status': 'ok', 'ok': True, 'result': result, 'path': ''}

    try:
        report = dict(backup_mod.run_backup(
            _SnapshotTarget(db_path), _backup_config(), reason='recovery'))
    except Exception as e:
        return {'status': 'failed', 'ok': False, 'path': '',
                'note': '%s: %s' % (type(e).__name__, e)}
    report.setdefault('ok', str(report.get('status', 'failed')) == 'ok')
    report.setdefault('path', '')
    return report


def run_recovery(db_path: str, home=None, workdir=None,
                 backup_fn=None, log_dir=None, methods=None) -> dict:
    """备份 -> 按组合尝试 -> 校验 -> 转正 -> 写明细日志。不抛异常、不退出。

    退出与崩溃计数交给调用方的 ``perform_exit``：那样启动期与运行时（消息
    处理中撞到损坏）走的是同一条链，都会写一份 error.log 让原生守卫计数。
    """
    started = time.time()
    db_path = os.path.abspath(db_path)
    report = {'status': 'failed', 'ok': False, 'db_path': db_path,
              'backup_path': '', 'attempts': [], 'note': '',
              'should_exit': True, 'data_may_be_damaged': True,
              'elapsed': 0.0}

    def finish(status, ok, note, **extra):
        report.update(status=status, ok=ok, note=note)
        report.update(extra)
        report['elapsed'] = round(time.time() - started, 3)
        report['env'] = environment_info(db_path)
        report['error_log'] = _append_error_log(report, home=home, log_dir=log_dir)
        return report

    if not os.path.isfile(db_path):
        return finish('failed', False, '库文件不存在: %s' % db_path)

    backup = _backup_before_repair(db_path, home=home, backup_fn=backup_fn)
    report['backup'] = backup
    if backup.get('path'):
        report['backup_path'] = backup['path']
    logger.error('数据库损坏，已开始应急恢复；备份结果: %s', backup)
    if not backup.get('ok'):
        # 没有可靠快照就不动库。否则恢复失败/误判时连回滚的余地都没有。
        return finish('failed', False,
                      '恢复前备份未完成，已停止恢复以免无备份改动库：%s'
                      % (backup.get('note') or '标准备份流程返回失败'))

    tmp_root = workdir or os.path.join(_state_dir(home), 'tmp')
    try:
        os.makedirs(tmp_root, exist_ok=True)
    except OSError as e:
        return finish('failed', False, '临时目录不可用: %s' % e)

    combos = _combinations() if methods is None else [
        tuple(methods)] if isinstance(methods, str) else [tuple(methods)]
    working = tmp_root
    try:
        with tempfile.TemporaryDirectory(prefix='zc-recover-', dir=working) as td:
            for combo in combos:
                current = os.path.join(td, 'current.db')
                shutil.copy2(db_path, current)
                steps = []
                for method in combo:
                    res = attempt(method, current, td)
                    steps.append(res)
                    if res.get('ok') and os.path.isfile(res.get('path', '')):
                        current = res['path']
                        check = _verify(current)
                        if check.get('ok'):
                            report['attempts'].append(
                                {'combo': '+'.join(combo), 'steps': steps,
                                 'verify': check})
                            # 坏库留档不删除，成功产物原子替换到原位置
                            corrupt = db_path + '.corrupt-' + datetime.now(
                                timezone.utc).strftime('%Y%m%d%H%M%S')
                            try:
                                shutil.copy2(db_path, corrupt)
                                os.replace(current, db_path)
                            except OSError as e:
                                return finish('failed', False,
                                              '恢复成功但替换原库失败: %s' % e,
                                              attempts=report['attempts'])
                            return finish('ok', True,
                                          '组合 %s 恢复成功（%s）。%s'
                                          % ('+'.join(combo), check['note'],
                                             DATA_WARNING),
                                          attempts=report['attempts'],
                                          corrupt_path=corrupt)
                report['attempts'].append({'combo': '+'.join(combo),
                                           'steps': steps,
                                           'verify': _verify(current)})
            return finish('failed', False,
                          '全部组合均未恢复成功。已保留原始库与备份。%s' % DATA_WARNING,
                          attempts=report['attempts'])
    except Exception as e:
        return finish('failed', False, '恢复流程异常（%s: %s）' % (type(e).__name__, e),
                      attempts=report['attempts'])


def perform_exit(report: dict = None, home=None, exit_fn=None, log_dir=None):
    """写一份崩溃日志（计入原生守卫的计数），然后退出进程。

    这就是"故意让框架崩溃"的落点：先写 error.log —— 它同时是崩溃计数源、
    给人看的处置指引、和触发安全模式的依据 —— 落盘后再拉闸。守护脚本把框架
    拉起时，启动入口的 ``should_abort_boot`` 会数这些 error.log；没到上限就
    正常启动（再撞损坏再写再崩），到了上限直接进安全模式。全程零新概念。

    日志写失败也不能跳过退出：留一个坏库在跑比进程退出更危险。
    """
    report = report or {}
    try:
        path = write_crash_report(report, home=home, log_dir=log_dir)
        report['crash_log'] = path
        logger.error('数据库应急恢复流程结束，框架退出（%s）。崩溃记录: %s',
                     report.get('status'), path)
    except Exception as e:
        logger.error('写崩溃记录失败（%s: %s）；仍按计划退出以免带病运行',
                     type(e).__name__, e)
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass
    fn = exit_fn or (lambda code: os._exit(code))
    return fn(EXIT_CODE)
