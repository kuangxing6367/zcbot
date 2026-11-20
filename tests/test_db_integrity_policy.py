"""运行期丢表策略：告警，不自动补建。

背景：`CREATE TABLE IF NOT EXISTS` 分不清「首次安装」与「运行期被删」。
旧行为对两者一视同仁地补建，结果是删表后得到一个空库，默认管理员口令
还可能被重新种回去。本文件锁定新策略的行为边界。
"""
import logging
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database import integrity
from framework.database.db import Database
from framework.database.init_db import (
    _REQUIRED_TABLES,
    auto_init_database,
    missing_required_tables,
)
from framework.database.schema import _auto_create_tables


@pytest.fixture
def no_log_coalescing():
    """临时摘掉日志合并过滤器。

    全量 pytest 里先跑过的 Framework 会把 LogCoalescer 挂到 root handler 上且不摘除；
    相邻用例的同一条空管理员告警会被当成重复告警合并掉。这里只在本用例内隔离
    日志合并，断言本身不变（仍然要求告警原文出现）。
    """
    from framework.coalesce_log import LogCoalescer

    saved = []
    for lg in (logging.getLogger(), logging.getLogger('zcbot')):
        for handler in list(lg.handlers):
            for flt in list(getattr(handler, 'filters', [])):
                if isinstance(flt, LogCoalescer):
                    handler.removeFilter(flt)
                    saved.append((handler, flt))
    try:
        yield
    finally:
        for handler, flt in saved:
            handler.addFilter(flt)


def make_db(tmp_path, name='t.db', **extra):
    cfg = {'type': 'sqlite', 'path': str(tmp_path / name)}
    cfg.update(extra)
    return Database(cfg)


def boot(db):
    auto_init_database(db)
    _auto_create_tables(db)


def test_first_install_creates_tables_and_marks(tmp_path):
    db = make_db(tmp_path)
    assert missing_required_tables(db) == list(_REQUIRED_TABLES)
    assert integrity.is_initialized(db) is False

    boot(db)

    assert missing_required_tables(db) == []
    assert integrity.is_initialized(db) is True
    # 首次安装仍然种默认管理员（否则没有入口登录）
    assert db.query_one("SELECT username FROM admin_users WHERE username=%s",
                        ("admin",)) is not None
    db.close()


def test_runtime_drop_alarms_and_keeps_tables_missing(tmp_path, caplog):
    db = make_db(tmp_path)
    boot(db)
    db.execute("INSERT INTO users(user_id, role, created_at) VALUES(%s,%s,%s)",
               ("u1", "member", "2026-01-01 00:00:00"))

    db.execute("DROP TABLE plugin_configs")
    caplog.set_level(logging.DEBUG, logger="zcbot")
    caplog.clear()

    assert auto_init_database(db) is False

    assert missing_required_tables(db) == ["plugin_configs"]
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "此前已完成初始化" in blob
    assert "plugin_configs" in blob
    assert db.schema_incomplete == ["plugin_configs"]
    # 业务数据未被牵连
    assert db.query_one("SELECT role FROM users WHERE user_id=%s", ("u1",))["role"] == "member"
    db.close()


def test_repeated_boot_does_not_flip_policy(tmp_path):
    """连续两次启动都不能把缺失表补回来。"""
    db = make_db(tmp_path)
    boot(db)
    db.execute("DROP TABLE audit_logs")
    boot(db)
    boot(db)
    assert missing_required_tables(db) == ["audit_logs"]
    db.close()


def test_repair_opt_in_skips_default_admin(tmp_path):
    db = make_db(tmp_path, repair_missing_tables=True)
    boot(db)
    db.execute("DROP TABLE admin_users")

    boot(db)

    assert missing_required_tables(db) == []
    assert db.query("SELECT * FROM admin_users") == []
    db.close()


def test_repair_empty_admin_users_warns_with_file_admin_hint(
        tmp_path, caplog, no_log_coalescing):
    db = make_db(tmp_path, repair_missing_tables=True)
    boot(db)
    db.execute("DROP TABLE admin_users")
    caplog.set_level(logging.WARNING, logger="zcbot")
    caplog.clear()

    boot(db)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "admin_users 表为空" in blob
    assert "admin_emergency.yaml" in blob
    assert "emergency_admin --reset" in blob
    db.close()


def test_extension_table_loss_is_not_rebuilt(tmp_path, caplog):
    db = make_db(tmp_path)
    boot(db)
    db.execute("DROP TABLE drive")

    caplog.set_level(logging.DEBUG, logger="zcbot")
    caplog.clear()
    _auto_create_tables(db)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "drive" in blob
    assert "运行期被删除" in blob
    assert db.table_exists("drive") is False
    db.close()


def test_marker_absent_complete_schema_is_adopted(tmp_path):
    """老版本升级上来的库：表齐但没有标记，启动时补写标记，不重种数据。"""
    db = make_db(tmp_path)
    boot(db)
    integrity.clear_marker(db)
    admin_before = len(db.query("SELECT * FROM admin_users"))

    assert auto_init_database(db) is False

    assert integrity.is_initialized(db) is True
    assert len(db.query("SELECT * FROM admin_users")) == admin_before
    db.close()


def test_clear_marker_allows_fresh_reinstall(tmp_path):
    """人工删标记后，空库按首次安装处理。"""
    db = make_db(tmp_path)
    boot(db)
    for table in _REQUIRED_TABLES:
        db.execute("DROP TABLE %s" % table)
    assert integrity.clear_marker(db) is True

    boot(db)

    assert missing_required_tables(db) == []
    assert db.query_one("SELECT username FROM admin_users WHERE username=%s",
                        ("admin",)) is not None
    db.close()


def test_schema_incomplete_surfaces_in_readyz():
    """探针读到缺失表就必须不就绪，避免继续接流量。"""
    from framework.api.observability import db_probe

    class FakeDB:
        schema_incomplete = ['admin_users']

        def scalar(self, _sql):
            return 1

    class FakeFramework:
        db = FakeDB()
        db_degraded = False

    ok, detail = db_probe(FakeFramework())
    assert ok is False
    assert '核心表缺失' in detail


# ───────────── 标记丢失时按库内证据判定（换机恢复 / 标记未随库走）─────────────

def test_marker_lost_but_schema_present_counts_as_initialized(tmp_path):
    """标记文件丢了，但库里的表还在 → 判定已初始化，并补写带 evidence 的标记。"""
    db = make_db(tmp_path)
    boot(db)
    os.unlink(integrity.marker_path(db))

    assert integrity.is_initialized(db) is True
    # 查询函数不改磁盘：补写标记由启动流程负责
    assert integrity.read_marker(db) is None
    boot(db)
    marker = integrity.read_marker(db) or {}
    assert marker.get('evidence')
    db.close()


def test_marker_lost_on_empty_db_allows_first_install(tmp_path):
    """真正的空库（一张表都没有）不能因为有标记判定逻辑就变成「已初始化」。"""
    db = make_db(tmp_path)
    assert integrity.is_initialized(db) is False
    db.close()


def test_marker_lost_and_admin_table_dropped_never_seeds_default_password(tmp_path):
    """最危险组合：标记丢失 + admin_users 被删。

    只认标记的实现会把它当成首次安装，重建 admin_users 并种回 admin/admin123，
    等于给任何能删表的人一个后门账号。必须按「运行期丢表」只告警不补建。
    """
    db = make_db(tmp_path)
    boot(db)
    os.unlink(integrity.marker_path(db))
    db.execute('DROP TABLE admin_users')

    assert integrity.is_initialized(db) is True
    boot(db)

    assert db.table_exists('admin_users') is False
    assert 'admin_users' in missing_required_tables(db)
    db.close()


def test_evidence_query_failure_is_conservative(tmp_path, monkeypatch):
    """证据查不出来时保守判为已初始化——宁可不种默认账号，也不在坏库上写入。"""
    db = make_db(tmp_path)
    boot(db)
    os.unlink(integrity.marker_path(db))
    monkeypatch.setattr(integrity, 'has_data_evidence', lambda _db: None)

    assert integrity.is_initialized(db) is True
    db.close()


def test_marker_backfill_records_existing_tables(tmp_path):
    """补写标记必须记下当前表名：写成空列表会让 may_create 放行所有表的重建。"""
    db = make_db(tmp_path)
    boot(db)
    os.unlink(integrity.marker_path(db))
    assert integrity.is_initialized(db) is True
    boot(db)  # 补写标记由启动流程完成

    marker = integrity.read_marker(db) or {}
    tables = marker.get('tables') or []
    assert tables
    assert 'admin_users' in tables
    assert integrity.may_create(db, 'admin_users') is False
    db.close()
