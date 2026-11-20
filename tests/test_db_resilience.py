"""数据库兜底回归测试（对应两张缺陷图）

覆盖三个真实故障面：

  场景2  库正常建好 → 运行中卷变只读
         · 写操作必须**明确抛错**，不能静默"成功"
         · 读操作必须照常工作（只读不等于不可用）
         · 解除只读后写必须恢复（连接不能被一次失败污染）

  场景3  部分表丢失 → 重启
         · 只丢部分表（admin_users/plugins 仍在）也必须自愈
           （历史实现只拿这两张表当探针，缺表永远不重建）
         · 既有数据不能丢，种子数据不能被重复播种
         · 表齐全时再次初始化必须是无动作（幂等）

  告警可观测性
         · 表缺失且建表失败时，必须报"初始化未完成 + 缺失清单"
         · 建 0 张表绝不能打"初始化完成"

只读用 SQLite 的 `PRAGMA query_only=ON` 模拟：它与只读挂载/只读文件系统
在 SQLite 内核层返回的是同一个错误码 SQLITE_READONLY（"attempt to write
a readonly database"），因此走的是同一条代码路径。
"""
import logging
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database.db import Database
from framework.database.init_db import (
    _REQUIRED_TABLES,
    auto_init_database,
    missing_required_tables,
)
from framework.database.schema import _auto_create_tables


def _new_db(tmp_path, name="t.db"):
    return Database({"type": "sqlite", "path": str(tmp_path / name)})


def _boot(db):
    """模拟一次完整启动：init.sql 补建核心表 + schema.py 补扩展表/迁移。

    真实启动（db.init_db）就是这个顺序，两步缺一不可：
    init.sql 只负责基础表，role / require_level 这类列由 schema.py 迁移补。
    """
    auto_init_database(db)
    _auto_create_tables(db)


# ─────────────────────── 场景3：运行期丢表只告警，不补建 ───────────────────────

def test_partial_table_loss_alarms_without_rebuild(tmp_path, caplog):
    """运行期丢表：必须告警，且不得静默重建（避免空表掩盖数据丢失）。"""
    db = _new_db(tmp_path)
    _boot(db)
    assert missing_required_tables(db) == []

    db.execute(
        "INSERT INTO users(user_id, role, created_at) VALUES(%s, %s, %s)",
        ("u_alive", "member", "2026-01-01 00:00:00"))
    admin_before = len(db.query("SELECT * FROM admin_users"))

    dropped = ["group_members", "plugin_configs", "system_config"]
    for t in dropped:
        db.execute("DROP TABLE %s" % t)
    assert set(missing_required_tables(db)) == set(dropped)

    caplog.set_level(logging.DEBUG, logger="zcbot")
    caplog.clear()
    auto_init_database(db)

    # 1) 仍然缺失：没有自动补建
    assert set(missing_required_tables(db)) == set(dropped)
    # 2) 既有数据不受影响
    row = db.query_one("SELECT role FROM users WHERE user_id=%s", ("u_alive",))
    assert row and row["role"] == "member"
    # 3) 默认管理员没有被重新种回来
    assert len(db.query("SELECT * FROM admin_users")) == admin_before
    # 4) 告警里点名缺失表与处置方式
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "此前已完成初始化" in blob
    assert "group_members" in blob
    assert "repair_missing_tables" in blob
    # 5) 状态可被探针读到
    assert set(db.schema_incomplete) == set(dropped)
    db.close()


def test_explicit_repair_rebuilds_without_default_admin(tmp_path):
    """显式授权补表：表可重建，但绝不能把默认管理员口令写回去。"""
    db = Database({
        "type": "sqlite",
        "path": str(tmp_path / "repair.db"),
        "repair_missing_tables": True,
    })
    _boot(db)
    db.execute("DROP TABLE admin_users")
    db.execute("DROP TABLE system_config")

    auto_init_database(db)

    assert missing_required_tables(db) == []
    # 表被补回来了，但里面没有默认管理员
    assert db.query("SELECT * FROM admin_users") == []
    db.close()


def test_reinit_is_noop_when_schema_complete(tmp_path):
    """表齐全时再次初始化必须无动作（幂等），返回 False。"""
    db = _new_db(tmp_path)
    _boot(db)
    assert auto_init_database(db) is False
    assert missing_required_tables(db) == []
    db.close()


def test_missing_detection_covers_whole_required_list(tmp_path):
    """自检必须覆盖 _REQUIRED_TABLES 全量，而不是只探两张表。"""
    db = _new_db(tmp_path)
    _boot(db)
    # 丢掉一张"历史探针不会看"的表
    db.execute("DROP TABLE audit_logs")
    assert missing_required_tables(db) == ["audit_logs"]
    assert set(_REQUIRED_TABLES) >= {"audit_logs", "group_members", "plugin_configs"}
    db.close()


# ─────────────────────── 场景2：运行中卷变只读 ───────────────────────

def test_readonly_write_raises_and_read_still_works(tmp_path):
    """只读卷：写必须抛错（不能静默），读照常，解除后写恢复。"""
    db = _new_db(tmp_path)
    _boot(db)
    db.execute(
        "INSERT INTO users(user_id, role, created_at) VALUES(%s, %s, %s)",
        ("u_ro", "member", "2026-01-01 00:00:00"))

    db.execute("PRAGMA query_only = ON")

    with pytest.raises(Exception) as ei:
        db.execute(
            "UPDATE users SET role=%s WHERE user_id=%s", ("admin", "u_ro"))
    msg = str(ei.value).lower()
    assert "readonly" in msg or "read-only" in msg, f"错误信息未表明只读: {ei.value}"

    # 读仍然可用（只读卷不等于数据库不可用）
    rows = db.query("SELECT role FROM users WHERE user_id=%s", ("u_ro",))
    assert rows and rows[0]["role"] == "member", "只读期间旧值应保持不变"

    # 解除只读后连接必须能继续写（一次失败不污染连接）
    db.execute("PRAGMA query_only = OFF")
    db.execute("UPDATE users SET role=%s WHERE user_id=%s", ("admin", "u_ro"))
    assert db.query_one(
        "SELECT role FROM users WHERE user_id=%s", ("u_ro",))["role"] == "admin"
    db.close()


def test_readonly_with_complete_schema_stays_quiet(tmp_path):
    """表齐全 + 只读：初始化应直接跳过，不该报错也不该尝试建表。"""
    db = _new_db(tmp_path)
    _boot(db)
    db.execute("PRAGMA query_only = ON")
    assert auto_init_database(db) is False
    assert missing_required_tables(db) == []
    db.close()


def test_readonly_with_missing_table_reports_failure(tmp_path, caplog):
    """表缺失 + 只读：必须告警并点明缺失表，不能悄悄重建。"""
    db = _new_db(tmp_path)
    _boot(db)
    db.execute("DROP TABLE group_members")
    db.execute("PRAGMA query_only = ON")

    caplog.set_level(logging.DEBUG, logger="zcbot")
    caplog.clear()
    auto_init_database(db)

    assert missing_required_tables(db) == ["group_members"]

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "此前已完成初始化" in blob, f"未按要求告警，实际日志:\n{blob}"
    assert "group_members" in blob, "缺失清单里应点名缺失表"
    assert "数据库自动初始化完成" not in blob, "丢表时不得声称初始化完成"
    db.close()


# ─────────────────────── 丢表不触发默认管理员重种 ───────────────────────

def test_seed_not_replayed_on_table_loss(tmp_path):
    """丢表后即使显式补建，也不会重新插入默认管理员。"""
    db = Database({
        "type": "sqlite",
        "path": str(tmp_path / "seed.db"),
        "repair_missing_tables": True,
    })
    _boot(db)
    admin_before = len(db.query("SELECT * FROM admin_users"))

    db.execute("DROP TABLE system_config")
    auto_init_database(db)

    assert len(db.query("SELECT * FROM admin_users")) == admin_before
    db.close()


# ─────────────── 只读降级：框架自己要吭声（不只靠插件报错） ───────────────

def test_persist_degraded_flag_warns_once_and_recovers(tmp_path):
    """只读降级必须置位 persist_degraded，且告警只打一次（不刷屏）。

    日志断言不走 caplog：caplog 依赖 root 传播链，前面跑过“真实 Framework
    接线”类用例后，框架自带的 logging 重配会把它挡掉，导致本用例在全量运行
    时假失败（单独跑却过）。这里直接挂在 'zcbot' logger 上收记录，只测被测
    行为本身，不依赖 pytest 捕获实现。
    """
    db = _new_db(tmp_path)
    _boot(db)
    assert db.persist_degraded is False

    db.execute("PRAGMA query_only = ON")

    records = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = _Sink()
    sink.setLevel(logging.DEBUG)
    target = logging.getLogger('zcbot')
    prev_level = target.level
    target.addHandler(sink)
    target.setLevel(logging.DEBUG)
    try:
        for i in range(3):
            with pytest.raises(Exception):
                db.execute(
                    "INSERT INTO users(user_id, role, created_at) VALUES(%s, %s, %s)",
                    ("u%d" % i, "member", "2026-01-01 00:00:00"))

        assert db.persist_degraded is True, "只读失败后必须置降级标记"
        warned = [r for r in records
                  if r.levelno >= logging.ERROR and "只读" in r.getMessage()]
        assert len(warned) == 1, f"只读告警应只打一次，实际 {len(warned)} 条"
    finally:
        target.removeHandler(sink)
        target.setLevel(prev_level)

    # 恢复可写：标记清除、写成功
    db.execute("PRAGMA query_only = OFF")
    db.execute(
        "INSERT INTO users(user_id, role, created_at) VALUES(%s, %s, %s)",
        ("u_ok", "member", "2026-01-01 00:00:00"))
    assert db.persist_degraded is False, "恢复可写后必须清除降级标记"
    db.close()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root 无视文件权限位（S_IREAD 文件仍可写），该场景需非特权用户才有意义",
)
def test_readonly_file_startup_reads_ok_write_fails(tmp_path):
    """真实只读文件：能启动、能读、写明确失败并置降级标记。"""
    import stat

    db = _new_db(tmp_path, "ro.db")
    _boot(db)
    db.execute(
        "INSERT INTO users(user_id, role, created_at) VALUES(%s, %s, %s)",
        ("u_seed", "member", "2026-01-01 00:00:00"))
    db.close()

    path = tmp_path / "ro.db"
    os.chmod(path, stat.S_IREAD)
    try:
        db2 = _new_db(tmp_path, "ro.db")
        # 表齐全 → 初始化应直接跳过，不该因只读而误报
        assert auto_init_database(db2) is False
        # 读可用
        assert db2.query("SELECT COUNT(*) AS c FROM users")[0]["c"] == 1
        # 写明确失败
        with pytest.raises(Exception) as ei:
            db2.execute(
                "INSERT INTO users(user_id, role, created_at) VALUES(%s, %s, %s)",
                ("u_new", "member", "2026-01-01 00:00:00"))
        assert "readonly" in str(ei.value).lower()
        assert db2.persist_degraded is True
        db2.close()
    finally:
        os.chmod(path, stat.S_IWRITE)
