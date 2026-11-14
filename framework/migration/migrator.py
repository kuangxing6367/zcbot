"""状态目录迁移执行器（幂等 / 可重入 / 绝不覆盖 / 绝不改写配置文件）。

设计对齐 Flyway 的公认做法（``flyway_schema_history`` 账本、
``baselineOnMigrate``、开机自动迁移、已应用版本跳过、并发安全），但迁移对象
是**磁盘布局**而不是数据库 schema：

- 账本 ``data/.zcbot-state-migration.json`` 记录本次内核版本、探测到的布局
  代际、候选版本区间、逐项动作与校验和；损坏的账本不可信，会重新探测；
- 已迁移过（账本 ``applied`` 且无待办动作）直接跳过，重复启动零写盘；
- 只搬三项状态资产：``config.yaml`` / ``core_plugins.yaml`` / ``plugins/``。
  **绝不触碰** ``framework/``、``core_plugins/``、``tools/`` 等代码资产，
  也绝不改写被搬文件的任何字节（用 ``shutil.move``，不是重写）；
- 目标已存在就跳过，不覆盖；新旧两代并存（mixed）时默认不自动搬，需人工确认；
- 单文件锁 ``data/.zcbot-state-migration.lock`` 防多进程同时迁移，陈旧锁超时回收；
- 中途失败写 ``partial`` 账本，下次启动可重入：已落盘的动作按账本判为已完成，
  未完成的继续，不会重复搬或覆盖。

**失败不阻断启动**：旧根目录布局仍由 ``framework.paths`` 兼容回退，
``ensure_auto_migrated()`` 吞掉一切异常并返回结构化结果。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone

from framework import paths

from . import detector, policy, version

__all__ = [
    'ensure_auto_migrated',
    'inspect',
    'ledger_path',
    'migrate',
    'plan',
    'read_ledger',
]

logger = logging.getLogger('zcbot.migration')

LEDGER_SCHEMA = 1
LOCK_STALE_SECONDS = 120
_ITEMS = (
    ('config', paths.CONFIG_NAME, 'file'),
    ('core_plugins_yaml', paths.CORE_YAML_NAME, 'file'),
    ('plugins', 'plugins', 'dir'),
)


def ledger_path(home=None) -> str:
    home = os.path.abspath(home) if home else paths.home_dir()
    return os.path.join(home, 'data', paths.MIGRATION_STATE_NAME)


def _lock_path(home: str) -> str:
    return os.path.join(home, 'data', paths.MIGRATION_LOCK_NAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _checksum(data: dict) -> str:
    body = {k: v for k, v in data.items() if k != 'checksum'}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def read_ledger(home=None):
    """返回 ``(账本 dict|None, 说明)``；校验和不符视为损坏，绝不据此跳过迁移。"""
    path = ledger_path(home)
    if not os.path.isfile(path):
        return None, '账本不存在'
    try:
        with open(path, 'rb') as f:
            raw = f.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return None, '账本过大，视为损坏'
        data = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data, dict):
            return None, '账本不是对象，视为损坏'
        if data.get('schema') != LEDGER_SCHEMA:
            return None, '账本 schema 不匹配，视为损坏'
        if data.get('checksum') != _checksum(data):
            return None, '账本校验和不符，视为损坏'
        return data, 'ok'
    except Exception as e:  # 损坏账本不能被当成「已迁移」
        logger.debug('[read_ledger] 捕获异常后按降级路径继续: %s', e)
        return None, '账本不可解析 (%s)，视为损坏' % type(e).__name__


def _is_empty_dir(path: str) -> bool:
    """目录存在且为空（空壳目录没有可搬内容）"""
    try:
        return os.path.isdir(path) and not os.listdir(path)
    except OSError:
        return False


def _outside(path: str, home: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path),
                                   os.path.realpath(home)]) != os.path.realpath(home)
    except (ValueError, OSError):
        return True


def _build_plan(home: str, report: dict, ledger: dict | None) -> list:
    state = report['state_dir']
    done = set()
    if ledger:
        done = {a.get('dst') for a in (ledger.get('actions') or [])
                if a.get('ok') and a.get('dst')}
    actions = []
    for key, name, kind in _ITEMS:
        src = os.path.join(home, name)
        dst = os.path.join(state, name)
        if not os.path.exists(src):
            actions.append({'key': key, 'kind': 'skip-missing', 'src': src, 'dst': dst})
            continue
        if kind == 'file' and not os.path.isfile(src):
            actions.append({'key': key, 'kind': 'wrong-type', 'src': src, 'dst': dst})
            continue
        if kind == 'dir' and not os.path.isdir(src):
            actions.append({'key': key, 'kind': 'wrong-type', 'src': src, 'dst': dst})
            continue
        if os.path.islink(src) and _outside(src, home):
            actions.append({'key': key, 'kind': 'unsafe-link', 'src': src, 'dst': dst})
            continue
        if kind == 'dir' and _is_empty_dir(src):
            # 空壳目录：没有内容可搬，但 move 到已存在的 dst 会被判 conflict，
            # 迁移就永久停在「需人工处理」。目录里确实没东西，跳过即可。
            actions.append({'key': key, 'kind': 'skip-empty', 'src': src, 'dst': dst})
            continue
        if os.path.exists(dst):
            if dst in done:
                actions.append({'key': key, 'kind': 'already-done', 'src': src, 'dst': dst})
            else:
                actions.append({'key': key, 'kind': 'conflict', 'src': src, 'dst': dst})
            continue
        actions.append({'key': key, 'kind': 'move', 'src': src, 'dst': dst})
    return actions


def plan(home=None) -> list:
    """只列出将执行的动作，不写任何文件。"""
    home = os.path.abspath(home) if home else paths.home_dir()
    report = detector.probe(home)
    ledger, _ = read_ledger(home)
    return _build_plan(home, report, ledger)


def inspect(home=None, config_path=None) -> dict:
    """只读体检：当前内核版本 + 目录布局代际 + 开关判定 + 账本 + 待办动作。"""
    home = os.path.abspath(home) if home else paths.home_dir()
    report = detector.probe(home)
    ledger, ledger_note = read_ledger(home)
    decision = policy.auto_migrate_enabled(config_path)
    actions = _build_plan(home, report, ledger)
    pending = [a for a in actions if a['kind'] == 'move']
    return {
        'kernel_version': version.current_version().as_dict(),
        'version_sources': version.version_sources(),
        'layout': report,
        'policy': decision.as_dict(),
        'ledger': ledger,
        'ledger_note': ledger_note,
        'actions': actions,
        'pending_count': len(pending),
        'would_migrate': bool(pending) and decision.enabled,
    }


def _acquire_lock(home: str):
    """``O_CREAT|O_EXCL`` 单文件锁；返回锁路径或 None（他人正在迁移）。"""
    data_dir = os.path.join(home, 'data')
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        return None
    path = _lock_path(home)
    try:
        if time.time() - os.stat(path).st_mtime > LOCK_STALE_SECONDS:
            os.unlink(path)          # 陈旧锁：上次进程崩溃遗留
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps({'pid': os.getpid(), 'ts': time.time()}).encode())
        finally:
            os.close(fd)
        return path
    except FileExistsError:
        return None
    except OSError:
        return None


def _write_ledger(home: str, record: dict) -> str:
    record['checksum'] = _checksum(record)
    path = ledger_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)           # 原子落盘，避免半截账本
    return path


def _base_record(home: str, report: dict, decision: policy.Decision, status: str) -> dict:
    return {
        'schema': LEDGER_SCHEMA,
        'status': status,
        'kernel_version': version.current_version_string(),
        'from_layout': report['layout'],
        'to_layout': detector.LAYOUT_DATA,
        'detected_candidates': report['detected_candidates'],
        'confidence': report['confidence'],
        'applied_at': _now(),
        'home': home,
        'policy': {'auto_migrate': decision.enabled, 'source': decision.source,
                   'reason': decision.reason},
        'actions': [],
    }


def migrate(home=None, dry_run=False, force=False, config_path=None) -> dict:
    """执行一次迁移。``dry_run`` 不写任何文件；``force`` 只忽略 mixed 告警，仍不覆盖。"""
    home = os.path.abspath(home) if home else paths.home_dir()
    decision = policy.auto_migrate_enabled(config_path)
    report = detector.probe(home)
    ledger, ledger_note = read_ledger(home)
    actions = _build_plan(home, report, ledger)
    pending = [a for a in actions if a['kind'] == 'move']
    conflicts = [a for a in actions if a['kind'] in ('conflict', 'unsafe-link', 'wrong-type')]
    result = {
        'status': '', 'home': home, 'layout': report['layout'],
        'kernel_version': version.current_version_string(),
        'policy': decision.as_dict(), 'ledger_note': ledger_note,
        'detected_candidates': report['detected_candidates'],
        'actions': actions, 'dry_run': dry_run, 'fatal': False,
    }

    if not os.path.isdir(home):
        result['status'] = 'nothing-to-do'
        result['detail'] = 'home 目录不存在（首次安装由启动器创建），跳过迁移'
        return result
    if report['layout'] == detector.LAYOUT_MIXED and not force:
        result['status'] = 'manual-required'
        result['detail'] = '新旧两处状态文件并存，自动迁移不会覆盖 data/ 下已有文件；'
        result['detail'] += '请先人工确认哪一份为准，或用 --force 只搬不冲突项'
        result['conflicts'] = conflicts
        return result
    if ledger and ledger.get('status') == 'applied' and not pending and not conflicts:
        result['status'] = 'already-migrated'
        result['detail'] = '账本记录已完成，重复启动零写盘'
        return result
    if ledger and ledger.get('status') == 'baseline' and not pending and not conflicts:
        result['status'] = 'already-baseline'
        result['detail'] = '目录已是 data/ 布局并已建基线，无需迁移'
        return result
    if not pending and not conflicts:
        result['status'] = 'nothing-to-do'
        result['detail'] = '无待迁移状态文件（全新 home 或已是新布局）'
        if report['layout'] == detector.LAYOUT_DATA and not dry_run:
            record = _base_record(home, report, decision, 'baseline')
            try:
                _write_ledger(home, record)   # 类比 Flyway baselineOnMigrate
                result['detail'] += '；已写入基线账本'
            except Exception as e:
                logger.debug('[migrate] 捕获异常后按降级路径继续: %s', e)
                result['detail'] += '；基线账本写入失败 (%s)' % type(e).__name__
        return result
    if dry_run:
        result['status'] = 'dry-run'
        result['detail'] = '仅预演，未写入任何文件；待搬 %d 项，冲突/跳过 %d 项' % (
            len(pending), len(conflicts))
        return result

    lock = _acquire_lock(home)
    if lock is None:
        result['status'] = 'locked-skip'
        result['detail'] = '另一个进程正在迁移或锁不可创建；旧布局仍可兼容运行'
        return result

    record = _base_record(home, report, decision, 'partial')
    try:
        for action in actions:
            if action['kind'] != 'move':
                record['actions'].append({**action, 'ok': False, 'skipped': True})
                continue
            try:
                os.makedirs(os.path.dirname(action['dst']), exist_ok=True)
                shutil.move(action['src'], action['dst'])
                if not os.path.exists(action['dst']) or os.path.exists(action['src']):
                    raise RuntimeError('未落盘或源未移走')
                record['actions'].append({**action, 'ok': True})
            except Exception as e:
                record['actions'].append({**action, 'ok': False,
                                          'error': '%s: %s' % (type(e).__name__, e)})
                record['status'] = 'partial'
                _write_ledger(home, record)
                result['status'] = 'partial'
                result['detail'] = '中途失败，已写 partial 账本，下次启动可重入'
                result['actions'] = record['actions']
                return result
        # 有未解决的 conflict 时不能宣称全部完成
        unresolved = [a for a in actions if a['kind'] in ('conflict', 'unsafe-link', 'wrong-type')]
        record['status'] = 'applied' if not unresolved else 'partial'
        _write_ledger(home, record)
        result['status'] = 'applied' if not unresolved else 'partial'
        result['detail'] = ('状态已统一到 data/，配置文件字节未改写'
                            if not unresolved else '部分项需人工处理，详见账本')
        result['actions'] = record['actions']
        result['ledger_path'] = ledger_path(home)
        return result
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def ensure_auto_migrated(config_path=None) -> dict:
    """启动链路钩子：按策略决定是否自动迁移；**任何异常都不阻断启动**。"""
    try:
        decision = policy.auto_migrate_enabled(config_path)
        if not decision.enabled:
            return {'status': 'disabled', 'policy': decision.as_dict(),
                    'detail': '配置已关闭自动迁移，保持当前布局（旧布局仍兼容）'}
        result = migrate(config_path=config_path)
        result['policy'] = decision.as_dict()
        return result
    except Exception as e:  # 迁移是增强能力，失败必须退回兼容路径
        logger.warning('[migration] 自动迁移失败，回退到旧布局兼容运行: %s: %s',
                       type(e).__name__, e)
        return {'status': 'error', 'fatal': False,
                'error': '%s: %s' % (type(e).__name__, e),
                'detail': '迁移失败不阻断启动，framework.paths 仍兼容旧布局'}
