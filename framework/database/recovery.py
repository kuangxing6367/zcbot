"""SQLite 损坏后的应急恢复：先备份，再按组合尝试恢复，最后退出。

A/B/C 三套手段是 SQLite 官方支持的：
- A ``REINDEX`` —— 索引类损坏，纯标准库，代价最小；
- B ``VACUUM INTO`` —— 库还能读时重建整库，纯标准库；
- C ``.recover`` CLI —— 库读不动时打捞，需要外部 ``sqlite3`` 命令。

组合顺序按手写稿：单个 → 两两 → 六种三排列。每一种组合都在原库的副本上做，
前一步的产物接着给后一步用；任何一步校验通过就停下。

恢复结果**永远不能当成数据完整**：SQLite 官方文档明确写了打捞可能丢行、
复活已删除内容、改掉字段类型、破坏约束。所以每次都写 ``error.log``，并且
退出前留哨兵文件；下次启动读到哨兵就大声告警，不自动清除，等人确认。

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

from framework.paths import home_dir, state_dir

logger = logging.getLogger('zcbot')

# 最近一次启动的哨兵检查结果，供状态接口/健康检查读取；不参与业务逻辑。
STARTUP_GUARD = {'guarded': False, 'sentinel': None, 'path': ''}

SENTINEL_FILE = 'db-recovery.exit'
ERROR_LOG_NAME = 'error.log'
EXIT_CODE = 78
CLI_ENV = 'ZCBOT_SQLITE3_CLI'
CLI_TIMEOUT_SEC = 300

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


def sentinel_path(home=None) -> str:
    return os.path.join(_state_dir(home), SENTINEL_FILE)


def write_sentinel(report: dict, home=None) -> str:
    """写哨兵。下次启动靠它知道「这台库被自动恢复过」。"""
    path = sentinel_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        'written_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'status': report.get('status', 'unknown'),
        'ok': bool(report.get('ok')),
        'data_may_be_damaged': True,
        'db_path': report.get('db_path', ''),
        'backup_path': report.get('backup_path', ''),
        'attempts': report.get('attempts', []),
        'note': report.get('note', ''),
        'acknowledged': False,
    }
    with open(path, 'w', encoding='utf-8', newline='') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def read_sentinel(home=None):
    path = sentinel_path(home)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {'broken': True}
    except (OSError, ValueError) as e:
        logger.error('数据库恢复哨兵文件读不出来（%s）: %s', path, e)
        return {'broken': True, 'path': path}


def clear_sentinel(home=None) -> bool:
    """只给人工确认调用。框架自己永远不调。"""
    path = sentinel_path(home)
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError as e:
        logger.warning('删除恢复哨兵失败: %s', e)
    return False


def startup_guard(home=None, log_fn=None) -> dict:
    """启动时检查哨兵：读到就持续告警，清楚说明要人工确认。

    这一层存在的理由：外部守护脚本会在框架退出后自动把它拉起来。若框架
    对「刚被恢复过」一无所知，它会把一份可能已经损坏的库当正常库继续写，
    用户也不会收到任何提示。所以哨兵**不自动清**，由人确认后再删。
    """
    global STARTUP_GUARD
    sentinel = read_sentinel(home)
    if sentinel is None:
        STARTUP_GUARD = {'guarded': False, 'sentinel': None,
                         'path': sentinel_path(home)}
        return STARTUP_GUARD

    path = sentinel_path(home)
    status = sentinel.get('status', 'unknown')
    emit = log_fn or (lambda level, msg: getattr(logger, level)(msg))
    emit('error', '=' * 72)
    emit('error', '警告：本次启动的数据库是自动恢复过的（上次结果：%s）' % status)
    emit('error', DATA_WARNING)
    emit('error', '恢复记录：%s' % path)
    emit('error', '备份文件：%s' % (sentinel.get('backup_path') or '未记录'))
    emit('error', '确认数据无误后，请人工删除该文件再重启：%s' % path)
    emit('error', '=' * 72)
    # 同时落一份到 error.log，避免日志轮转后用户找不到。写日志失败不能反过来
    # 吞掉哨兵告警——那时更要让本轮控制台日志把风险说清楚。
    try:
        _append_error_log({'status': 'startup-guard', 'note': DATA_WARNING,
                           'sentinel': path, 'env': environment_info()},
                          home=home)
    except Exception as e:
        emit('error', '写 error.log 失败（%s: %s），以上告警请人工留存'
             % (type(e).__name__, e))
    STARTUP_GUARD = {'guarded': True, 'sentinel': sentinel, 'path': path}
    return STARTUP_GUARD


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


def _backup_before_repair(db_path: str, home=None, backup_fn=None) -> dict:
    """恢复前的快照。失败也要记下来，但绝不因为备份失败就跳过恢复。"""
    if backup_fn is not None:
        try:
            result = backup_fn(db_path)
            if isinstance(result, str):
                return {'ok': True, 'path': result}
            if isinstance(result, dict):
                return result
            return {'ok': True, 'result': result}
        except Exception as e:
            return {'ok': False, 'note': '%s: %s' % (type(e).__name__, e)}
    directory = os.path.join(_state_dir(home), 'backups')
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')[:-3]
        target = os.path.join(directory, 'zcbot-recovery-%s.db' % stamp)
        shutil.copy2(db_path, target)
        return {'ok': True, 'path': target}
    except Exception as e:
        return {'ok': False, 'note': '%s: %s' % (type(e).__name__, e)}


def run_recovery(db_path: str, home=None, workdir=None,
                 backup_fn=None, log_dir=None, methods=None) -> dict:
    """备份 -> 按组合尝试 -> 校验 -> 转正 -> 写日志与哨兵。不抛异常。"""
    started = time.time()
    db_path = os.path.abspath(db_path)
    report = {'status': 'failed', 'ok': False, 'db_path': db_path,
              'backup_path': '', 'attempts': [], 'note': '',
              'should_exit': True, 'data_may_be_damaged': True,
              'elapsed': 0.0}

    def finish(status, ok, note, keep_existing_sentinel=False, **extra):
        report.update(status=status, ok=ok, note=note)
        report.update(extra)
        report['elapsed'] = round(time.time() - started, 3)
        report['env'] = environment_info(db_path)
        report['error_log'] = _append_error_log(report, home=home, log_dir=log_dir)
        if keep_existing_sentinel:
            # 已有未确认的哨兵是原始证据，不能让第二次尝试把 attempts 覆盖掉。
            report['sentinel'] = sentinel_path(home)
        else:
            report['sentinel'] = write_sentinel(report, home=home)
        return report

    guard = read_sentinel(home)
    if guard is not None:
        return finish('guarded', False,
                      '已有恢复记录未确认，拒绝再次自动恢复。人工确认后删除 %s'
                      % sentinel_path(home), keep_existing_sentinel=True)

    if not os.path.isfile(db_path):
        return finish('failed', False, '库文件不存在: %s' % db_path)

    backup = _backup_before_repair(db_path, home=home, backup_fn=backup_fn)
    report['backup'] = backup
    if backup.get('path'):
        report['backup_path'] = backup['path']
    logger.error('数据库损坏，已开始应急恢复；备份结果: %s', backup)

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


def perform_exit(report: dict = None, home=None, exit_fn=None):
    """备份/恢复后默认退出。日志先落盘，再拉闸。

    退出前哨兵必须已经写好——外部守护脚本会立刻把框架拉起来，下一轮启动时
    全靠它告知「这份库刚被恢复过、数据可能有问题」。
    """
    if report is not None and not report.get('sentinel'):
        write_sentinel(report, home=home)
    path = report.get('sentinel') if report else sentinel_path(home)
    logger.error('数据库应急恢复流程结束，框架退出（%s）。确认前请勿让守护脚本'
                 '反复拉起；人工确认后删除: %s',
                 (report or {}).get('status'), path)
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass
    fn = exit_fn or (lambda code: os._exit(code))
    return fn(EXIT_CODE)
