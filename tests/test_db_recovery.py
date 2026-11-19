"""数据库损坏应急恢复：哨兵、组合顺序、日志与退出行为。

这里刻意不依赖真实损坏的库文件——那类样本不好稳定构造，而且测的是 sqlite
而不是我们自己的流程。用受控替身把 every attempt 的结果钉住，测的是编排：
备份是否先做、组合顺序、成功/失败时的产物、哨兵是否被误清。
"""
import json
import os
import shutil
import sqlite3

import pytest

from framework.database import db as db_mod
from framework.database import recovery as rc


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


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT id, v FROM t ORDER BY id').fetchall()
    finally:
        con.close()


def _log_lines(home):
    path = os.path.join(home, 'data', 'logs', 'error.log')
    if not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


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


# ── 哨兵：框架自己绝不清除 ─────────────────────────────────

def test_sentinel_roundtrip_and_manual_clear(home):
    assert rc.read_sentinel(home) is None
    path = rc.write_sentinel({'status': 'ok', 'backup_path': '/b', 'attempts': [1]},
                             home=home)
    assert os.path.isfile(path)
    got = rc.read_sentinel(home)
    assert got['status'] == 'ok'
    assert got['data_may_be_damaged'] is True
    assert got['acknowledged'] is False
    assert rc.clear_sentinel(home) is True
    assert rc.read_sentinel(home) is None


def test_startup_guard_warns_and_does_not_clear(home, caplog):
    path = rc.write_sentinel({'status': 'ok', 'note': 'n'}, home=home)
    with caplog.at_level('ERROR', logger='zcbot'):
        guard = rc.startup_guard(home=home)
    assert guard['guarded'] is True
    assert rc.STARTUP_GUARD['guarded'] is True
    assert os.path.isfile(path)  # 关键：不能自动清
    text = caplog.text
    assert '自动恢复过' in text
    assert '数据有可能已损坏' in text
    assert '人工删除' in text
    # 告警同时落 error.log，日志轮转后也能查
    lines = _log_lines(home)
    assert lines and lines[-1]['status'] == 'startup-guard'


def test_startup_guard_clean_start(home):
    guard = rc.startup_guard(home=home)
    assert guard['guarded'] is False
    assert rc.read_sentinel(home) is None


def test_broken_sentinel_is_not_silently_ignored(home):
    with open(rc.sentinel_path(home), 'w', encoding='utf-8') as f:
        f.write('{not json')
    guard = rc.startup_guard(home=home)
    assert guard['guarded'] is True
    assert guard['sentinel'].get('broken') is True


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


# ── 编排：备份 → 组合 → 转正 → 哨兵 ────────────────────────

def test_recovery_success_checks_every_step_and_leaves_sentinel(home, db, monkeypatch):
    seen = []

    def fake_attempt(method, path, workdir):
        seen.append(method)
        return {'ok': True, 'path': path, 'note': method}

    monkeypatch.setattr(rc, 'attempt', fake_attempt)
    backup = {'called': False}

    def fake_backup(path):
        backup['called'] = True
        target = path + '.bak'
        shutil.copy2(path, target)
        return target

    report = rc.run_recovery(db, home=home, backup_fn=fake_backup)
    assert backup['called'] is True  # 恢复之前必须先备份
    assert report['status'] == 'ok'
    assert report['ok'] is True
    assert report['should_exit'] is True
    assert seen == ['reindex']  # 第一个组合就通过，不该继续试别的
    assert _rows(db) == [(1, 'alive')]  # 数据还在
    assert os.path.isfile(report['backup_path'])
    assert os.path.isfile(report['sentinel'])
    assert any('.corrupt-' in str(v) for v in report.values() if isinstance(v, str))
    log = _log_lines(home)
    assert log and '数据有可能已损坏' in log[-1]['warning']


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
    # 单个手段先全试完，才轮到两两组合——这是纸上的顺序
    assert report['attempts'][1]['combo'] == 'vacuum_into'
    assert _rows(db) == [(1, 'alive')]


def test_pairs_run_only_after_all_singles(home, db, monkeypatch):
    calls = []

    def fake_attempt(method, path, workdir):
        calls.append(method)
        # 三个单打全失败后，第四个调用是第一个两两组合的 A
        ok = len(calls) == 5
        return {'ok': ok, 'path': path if ok else '', 'note': 'x'}

    monkeypatch.setattr(rc, 'attempt', fake_attempt)
    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'ok'
    assert calls == ['reindex', 'vacuum_into', 'recover_cli',
                     'reindex', 'vacuum_into']
    assert report['attempts'][3]['combo'] == 'reindex+vacuum_into'


def test_all_combos_fail_still_writes_log_and_sentinel(home, db, monkeypatch):
    monkeypatch.setattr(rc, 'attempt',
                        lambda m, p, w: {'ok': False, 'path': '', 'note': '失败'})
    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'failed'
    assert report['ok'] is False
    assert len(report['attempts']) == len(rc._combinations())
    assert _rows(db) == [(1, 'alive')]  # 原库没被坏产物覆盖
    assert os.path.isfile(report['sentinel'])
    assert _log_lines(home)


def test_existing_sentinel_blocks_second_recovery(home, db, monkeypatch):
    rc.write_sentinel({'status': 'ok', 'note': 'original'}, home=home)
    monkeypatch.setattr(rc, 'attempt',
                        lambda *a: pytest.fail('有哨兵时不该再尝试恢复'))
    report = rc.run_recovery(db, home=home)
    assert report['status'] == 'guarded'
    assert _rows(db) == [(1, 'alive')]
    # 第二次尝试不能把第一次的恢复记录覆盖掉
    assert rc.read_sentinel(home)['note'] == 'original'


def test_exit_writes_sentinel_and_uses_given_code(home, monkeypatch):
    codes = []
    rc.perform_exit({'status': 'ok', 'sentinel': ''}, home=home,
                    exit_fn=lambda code: codes.append(code))
    assert codes == [rc.EXIT_CODE]
    assert rc.read_sentinel(home) is not None


def test_restart_after_crash_sees_the_warning(home, db, monkeypatch):
    """外部脚本拉起后的第二轮：必须知道库刚被恢复过，不能当没事。"""
    monkeypatch.setattr(rc, 'attempt',
                        lambda m, p, w: {'ok': True, 'path': p, 'note': 'ok'})
    first = rc.run_recovery(db, home=home)
    assert first['ok'] is True

    # 模拟新进程：只调启动检查，不改任何状态
    rc.STARTUP_GUARD = {'guarded': False}
    guard = rc.startup_guard(home=home)
    assert guard['guarded'] is True
    assert guard['sentinel']['data_may_be_damaged'] is True
    assert _rows(db) == [(1, 'alive')]


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
                'in_venv', 'free_bytes', 'db_bytes'):
        assert key in info, key
    assert info['db_bytes'] == os.path.getsize(db)
    assert isinstance(info['in_venv'], bool)


def test_module_has_no_emoji():
    path = rc.__file__
    with open(path, encoding='utf-8') as f:
        text = f.read()
    assert not any(ord(ch) > 0xFFFF for ch in text)
