"""方言翻译语义一致性：同一份源码 SQL，两种后端必须给出相同结果。

源码按 MySQL 风格书写（这是框架的书写约定），SQLite 侧靠方言层翻译。
这些用例的价值在于：翻译一旦退化（比如 SELECT 里的 IF 不再被改写），
SQLite 上会直接抛 OperationalError，而 MySQL 上照样通过 —— 只有双后端
都跑才能暴露。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from framework.database.dialect import (
    _translate_sql_for_mysql,
    _translate_sql_for_sqlite,
)

# ── 纯翻译层（不连库，任何后端都跑）────────────────────────────

@pytest.mark.parametrize('sql,expect', [
    ("SELECT IF(a > 0, 'y', 'n') FROM t",
     "SELECT CASE WHEN a > 0 THEN 'y' ELSE 'n' END FROM t"),
    ("SELECT IF(a>0, IF(a>10,'big','small'), 'neg') FROM t",
     "SELECT CASE WHEN a>0 THEN CASE WHEN a>10 THEN 'big' ELSE 'small' END "
     "ELSE 'neg' END FROM t"),
    ("SELECT IFNULL(a, 0) FROM t", "SELECT COALESCE(a, 0) FROM t"),
    ("SELECT UNIX_TIMESTAMP() AS ts", "SELECT strftime('%s','now') AS ts"),
])
def test_sqlite_function_translation(sql, expect):
    """MySQL 专有函数在 SQLite 侧必须被改写（含 SELECT，不只是 DML）。"""
    assert _translate_sql_for_sqlite(sql) == expect


@pytest.mark.parametrize('sql', [
    "SELECT IF(a > 0, 'y', 'n') FROM t",
    "SELECT iif(a > 0, 'y', 'n') FROM t",
    "SELECT COUNTIF(a) FROM t",
])
def test_no_false_positive_translation(sql):
    """不该动的地方不能动：iif/COUNTIF 不在翻译范围，含字面量也不能误伤。"""
    out = _translate_sql_for_sqlite(sql)
    if sql.startswith("SELECT IF("):
        assert 'CASE WHEN' in out
    else:
        assert 'CASE WHEN' not in out


def test_placeholder_not_replaced_inside_literal():
    """%s 只替换占位符，不能把字符串字面量里的 %s 也替换掉。

    真实事故：UNIX_TIMESTAMP() 翻成 strftime('%s','now') 后，占位符转换把
    其中的 '%s' 换成了 '?'，SQLite 返回字面量 '?' 而不是时间戳。
    """
    out = _translate_sql_for_sqlite("SELECT strftime('%s','now'), %s")
    assert "'%s'" in out, f"字面量里的 %s 被误替换: {out}"
    assert out.endswith('?'), f"真正的占位符未被替换: {out}"


def test_on_duplicate_translated():
    sql = ("INSERT INTO t (a,b) VALUES (%s,%s) "
           "ON DUPLICATE KEY UPDATE b = VALUES(b)")
    out = _translate_sql_for_sqlite(sql)
    assert 'ON CONFLICT' in out and 'EXCLUDED.b' in out
    assert 'ON DUPLICATE' not in out


def test_insert_ignore_translated():
    out = _translate_sql_for_sqlite("INSERT IGNORE INTO t (a) VALUES (%s)")
    assert 'INSERT OR IGNORE' in out


def test_prefix_index_budget_767():
    """5.5/5.6 的 767 字节上限：组合索引必须按**累计**字节算前缀。"""
    sql = ("CREATE TABLE t (id INT PRIMARY KEY, target_type VARCHAR(50), "
           "target_name VARCHAR(200), INDEX idx_t (target_type, target_name))")
    out = _translate_sql_for_mysql(sql, max_key_bytes=767)
    assert '`target_name`(' in out, f"组合索引未加前缀: {out}"
    assert 'target_type`(' not in out, f"短列不该加前缀: {out}"


def test_prefix_index_unique_key():
    """UNIQUE KEY 的列超长同样要加前缀（否则 1071 建表失败）。"""
    sql = ("CREATE TABLE t (id INT PRIMARY KEY, token VARCHAR(512) NOT NULL, "
           "UNIQUE KEY uk_token (token))")
    out = _translate_sql_for_mysql(sql, max_key_bytes=767)
    assert 'uk_token (`token`(' in out, f"UNIQUE KEY 未加前缀: {out}"


def test_prefix_index_no_enum():
    """ENUM 列不能加前缀（MySQL 对非字符串类型写前缀会报错）。"""
    sql = ("CREATE TABLE t (id INT PRIMARY KEY, "
           "result ENUM('ok','bad') DEFAULT 'ok', INDEX idx_r (result))")
    out = _translate_sql_for_mysql(sql, max_key_bytes=767)
    assert 'result`(' not in out, f"ENUM 被误加前缀: {out}"


def test_prefix_index_modern_budget_untouched():
    """8.0 的 3072 字节预算下，这些索引不该被改写。"""
    sql = ("CREATE TABLE t (id INT PRIMARY KEY, token VARCHAR(512) NOT NULL, "
           "UNIQUE KEY uk_token (token))")
    assert _translate_sql_for_mysql(sql, max_key_bytes=3072) == sql


# ── 连库语义（每个后端各跑一遍）────────────────────────────────

def _seed(db, table):
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "k VARCHAR(32) NOT NULL, v INTEGER, note VARCHAR(64), "
               "UNIQUE KEY uk_k (k))")
    db.execute_many(
        f"INSERT INTO {table} (k,v,note) VALUES (%s,%s,%s)",
        [('a', 5, None), ('b', -3, 'neg'), ('c', 0, 'zero')])


def test_if_function_in_select(db):
    """SELECT 里的 IF() 必须能跑（回归：曾只在 DDL/DML 分支翻译）。"""
    t = 't_if_sel'
    _seed(db, t)
    try:
        rows = db.query(
            f"SELECT k, IF(v > 0, 'pos', 'nonpos') AS s FROM {t} ORDER BY k")
        got = [(r['k'], r['s']) for r in rows]
        assert got == [('a', 'pos'), ('b', 'nonpos'), ('c', 'nonpos')], got
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_ifnull_in_select(db):
    t = 't_ifnull_sel'
    _seed(db, t)
    try:
        rows = db.query(
            f"SELECT k, IFNULL(note, 'EMPTY') AS s FROM {t} ORDER BY k")
        assert [(r['k'], r['s']) for r in rows] == [
            ('a', 'EMPTY'), ('b', 'neg'), ('c', 'zero')]
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_unix_timestamp_returns_number(db):
    """UNIX_TIMESTAMP() 必须返回真实秒数，不能是字面量 '?'。"""
    row = db.query_one("SELECT UNIX_TIMESTAMP() AS ts")
    ts = str(row['ts'])
    assert ts.isdigit(), f"UNIX_TIMESTAMP() 返回非数字: {ts!r}"
    assert 1_600_000_000 < int(ts) < 2_100_000_000, f"时间戳不在合理区间: {ts}"


def test_now_function_inserts_timestamp(db):
    """NOW() 写入必须落成可解析的时间字符串。"""
    t = 't_now_ins'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "at VARCHAR(32))")
    try:
        db.execute(f"INSERT INTO {t} (at) VALUES (NOW())")
        v = str(db.query_one(f"SELECT at FROM {t}")['at'])
        assert len(v) >= 10 and v[:4].isdigit(), f"NOW() 结果异常: {v!r}"
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_on_duplicate_key_upsert(db):
    """ON DUPLICATE KEY UPDATE 在两边都要真正去重并更新。"""
    t = 't_upsert'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "k VARCHAR(32) NOT NULL, v INTEGER, UNIQUE KEY uk_k (k))")
    try:
        db.execute(f"INSERT INTO {t} (k,v) VALUES (%s,%s) "
                   "ON DUPLICATE KEY UPDATE v = VALUES(v)", ('x', 1))
        db.execute(f"INSERT INTO {t} (k,v) VALUES (%s,%s) "
                   "ON DUPLICATE KEY UPDATE v = VALUES(v)", ('x', 9))
        rows = db.query(f"SELECT k, v FROM {t}")
        assert len(rows) == 1, f"应只有一行，实际 {len(rows)}"
        assert rows[0]['v'] == 9, "未被更新: {}".format(rows[0]['v'])
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_insert_ignore_skips_duplicate(db):
    t = 't_ins_ignore'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "k VARCHAR(32) NOT NULL, UNIQUE KEY uk_k (k))")
    try:
        db.execute(f"INSERT IGNORE INTO {t} (k) VALUES (%s)", ('d',))
        db.execute(f"INSERT IGNORE INTO {t} (k) VALUES (%s)", ('d',))
        assert db.scalar(f"SELECT COUNT(*) FROM {t}") == 1
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_backtick_identifiers(db):
    """反引号列名/表名在两边都要能用。"""
    t = 't_backtick'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (`id` INTEGER PRIMARY KEY AUTOINCREMENT, "
               "`key` VARCHAR(32), `value` INTEGER)")
    try:
        db.execute(f"INSERT INTO {t} (`key`,`value`) VALUES (%s,%s)", ('k', 7))
        row = db.query_one(f"SELECT `key`, `value` FROM {t} WHERE `value` = %s",
                           (7,))
        assert row['key'] == 'k' and row['value'] == 7
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_limit_offset(db):
    t = 't_limit'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "v INTEGER)")
    try:
        db.execute_many(f"INSERT INTO {t} (v) VALUES (%s)",
                        [(i,) for i in range(10)])
        rows = db.query(f"SELECT id FROM {t} ORDER BY id LIMIT 3 OFFSET 2")
        assert [r['id'] for r in rows] == [3, 4, 5]
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_aggregate_and_group_by(db, caps):
    t = 't_agg'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "g VARCHAR(16), v INTEGER)")
    try:
        db.execute_many(f"INSERT INTO {t} (g,v) VALUES (%s,%s)",
                        [('x', 1), ('x', 2), ('y', 3)])
        agg = db.query_one(
            "SELECT COUNT(*) AS c, SUM(v) AS s, MIN(v) AS mn, MAX(v) AS mx "
            f"FROM {t}")
        assert (agg['c'], agg['s'], agg['mn'], agg['mx']) == (3, 6, 1, 3)
        groups = db.query(
            f"SELECT g, COUNT(*) AS c FROM {t} GROUP BY g ORDER BY g")
        assert [(r['g'], r['c']) for r in groups] == [('x', 2), ('y', 1)]
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")
