"""数据库就绪探针必须真的读库：库文件、schema、核心表、TTL、告警限频。"""
import gc
import logging
import os
import shutil
import sys
import weakref
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.api import observability
from framework.database.db import Database
from framework.database.init_db import (
    _REQUIRED_TABLES,
    auto_init_database,
)


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, 'db_probe_ttl', 0)
    path = str(tmp_path / 'probe.db')
    db = Database({'type': 'sqlite', 'path': path})
    auto_init_database(db)
    fw = SimpleNamespace(db=db, _running=True, _ready=True, db_degraded=False)
    yield fw, db, path
    try:
        db.close()
    except Exception:
        pass


def test_normal_probe_reports_required_table_count(live_db):
    fw, _db, _path = live_db
    ok, detail = observability.db_probe(fw)
    expected = f'{len(_REQUIRED_TABLES)}/{len(_REQUIRED_TABLES)}'
    assert ok is True
    assert expected in detail, detail


def test_drop_table_fails_probe_and_updates_schema_incomplete(live_db):
    fw, db, _path = live_db
    db.execute('DROP TABLE group_members')
    ok, detail = observability.db_probe(fw, force=True)
    assert ok is False
    assert 'group_members' in detail
    assert 'group_members' in (db.schema_incomplete or [])


def test_moved_db_file_fails_probe_without_recreating(live_db):
    fw, db, path = live_db
    moved = path + '.moved'
    db.close()
    os.replace(path, moved)
    ok, detail = observability.db_probe(fw, force=True)
    assert ok is False
    assert '不存在' in detail
    assert not os.path.exists(path), '探针不允许为探测而新建空库文件'
    assert os.path.exists(moved)


def test_ttl_caches_real_query_and_force_bypasses(live_db, monkeypatch):
    fw, db, _path = live_db
    calls = {'n': 0}
    original = db.query

    def counted(sql, params=None):
        calls['n'] += 1
        return original(sql, params)

    monkeypatch.setattr(db, 'query', counted)
    monkeypatch.setattr(observability, 'db_probe_ttl', 30)
    assert observability.db_probe(fw, force=True)[0] is True
    assert calls['n'] == 1
    assert observability.db_probe(fw)[0] is True
    assert calls['n'] == 1, 'TTL 内不应再打库'
    assert observability.db_probe(fw, force=True)[0] is True
    assert calls['n'] == 2, 'force=True 必须绕过缓存'


def test_recovered_table_clears_schema_incomplete(live_db):
    fw, db, _path = live_db
    db.execute('DROP TABLE group_members')
    assert observability.db_probe(fw, force=True)[0] is False
    assert db.schema_incomplete
    db.execute('CREATE TABLE group_members (group_id TEXT, user_id TEXT, role TEXT)')
    ok, detail = observability.db_probe(fw, force=True)
    assert ok is True, detail
    assert db.schema_incomplete == []


def test_missing_table_error_is_rate_limited(live_db, caplog, monkeypatch):
    fw, db, _path = live_db
    monkeypatch.setattr(observability, '_MISSING_LOG_LAST', {})
    db.execute('DROP TABLE group_members')
    with caplog.at_level(logging.ERROR, logger='zcbot'):
        for _ in range(5):
            checks = observability.collect_checks(fw)
            assert checks['database']['ok'] is False
    hits = [r for r in caplog.records
            if r.levelno == logging.ERROR and '核心表运行期缺失' in r.getMessage()]
    assert len(hits) == 1
    assert 'group_members' in hits[0].getMessage()
    assert 'data/backups/' in hits[0].getMessage()
    assert 'repair_missing_tables' in hits[0].getMessage()


def test_probe_cache_pins_objects_until_expired(monkeypatch):
    """缓存键用 id()，条目必须持有引用，避免 id 复用后命中旧 ready 结果。"""
    monkeypatch.setattr(observability, 'db_probe_ttl', 30)

    class _DB:
        def scalar(self, sql):
            return 1

    class _Framework:
        pass

    fw = _Framework()
    fw.db = _DB()
    assert observability.db_probe(fw, force=True)[0] is True

    fw_ref = weakref.ref(fw)
    db_ref = weakref.ref(fw.db)
    del fw
    gc.collect()
    assert fw_ref() is not None
    assert db_ref() is not None

    observability._DB_PROBE_CACHE.clear()
    gc.collect()
    assert fw_ref() is None
    assert db_ref() is None
