"""数据库损坏应急恢复：标准备份、组合顺序、人话日志与崩溃计数联动。

这里刻意不依赖真实损坏的库文件——那类样本不好稳定构造，而且测的是 sqlite
而不是我们自己的流程。用受控替身把每一次尝试的结果钉住，测的是编排：
备份是不是走了标准管线、组合顺序对不对、退出是否落进原生崩溃守卫。
"""
import json
import os
import sqlite3

import pytest

from framework import crash_guard
from framework.database import backup as backup_mod
from framework.database import db as db_mod
from framework.database import recovery as rc


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """恢复流程读配置时只拿默认值，避免本机 config.yaml 干扰断言。"""
    monkeypatch.setattr(rc, 'get_config', lambda: {})


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / 'home'
    (h / 'data').mkdir(parents=True)
    return str(h)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / 'zcbot.db'
    con = sqlite3.connect(str(path))
    try:
        con.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
        con.execute("INSERT INTO t (v) VALUES ('alive')")
        con.commit()
    finally:
        con.close()
    return str(path)


@pytest.fixture()
def crash_dir(tmp_path):
    d = tmp_path / 'crash'
    d.mkdir()
    return str(d)


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT id, v FROM t ORDER BY id').fetchall()
    finally:
        con.close()


def _recovery_logs(home):
    path = os.path.join(home, 'data', 'logs', 'error.log')
    if not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def _crash_config(crash_dir, max_restarts=5, enabled=True):
    return {'framework': {'crash': {
        'enabled': enabled,
        'max_restarts': max_restarts,
        'path': os.path.join(crash_dir, 'error.log'),
    }}}


def _crash_text(crash_dir):
    path = os.path.join(crash_dir, 'error.log')
    if not os.path.isfile(path):
        return ''
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── 损坏识别：不能把别的错误当成损坏 ─────────────────────────

def test_corruption_detection():
    class E(Exception):
        sqlite_errorname = 'SQLITE_CORRUPT'

    class Locked(Exception):
        sqlite_errorname = 'SQLITE_BUSY'

    assert rc.is_corruption(E('boom')) is True
    assert rc.is_corruption(sqlite3.DatabaseError('database disk image is malformed'))
    assert rc.is_corruption(sqlite3.DatabaseError('file is not a database'))
    assert rc.is_corruption(Locked('database is locked')) is False
    assert rc.is_corruption(sqlite3.OperationalError('no such table: t')) is False
    assert rc.is_corruption(ValueError('disk full')) is False


# ── 组合顺序：纸上写死了 ──────────────────────────────────

def test_combination_order_matches_handwriting():
    combos = rc._combinations()
    assert combos[:3] == [('reindex',), ('vacuum_into',), ('recover_cli',)]
    assert combos[3:6] == [('reindex', 'vacuum_into'),
                          ('reindex', 'recover_cli'),
                          ('vacuum_into', 'recover_cli')]
    assert combos[6:] == list(rc.TRIPLES)
    assert len(combos) == 12
    assert len(set(combos)) == 12


# ── 单种手段 ─────────────────────────────────────────────

def test_reindex_and_vacuum_on_healthy_db(db, tmp_path):
    r = rc.attempt('reindex', db, str(tmp_path))
    assert r['ok'] is True
    assert r['path'] == db

    v = rc.attempt('vacuum_into', db, str(tmp_path))
    assert v['ok'] is True
    assert v['path'] != db
    assert rc._verify(v['path'])['ok'] is True
    os.remove(v['path'])


def test_reindex_on_junk_does_not_pretend_success(tmp_path):
    junk = tmp_path / 'junk.db'
    junk.write_bytes(b'not a database at all')
    r = rc.attempt('reindex', str(junk), str(tmp_path))
    assert r['ok'] is False
    assert r['note']


def test_recover_cli_absent_is_reported_not_crashed(db, tmp_path, monkeypatch):
    monkeypatch.setattr(rc, '_find_cli', lambda: '')
    r = rc.attempt('recover_cli', db, str(tmp_path))
    assert r['ok'] is False
    assert rc.CLI_ENV in r['note']


# ── 恢复前备份：必须走标准管线 ─────────────────────────────

def test_backup_before_repair_uses_standard_pipeline(db, monkeypatch):
    seen = []

    def fake_run_backup(target, cfg=None, reason='', out_dir=None):
        seen.append((target._db_path, dict(cfg or {}), reason))
        return {'ok': True, 'status': 'ok', 'path': '/tmp/snapshot.db',
                'size': 123}

    monkeypatch.setattr(backup_mod, 'run_backup', fake_run_backup)
    monkeypatch.setattr(rc, 'get_config', lambda: {
        'database': {'backup': {'keep': 7, 'probe_chunk_kb': 256,
                                'refuse_above_mb': 10}}})

    result = rc._backup_before_repair(db)
    assert result['ok'] is True
    assert result['path'] == '/tmp/snapshot.db'
    assert len(seen) == 1
    path, cfg, reason = seen[0]
    assert path == db
    assert reason == 'recovery'
    # 强制开快照、放开体积上限，但空间探测 / 校验 / 轮转这些闸门照走。
    assert cfg['enabled'] is True
    assert cfg['allow_large'] is True
    assert cfg['keep'] == 7
    assert cfg['probe_chunk_kb'] == 256
    assert cfg['refuse_above_mb'] == 10


def test_recovery_stops_when_backup_fails_and_never_touches_db(
        home, db, monkeypatch):
    def fake_run_backup(target, cfg=None, reason='', out_dir=None):
        return {'ok': False, 'status': 'failed', 'path': '',
                'note': '空间不足，未产生备份'}

    monkeypatch.setattr(backup_mod, 'run_backup', fake_run_backup)
    monkeypatch.setattr(rc, 'attempt',
                        lambda *a: pytest.fail('没有可靠快照时不该动库'))

    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'failed'
    assert report['ok'] is False
    assert '空间不足' in report['note']
    assert report['attempts'] == []
    assert _rows(db) == [(1, 'alive')]


# ── 编排：备份 → 组合 → 校验 → 转正 → 明细日志 ───────────────

def test_recovery_success_uses_real_standard_backup(home, db, monkeypatch):
    seen = []

    def fake_attempt(method, path, workdir):
        seen.append(method)
        return {'ok': True, 'path': path, 'note': method}

    monkeypatch.setattr(rc, 'attempt', fake_attempt)
    report = rc.run_recovery(db, home=home)

    assert report['status'] == 'ok'
    assert report['ok'] is True
    assert report['should_exit'] is True
    assert seen == ['reindex']
    assert _rows(db) == [(1, 'alive')]
    assert os.path.isfile(report['backup_path'])
    assert '-recovery.' in os.path.basename(report['backup_path'])
    assert any('.corrupt-' in str(v) for v in report.values()
               if isinstance(v, str))
    logs = _recovery_logs(home)
    assert logs and '数据有可能已损坏' in logs[-1]['warning']


def test_recovery_falls_through_to_next_combo(home, db, monkeypatch):
    calls = []

    def fake_attempt(method, path, workdir):
        calls.append(method)
        if len(calls) == 1:
            return {'ok': False, 'path': '', 'note': '故意失败'}
        return {'ok': True, 'path': path, 'note': '好了'}

    monkeypatch.setattr(rc, 'attempt', fake_attempt)
    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'ok'
    assert calls[:2] == ['reindex', 'vacuum_into']
    assert report['attempts'][0]['combo'] == 'reindex'
    assert report['attempts'][1]['combo'] == 'vacuum_into'
    assert _rows(db) == [(1, 'alive')]


def test_pairs_run_only_after_all_singles(home, db, monkeypatch):
    calls = []

    def fake_attempt(method, path, workdir):
        calls.append(method)
        ok = len(calls) == 5
        return {'ok': ok, 'path': path if ok else '', 'note': 'x'}

    monkeypatch.setattr(rc, 'attempt', fake_attempt)
    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'ok'
    assert calls == ['reindex', 'vacuum_into', 'recover_cli',
                     'reindex', 'vacuum_into']
    assert report['attempts'][3]['combo'] == 'reindex+vacuum_into'


def test_all_combos_fail_still_writes_log(home, db, monkeypatch):
    monkeypatch.setattr(rc, 'attempt',
                        lambda m, p, w: {'ok': False, 'path': '', 'note': '失败'})
    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'failed'
    assert report['ok'] is False
    assert len(report['attempts']) == len(rc._combinations())
    assert _rows(db) == [(1, 'alive')]
    assert _recovery_logs(home)


# ── 退出：写人话 error.log，落进原生崩溃计数 ────────────────

def test_crash_report_is_human_readable(crash_dir):
    path = rc.write_crash_report(
        {'status': 'ok', 'ok': True, 'backup_path': '/backups/snapshot.db',
         'note': 'REINDEX 完成',
         'env': {'sqlite': '3.45.0'}},
        log_dir=crash_dir)
    assert path == os.path.join(crash_dir, 'error.log')
    text = _crash_text(crash_dir)
    assert '数据库损坏' in text
    assert '请人工核对' in text
    assert '/backups/snapshot.db' in text
    assert '数据有可能已损坏' in text
    assert '[数据库应急恢复]' in text
    assert 'NoneType: None' not in text
    assert 'context: {' not in text


def test_perform_exit_writes_crash_log_and_uses_standard_code(
        crash_dir, monkeypatch):
    codes = []
    report = {'status': 'ok', 'ok': True, 'backup_path': '/b'}
    rc.perform_exit(report, log_dir=crash_dir,
                    exit_fn=lambda code: codes.append(code))
    assert codes == [rc.EXIT_CODE]
    assert rc.EXIT_CODE == 1  # 普通非零退出码，交给原生崩溃守卫计数
    assert os.path.isfile(os.path.join(crash_dir, 'error.log'))
    assert report['crash_log'] == os.path.join(crash_dir, 'error.log')


def test_recovery_crashes_accumulate_until_native_guard_aborts(crash_dir):
    cfg = _crash_config(crash_dir, max_restarts=3)
    for i in range(3):
        assert crash_guard.count_crash_markers(cfg, crash_dir) == i
        assert crash_guard.should_abort_boot(cfg, crash_dir) is False
        rc.perform_exit({'status': 'ok', 'ok': True}, log_dir=crash_dir,
                        exit_fn=lambda code: None)
    assert crash_guard.count_crash_markers(cfg, crash_dir) == 3
    # 守护脚本再拉起时，启动入口读到的就是这个结果：安全模式，不装插件不写库。
    assert crash_guard.should_abort_boot(cfg, crash_dir) is True


def test_disabled_crash_guard_never_aborts_after_recovery(crash_dir):
    cfg = _crash_config(crash_dir, max_restarts=1, enabled=False)
    rc.perform_exit({'status': 'failed', 'ok': False}, log_dir=crash_dir,
                    exit_fn=lambda code: None)
    assert crash_guard.count_crash_markers(cfg, crash_dir) == 1
    assert crash_guard.should_abort_boot(cfg, crash_dir) is False


def test_crash_log_write_failure_still_exits(crash_dir, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError('只读文件系统')

    monkeypatch.setattr(rc, 'write_crash_report', broken)
    codes = []
    rc.perform_exit({'status': 'failed'}, log_dir=crash_dir,
                    exit_fn=lambda code: codes.append(code))
    assert codes == [rc.EXIT_CODE]


# ── 运行时 SQL 错误也要能触发，不只限启动建表 ────────────────────

def test_emergency_recover_ignores_non_corruption(monkeypatch):
    calls = []
    monkeypatch.setattr(rc, 'run_recovery',
                        lambda p: calls.append(('run', p)) or {'status': 'ok'})
    monkeypatch.setattr(rc, 'perform_exit',
                        lambda report: calls.append(('exit', report)))

    assert db_mod._emergency_recover(
        sqlite3.OperationalError('no such table: t'), 'x.db') is False
    assert calls == []


def test_emergency_recover_resolves_relative_path_and_exits(monkeypatch):
    calls = []
    monkeypatch.setattr(rc, 'run_recovery',
                        lambda p: calls.append(('run', p)) or {'status': 'ok'})
    monkeypatch.setattr(rc, 'perform_exit',
                        lambda report: calls.append(('exit', report)))

    err = sqlite3.DatabaseError('database disk image is malformed')
    assert db_mod._emergency_recover(err, 'data/zcbot.db', source='query') is True
    assert calls[0][0] == 'run'
    assert os.path.isabs(calls[0][1])
    assert os.path.normpath(calls[0][1]).endswith(
        os.path.join('data', 'zcbot.db'))
    assert calls[1][0] == 'exit'
    assert calls[1][1]['status'] == 'ok'


@pytest.mark.parametrize('method,source', [('query', 'query'),
                                           ('query_one', 'query_one'),
                                           ('execute', 'write')])
def test_runtime_corruption_enters_recovery(db, monkeypatch, method, source):
    seen = []
    monkeypatch.setattr(
        db_mod, '_emergency_recover',
        lambda err, path, source='': seen.append(source) or True)
    database = db_mod.Database({'type': 'sqlite', 'path': db})

    def boom(*args, **kwargs):
        raise sqlite3.DatabaseError('database disk image is malformed')

    monkeypatch.setattr(database, '_run_with_reconnect', boom)
    with pytest.raises(sqlite3.DatabaseError):
        getattr(database, method)('SELECT 1')
    assert seen == [source]


# ── 环境信息 ─────────────────────────────────────────────

def test_environment_info_fields(db):
    info = rc.environment_info(db)
    for key in ('system', 'python', 'sqlite', 'in_lxc', 'in_docker',
                'in_venv', 'free_bytes', 'total_bytes', 'db_bytes'):
        assert key in info, key
    assert info['db_bytes'] == os.path.getsize(db)
    assert info['total_bytes'] >= info['free_bytes'] >= 0
    assert isinstance(info['in_venv'], bool)


def test_environment_info_records_memory_and_cgroup(db, monkeypatch, tmp_path):
    """手写稿要求：内存配置、可用内存、cgroup 上限都进 error.log。"""
    proc = tmp_path / 'meminfo'
    proc.write_text('MemTotal:       16384 kB\nMemAvailable:    8192 kB\n', encoding='utf-8')
    v2 = tmp_path / 'memory.max'
    v2.write_text('2147483648\n', encoding='utf-8')
    monkeypatch.setattr(rc, 'PROC_MEMINFO', str(proc))
    monkeypatch.setattr(rc, 'CGROUP_V2_MEM_MAX', str(v2))
    monkeypatch.setattr(rc, 'CGROUP_V1_MEM_LIMIT', str(tmp_path / 'missing'))
    monkeypatch.setattr(rc.platform, 'system', lambda: 'Linux')
    info = rc.environment_info(db)
    assert info['mem_total_bytes'] == 16384 * 1024
    assert info['mem_available_bytes'] == 8192 * 1024
    assert info['cgroup_mem_limit_bytes'] == 2147483648


def test_cgroup_v1_fallback_and_unlimited(tmp_path, monkeypatch):
    v2 = tmp_path / 'memory.max'
    v2.write_text('max\n', encoding='utf-8')
    v1 = tmp_path / 'memory.limit_in_bytes'
    v1.write_text('9223372036854771712\n', encoding='utf-8')  # cgroup v1 的"无限制"
    monkeypatch.setattr(rc, 'CGROUP_V2_MEM_MAX', str(v2))
    monkeypatch.setattr(rc, 'CGROUP_V1_MEM_LIMIT', str(v1))
    assert rc._cgroup_memory_limit() is None
    v1.write_text('1073741824\n', encoding='utf-8')
    assert rc._cgroup_memory_limit() == 1073741824


def test_memory_usage_unknown_platform(monkeypatch):
    monkeypatch.setattr(rc.platform, 'system', lambda: 'Plan9')
    assert rc._memory_usage() == (None, None)


def test_crash_log_carries_full_environment(crash_dir, monkeypatch, tmp_path):
    proc = tmp_path / 'meminfo'
    proc.write_text('MemTotal:       16384 kB\nMemAvailable:    8192 kB\n', encoding='utf-8')
    monkeypatch.setattr(rc, 'PROC_MEMINFO', str(proc))
    monkeypatch.setattr(rc.platform, 'system', lambda: 'Linux')
    path = rc.write_crash_report({'status': 'failed', 'ok': False}, log_dir=crash_dir)
    text = _crash_text(crash_dir)
    assert 'mem_total_bytes' in text
    assert 'total_bytes' in text
    assert os.path.basename(path) == 'error.log'


def test_module_has_no_emoji():
    for module in (rc, crash_guard):
        with open(module.__file__, encoding='utf-8') as f:
            text = f.read()
        assert not any(ord(ch) > 0xFFFF for ch in text)
