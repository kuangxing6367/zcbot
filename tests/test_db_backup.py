"""数据库自动备份回归。

这组用例锁的是「无人看着跑」时最容易悄悄错掉的几件事：

- 轮转真的删到只剩 keep 份，且同一秒内连跑两次不会互相覆盖（覆盖会让
  「备份了」变成假象）；
- 空间不足时先探测：探针写到底就停手并删除，**绝不把磁盘填满**（填满会让
  正在跑的数据库写不进去，比没有备份更糟）；
- 超过体积上限就停下来等确认，且不留下半成品 .part；
- 校验没过就不转正（否则坏文件会被当成备份，等到要恢复时才炸）；
- 所有阈值都能通过配置改，写错了要退回安全默认值而不是让线程崩掉。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

import pytest

from framework.database import backup as bk


class FakeDB:
    """够用的 sqlite 替身：只实现备份流程会用到的那两个口子。"""

    db_type = 'sqlite'

    def __init__(self, path):
        self._db_path = path
        self.config = {}

    def execute(self, sql, params=()):
        conn = sqlite3.connect(self._db_path)
        try:
            # 真实 Database.execute 会把 %s 转成 sqlite 的 ?；替身照做，
            # 否则 VACUUM INTO 这条路径永远测不到（会静默退化成文件复制）
            conn.execute(sql.replace('%s', '?'), params)
            conn.commit()
        finally:
            conn.close()


@pytest.fixture()
def workdir():
    tmp = tempfile.mkdtemp(prefix='zcbot_bk_')
    db_path = os.path.join(tmp, 'zcbot.db')
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
    conn.executemany('INSERT INTO t (v) VALUES (?)',
                     [('x' * 200,) for _ in range(1500)])
    conn.commit()
    conn.close()
    yield {'dir': tmp, 'db': db_path, 'out': os.path.join(tmp, 'backups')}
    shutil.rmtree(tmp, ignore_errors=True)


def _files(out):
    try:
        return sorted(os.listdir(out))
    except OSError:
        return []


# ── 正常路径与轮转 ────────────────────────────────────────────

def test_backup_ok_compresses_and_rotates(workdir):
    """连跑三次只留 2 份，且文件名唯一（同一秒内不能撞名覆盖）。"""
    db = FakeDB(workdir['db'])
    for _ in range(3):
        r = bk.run_backup(db, {'keep': 2}, reason='auto')
        assert r['status'] == 'ok', r['note']
        assert r['compressed'] is True  # 默认 100KB 阈值，这个库压得动
        assert (r.get('verify') or {}).get('kind') == 'zip-crc'

    files = [n for n in _files(workdir['out']) if not n.endswith('.part')]
    assert len(files) == 2, files
    assert len(set(files)) == 2
    assert not [n for n in _files(workdir['out']) if n.endswith('.part')]


def test_small_db_below_threshold_is_not_compressed(workdir):
    """阈值之上的才压；把阈值调到 10MB，这个库就该原样留 .db。"""
    r = bk.run_backup(FakeDB(workdir['db']),
                      {'compress_threshold_kb': 10240, 'keep': 2}, reason='auto')
    assert r['status'] == 'ok'
    assert r['compressed'] is False
    assert r['path'].endswith('.db')
    assert (r.get('verify') or {}).get('kind') == 'sqlite'


def test_previous_parts_are_cleaned(workdir):
    """上次中断留下的 .part 不是备份，开跑前应清掉。"""
    os.makedirs(workdir['out'], exist_ok=True)
    stale = os.path.join(workdir['out'], bk.BACKUP_PREFIX + 'old.db' + bk.PART_SUFFIX)
    with open(stale, 'wb') as f:
        f.write(b'half')
    bk.run_backup(FakeDB(workdir['db']), {'keep': 2}, reason='auto')
    assert not os.path.exists(stale)


# ── 空间：探测必须停手并清理 ──────────────────────────────────

def test_probe_stops_at_floor_and_removes_probe(workdir, monkeypatch):
    """可用空间始终贴着地板 → 判定不足、中止备份，且探针文件不残留。

    这里把 free_bytes 钉在 100KB：既低于本次快照需要的量（触发探测），
    也低于 512KB 停手线（探测第一块就该收手）。
    """
    monkeypatch.setattr(bk, 'free_bytes', lambda path: 100 * 1024)
    r = bk.run_backup(FakeDB(workdir['db']), {'keep': 2, 'min_free_after_mb': 0.5},
                      reason='auto')
    assert r['status'] == 'failed'
    assert '空间' in r['note']
    assert r['probe'] is not None and r['probe']['ok'] is False
    assert not [n for n in _files(workdir['out']) if 'disk-probe' in n]
    # 失败时不该产生任何备份文件
    assert [n for n in _files(workdir['out']) if not n.endswith('.part')] == []


def test_probe_reports_capped_instead_of_pretending_success(workdir):
    """需求超过探测上限时只能判「不确定」，不能判「空间够」。"""
    probe = bk.elastic_probe(workdir['dir'], need_bytes=64 * 1024 * 1024,
                             floor_bytes=512 * 1024, chunk_bytes=256 * 1024,
                             max_bytes=1024 * 1024)
    assert probe['kind'] == 'capped'
    assert probe['ok'] is False
    assert not [n for n in _files(workdir['dir']) if 'disk-probe' in n]


def test_probe_success_writes_then_cleans(workdir):
    """写得下就判弹性存储，并且探针文件同样要删干净。"""
    probe = bk.elastic_probe(workdir['dir'], need_bytes=1024 * 1024,
                             floor_bytes=512 * 1024, chunk_bytes=256 * 1024)
    assert probe['ok'] is True
    assert probe['kind'] == 'elastic'
    assert probe['written'] == 1024 * 1024
    assert not [n for n in _files(workdir['dir']) if 'disk-probe' in n]


def test_probe_failure_still_cleans(workdir, monkeypatch):
    """探测自身抛错时也必须删掉探针（否则用户磁盘上留一个不知多大的垃圾）。
    """
    real_open = open

    class Boom:
        def __init__(self, *a, **kw):
            self._f = real_open(*a, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def write(self, buf):
            raise OSError('模拟满盘')

    monkeypatch.setattr('builtins.open', Boom)
    probe = bk.elastic_probe(workdir['dir'], need_bytes=1024 * 1024,
                             floor_bytes=512 * 1024)
    monkeypatch.undo()
    assert probe['ok'] is False
    assert not [n for n in _files(workdir['dir']) if 'disk-probe' in n]


def test_probe_never_writes_when_already_below_floor(workdir, monkeypatch):
    """可用空间已低于停手线时，**一个字节都不能写**。

    这条是「错一点就填满磁盘」的第一道闸：旧写法先写一整块再判断，
    等于「越不足越先写」。
    """
    monkeypatch.setattr(bk, 'free_bytes', lambda path: 100 * 1024)  # 100KB < 512KB 地板
    probe = bk.elastic_probe(workdir['dir'], need_bytes=8 * 1024 * 1024,
                             floor_bytes=512 * 1024)
    assert probe['ok'] is False
    assert probe['written'] == 0
    assert '低于停手线' in probe['note']
    assert not [n for n in _files(workdir['dir']) if 'disk-probe' in n]


def test_probe_slice_never_crosses_floor(workdir, monkeypatch):
    """每片大小按「可用 − 停手线 − 余量」现算，累计写入不能越线。

    用一个会随探针文件增长而减少的假 free：写多少就少多少，和真磁盘同构。
    2MB 可用、512KB 地板 → 最多只能写约 1.25MB；旧实现按固定块长猛写会直接
    冲过地板。
    """
    total, floor = 2 * 1024 * 1024, 512 * 1024

    def fake_free(path):
        used = 0
        try:
            for n in os.listdir(path):
                p = os.path.join(path, n)
                if os.path.isfile(p):
                    used += os.path.getsize(p)
        except OSError:
            pass
        return max(0, total - used)

    monkeypatch.setattr(bk, 'free_bytes', fake_free)
    probe = bk.elastic_probe(workdir['dir'], need_bytes=8 * 1024 * 1024,
                             floor_bytes=floor, chunk_bytes=1024 * 1024)
    assert probe['ok'] is False
    assert probe['kind'] == 'insufficient'
    # 核心断言：写掉的量不足以让可用空间掉到地板以下
    assert probe['written'] <= total - floor, probe
    assert not [n for n in _files(workdir['dir']) if 'disk-probe' in n]


def test_probe_chunk_config_cannot_blow_up_buffer(workdir):
    """把块长配成 4GB 也只该写 1MB —— 缓冲区固定 64KB，不按配置分配。"""
    probe = bk.elastic_probe(workdir['dir'], need_bytes=1024 * 1024,
                             floor_bytes=512 * 1024,
                             chunk_bytes=4096 * 1024 * 1024)
    assert probe['ok'] is True
    assert probe['written'] == 1024 * 1024
    assert not [n for n in _files(workdir['dir']) if 'disk-probe' in n]


# ── 体积上限 ──────────────────────────────────────────────────

def test_refuse_above_limit_leaves_nothing(workdir):
    """超过上限：返回 refused 并清掉半成品，不产生备份文件。"""
    r = bk.run_backup(FakeDB(workdir['db']), {'refuse_above_mb': 0.0001}, reason='auto')
    assert r['status'] == 'refused'
    assert '上限' in r['note']
    assert [n for n in _files(workdir['out'])] == []


def test_allow_large_overrides_limit(workdir):
    r = bk.run_backup(FakeDB(workdir['db']),
                      {'refuse_above_mb': 0.0001, 'allow_large': True}, reason='auto')
    assert r['status'] == 'ok'


# ── 校验 ──────────────────────────────────────────────────────

def test_verify_failure_discards_backup(workdir, monkeypatch):
    """校验没过就不能转正 —— 坏文件被当成备份，等于没有备份。"""
    monkeypatch.setattr(bk, 'verify_backup', lambda *a, **kw: {
        'ok': False, 'kind': 'crc', 'note': '故意失败'})
    r = bk.run_backup(FakeDB(workdir['db']), {'keep': 2}, reason='auto')
    assert r['status'] == 'failed'
    assert '校验' in r['note']
    assert [n for n in _files(workdir['out'])] == []


def test_verify_reads_part_file_by_logical_extension(workdir):
    """在 .part 上校验时不能因为扩展名不是 .zip 就跳过 CRC。"""
    db = FakeDB(workdir['db'])
    os.makedirs(workdir['out'], exist_ok=True)
    src = os.path.join(workdir['out'], 'x.db')
    bk.snapshot_to(db, workdir['db'], src)
    zpath = os.path.join(workdir['out'], 'x.zip' + bk.PART_SUFFIX)
    assert bk.compress_zip(src, zpath)['ok']
    res = bk.verify_backup(zpath, expect_bytes=os.path.getsize(zpath))
    assert res['kind'] == 'zip-crc', res


# ── 配置 ──────────────────────────────────────────────────────

def test_config_clamped_to_safe_range():
    conf = bk.load_config({'keep': 0, 'interval_hours': -5, 'headroom_ratio': 0.1,
                           'compress_level': 99, 'min_free_after_mb': 0})
    assert conf['keep'] == 1
    assert conf['interval_hours'] >= 0.05
    assert conf['headroom_ratio'] == 1.0
    assert conf['compress_level'] == 9
    assert conf['min_free_after_mb'] == 0.05


def test_config_bad_type_falls_back_to_default():
    """写错类型只该退回默认值，不该让备份线程崩。"""
    conf = bk.load_config({'keep': '很多', 'interval_hours': 'abc'})
    assert conf['keep'] == bk.DEFAULTS['keep']
    assert conf['interval_hours'] == bk.DEFAULTS['interval_hours']


def test_disabled_and_mysql_are_skipped(workdir):
    r = bk.run_backup(FakeDB(workdir['db']), {'enabled': False})
    assert r['status'] == 'skipped'

    class MyDB(FakeDB):
        db_type = 'mysql'

    r2 = bk.run_backup(MyDB(workdir['db']), {})
    assert r2['status'] == 'skipped'
    assert 'MySQL' in r2['note']


# ── 服务层 ────────────────────────────────────────────────────

class _Fw:
    def __init__(self, db):
        self.db = db
        self.config = {'database': {'backup': {'keep': 2}}}


def test_service_writes_state_and_status(workdir):
    fw = _Fw(FakeDB(workdir['db']))
    svc = bk.BackupService(fw, out_dir=workdir['out'])
    report = svc.run_once('manual')
    assert report['status'] == 'ok'

    state = os.path.join(workdir['out'], bk.STATE_FILE)
    assert os.path.isfile(state), '重启后 WebUI 要看上次结果，必须落盘'
    with open(state, encoding='utf-8') as f:
        doc = json.load(f)
    assert doc['last']['status'] == 'ok'
    assert doc['runs'] == 1

    st = svc.status()
    assert st['runs'] == 1
    assert st['failures'] == 0
    assert len(st['backups']) == 1
    assert st['keep'] == 2


def test_service_thread_start_stop(workdir):
    """线程要能干净停掉：测试里泄漏一个 daemon 线程会污染后续用例。"""
    fw = _Fw(FakeDB(workdir['db']))
    svc = bk.BackupService(fw, {'enabled': True, 'initial_delay_minutes': 999,
                                'interval_hours': 6}, out_dir=workdir['out'])
    assert svc.start() is True
    assert svc.status()['running'] is True
    svc.stop(timeout=3)
    assert svc.status()['running'] is False
