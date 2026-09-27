# -*- coding: utf-8 -*-
"""
DB schema / 类型约束回归测试（T2 / T3）

覆盖两类线上事故，防止"一半对"修复回潮：
  T2: commands 表靠 ON DUPLICATE KEY UPDATE 去重，但历史 schema 无唯一索引
      → 重复行堆叠。验证：新库建表带唯一约束 + 旧库清洗 + 补索引 + 幂等。
  T3: openid 等身份列是字符串，历史建表却用 BIGINT
      → SQLite 下 19 位以上数字 openid 被静默转浮点丢精度 / MySQL datatype
      mismatch。验证：身份列统一 VARCHAR(64)/TEXT、旧库自动重建、MySQL
      MODIFY 幂等。

纯 pytest 用例（无模块级 sys.exit），与 CI 的显式 pytest 文件列表配合。
运行：python -m pytest tests/test_db_regression.py -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database.db import Database, _auto_create_tables
from framework.database.init_db import auto_init_database


# ── 公共构造 ──────────────────────────────────────────────────

def _new_db(path) -> Database:
    """建一个全新 SQLite 库（走 init.sql 完整建表）。"""
    db = Database({'type': 'sqlite', 'path': str(path), 'name': 't'})
    auto_init_database(db)
    _auto_create_tables(db)
    return db


def _old_commands_table(db, rows):
    """造一个无唯一索引的历史 commands 表并塞入重复行。"""
    db.execute(
        "CREATE TABLE commands ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "plugin_name TEXT NOT NULL, "
        "pattern TEXT NOT NULL DEFAULT '', "
        "handler TEXT NOT NULL, "
        "description TEXT DEFAULT '', "
        "is_active INTEGER DEFAULT 1, "
        "require_level INTEGER DEFAULT 0, "
        "require_perm TEXT DEFAULT '')"
    )
    for i, (plugin, handler) in enumerate(rows):
        db.execute(
            "INSERT INTO commands (plugin_name, pattern, handler, description, "
            "is_active, require_level) VALUES (%s, %s, %s, %s, %s, %s)",
            (plugin, '/x', handler, f'd-{i}', 1, 0))
    # 历史库只有普通索引，无 UNIQUE
    db.execute("CREATE INDEX idx_cmd ON commands (plugin_name, handler)")


# ── T2: commands 唯一索引 / 去重 ──────────────────────────────

def test_commands_unique_fresh_db_dedup(tmp_path):
    """新库：init.sql 已带唯一约束，重复 ON DUPLICATE 只留 1 行。"""
    db = _new_db(tmp_path / 'fresh_commands.db')
    for _ in range(3):
        db.execute(
            "INSERT INTO commands (plugin_name, pattern, handler, description, "
            "is_active) VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE pattern = VALUES(pattern)",
            ('p_demo', r'^/demo$', 'h_demo', 'd', 1))
    row = db.query_one("SELECT COUNT(*) AS c, MIN(id) AS mid FROM commands")
    assert row['c'] == 1, f"重复插入未去重: {row}"
    # 索引存在
    idxs = [r['name'] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='commands'")]
    assert any('uk_plugin_handler' in n or 'autoindex' in n for n in idxs), idxs


def test_commands_legacy_dedup_and_index(tmp_path):
    """旧库：无唯一约束 + 已有重复 → 清洗保留最小 id、补索引、幂等。"""
    db = Database({'type': 'sqlite', 'path': str(tmp_path / 'legacy.db'),
                   'name': 't'})
    _old_commands_table(db, [
        ('p1', 'h1'), ('p1', 'h1'),   # 重复组 → 保留 1 行
        ('p2', 'h2'), ('p2', 'h2'),   # 重复组 → 保留 1 行
    ])
    _auto_create_tables(db)

    rows = db.query("SELECT id, plugin_name, handler FROM commands "
                    "ORDER BY id")
    assert len(rows) == 2, f"清洗后应 2 行: {rows}"
    assert rows[0]['id'] == 1 and rows[1]['id'] == 3, f"应保留最小 id: {rows}"

    # 唯一索引已补 → 再插重复应被拦截/去重
    db.execute(
        "INSERT INTO commands (plugin_name, pattern, handler, description, "
        "is_active) VALUES (%s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE pattern = VALUES(pattern)",
        ('p2', '/x', 'h2', 'dup-again', 1))
    row = db.query_one("SELECT COUNT(*) AS c FROM commands")
    assert row['c'] == 2, f"补索引后应仍 2 行: {row}"

    # 幂等：再跑迁移不炸、不重复清洗
    _auto_create_tables(db)
    row = db.query_one("SELECT COUNT(*) AS c FROM commands")
    assert row['c'] == 2


# ── T3: 身份列字符串类型 / 迁移 ───────────────────────────────

_IDENTITY_COLS = {
    'users': ('user_id',),
    'groups_info': ('group_id',),
    'group_members': ('group_id', 'user_id'),
    'perm_user_nodes': ('user_id',),
    'group_plugin_settings': ('group_id',),
}


def _assert_identity_types_are_text(db):
    for tbl, cols in _IDENTITY_COLS.items():
        if not db.table_exists(tbl):
            continue
        infos = {r['name']: r for r in db.table_info(tbl)}
        for c in cols:
            t = (infos.get(c) or {}).get('type', 'MISSING')
            assert str(t).upper().startswith(('TEXT', 'VARCHAR', 'CHAR')), \
                f"{tbl}.{c} 类型 {t} 不是字符串"


def test_identity_columns_fresh_db(tmp_path):
    """新库：5 张表的身份列全部是字符串类型，杜绝 BIGINT 建表。"""
    db = _new_db(tmp_path / 'fresh_identity.db')
    _assert_identity_types_are_text(db)


def test_identity_columns_legacy_bigint_rebuild(tmp_path):
    """旧库 BIGINT 身份列：自动重建为 TEXT，超长数字 openid 保精度，
    UNIQUE/普通索引保留，幂等。"""
    p = tmp_path / 'legacy_bigint.db'
    raw = Database({'type': 'sqlite', 'path': str(p), 'name': 't'})
    raw.execute("CREATE TABLE users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id BIGINT NOT NULL, "
                "nickname TEXT DEFAULT '', "
                "remark TEXT, "
                "UNIQUE(user_id))")
    raw.execute("CREATE INDEX idx_u_nick ON users (nickname)")
    # 字符串 openid + 超长数字 openid 都写入
    raw.execute("INSERT INTO users (user_id, nickname) VALUES (%s, %s)",
                ('abc_openid_1', 'str_ok'))

    _auto_create_tables(raw)

    # 类型不再是整数
    t = next(r for r in raw.table_info('users') if r['name'] == 'user_id')
    assert str(t['type']).upper() == 'TEXT', t

    # 19/20 位数字 openid 以原文 TEXT 保存，不被转浮点
    raw.execute("INSERT INTO users (user_id, nickname) VALUES (%s, %s)",
                ('12345678901234567890', 'n1'))
    vals = {r['nickname']: r['user_id']
            for r in raw.query("SELECT user_id, nickname FROM users")}
    assert vals['n1'] == '12345678901234567890', f"精度丢失: {vals['n1']!r}"
    assert vals['str_ok'] == 'abc_openid_1'

    # UNIQUE 隐式索引 + 普通索引都保留
    names = [r['name'] for r in raw.query(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='users'")]
    assert any('autoindex' in n for n in names), names
    assert 'idx_u_nick' in names, names

    # 唯一约束行为仍生效
    try:
        raw.execute("INSERT INTO users (user_id, nickname) VALUES (%s, %s)",
                    ('abc_openid_1', 'dup'))
        raise AssertionError("唯一约束丢失：重复 user_id 插入成功")
    except Exception as e:
        assert 'UNIQUE' in str(e).upper() or 'CONSTRAINT' in str(e).upper(), e

    # 幂等复跑
    _auto_create_tables(raw)
    t = next(r for r in raw.table_info('users') if r['name'] == 'user_id')
    assert str(t['type']).upper() == 'TEXT'


def test_identity_mysql_modify_sql_and_idempotent():
    """MySQL 分支：BIGINT 列触发 MODIFY COLUMN VARCHAR(64)，已是字符串则跳过。"""
    from framework.database import schema

    class _MockMySQL:
        db_type = 'mysql'

        def __init__(self):
            self.calls = []
            self.cols = {
                'users': [{'Field': 'user_id', 'Type': 'varchar(64)'}],
                'groups_info': [{'Field': 'group_id', 'Type': 'bigint(20)'}],
                'perm_user_nodes': [{'Field': 'user_id', 'Type': 'bigint(20)'}],
            }

        def table_exists(self, t):
            return t in self.cols

        def table_info(self, t):
            return self.cols[t]

        def execute(self, sql, params=None):
            import re
            self.calls.append(sql)
            m = re.match(
                r"ALTER TABLE `(\w+)` MODIFY COLUMN `(\w+)` "
                r"(VARCHAR\(64\)) NOT NULL", sql)
            if m:
                tbl, col, newt = m.groups()
                for c in self.cols[tbl]:
                    if c['Field'] == col:
                        c['Type'] = newt.lower()

    db = _MockMySQL()
    schema._migrate_identity_id_columns(db)
    assert len(db.calls) == 2, f"应只 MODIFY 两个 bigint 列: {db.calls}"
    assert all('MODIFY COLUMN' in s and 'VARCHAR(64) NOT NULL' in s
               for s in db.calls), db.calls
    # 幂等：再跑一遍无事发生
    db.calls.clear()
    schema._migrate_identity_id_columns(db)
    assert db.calls == [], f"幂等失败: {db.calls}"