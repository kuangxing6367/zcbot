"""Schema 一致性：init.sql 在两种后端上必须产生等价的逻辑结构。

断言的是「等价」而不是「字面相同」：类型名、索引名各后端本来就不一样，
真正要一致的是表集合、列集合、唯一约束、以及**索引的列组合**。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from framework.database.dialect import extract_plain_indexes
from framework.database.init_db import _REQUIRED_TABLES


def _init_sql_text():
    return open(os.path.join(_ROOT, 'sql', 'init.sql'), encoding='utf-8').read()


def _tables(db):
    if db.db_type == 'sqlite':
        rows = db.query("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'")
    else:
        rows = db.query("SELECT table_name AS name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()")
    return {r['name'].lower() for r in rows}


def _low(row):
    """把一行 dict 的键统一成小写。

    MySQL 的 information_schema 列名是大写（INDEX_NAME），pymysql 的 DictCursor
    按服务端返回的标签建键，直接 r['index_name'] 会 KeyError。框架自己在
    schema_snapshot 里用 `AS tbl` 别名 + `.get()` 兜底规避，测试这边统一归一化。
    """
    return {str(k).lower(): v for k, v in dict(row).items()}


def _columns(db, table):
    if db.db_type == 'sqlite':
        return [r['name'].lower() for r in db.query(
            f"PRAGMA table_info({table})")]
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s "
        "ORDER BY ordinal_position", (table,))
    return [_low(r)['column_name'].lower() for r in rows]


def _index_columns(db, table):
    """返回 {索引名: (列, ...)}，按索引名归一化后比较列组合。"""
    out = {}
    if db.db_type == 'sqlite':
        for r in db.query(f"PRAGMA index_list({table})"):
            name = r['name']
            cols = tuple(
                x['name'].lower() for x in db.query(
                    f"PRAGMA index_info({name})")
                if x['name'])
            out[name] = cols
    else:
        rows = db.query(
            "SELECT index_name, column_name FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name=%s "
            "ORDER BY index_name, seq_in_index", (table,))
        for r in rows:
            d = _low(r)
            out.setdefault(d['index_name'], []).append(d['column_name'].lower())
        out = {k: tuple(v) for k, v in out.items() if k != 'PRIMARY'}
    return out


# ── 用例 ──────────────────────────────────────────────────────

def test_all_required_tables_created(schema_db):
    """init.sql 声明的核心表必须全部建出来（不是「建了大部分」）。"""
    have = _tables(schema_db)
    missing = sorted(set(t.lower() for t in _REQUIRED_TABLES) - have)
    assert not missing, f"缺少核心表: {missing}"


def test_table_count_matches_init_sql(schema_db):
    """建出的表数应与 init.sql 的 CREATE TABLE 数一致（不多不少）。"""
    import re
    count = len(re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+`?\w+`?',
                           _init_sql_text(), re.IGNORECASE))
    have = _tables(schema_db)
    assert len(have) == count, f"期望 {count} 张，实际 {len(have)} 张: {sorted(have)}"


def test_no_column_drift_against_init_sql(schema_db):
    """每张表的列集合必须与 init.sql 完全一致（防止某后端丢列/多列）。"""
    import re
    text = _init_sql_text()
    expected = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.search(r'CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)`?\s*\(', s, re.IGNORECASE)
        if m:
            cur = m.group(1).lower()
            expected[cur] = []
            continue
        if cur is None:
            continue
        if s.startswith(')'):
            cur = None
            continue
        if re.match(r'^(PRIMARY|UNIQUE|INDEX|KEY|CONSTRAINT|FOREIGN)\b', s, re.IGNORECASE):
            continue
        m2 = re.match(r'`?(\w+)`?\s+[A-Za-z]', s)
        if m2:
            expected[cur].append(m2.group(1).lower())

    drift = []
    for table, cols in expected.items():
        got = _columns(schema_db, table)
        if set(got) != set(cols):
            drift.append(f'{table}: 缺 {sorted(set(cols) - set(got))} 多 {sorted(set(got) - set(cols))}')
    assert not drift, f"列漂移: {drift[:5]}"


def test_plain_indexes_present(schema_db):
    """init.sql 里声明的普通索引，在**每个**后端都必须真的存在。

    这是一个真实回归点：SQLite 的 CREATE TABLE 不支持内联 KEY/INDEX，
    方言层历史上把整行删掉就完事，导致 SQLite 部署一个二级索引都没有，
    按非唯一列过滤全部走全表扫描。
    """
    declared = extract_plain_indexes(_init_sql_text())
    assert declared, "init.sql 里没解析到普通索引，抽取逻辑可能坏了"

    have = _tables(schema_db)
    missing = []
    for table, name, cols in declared:
        if table.lower() not in have:
            continue
        want = tuple(c.lower() for c in cols)
        actual = _index_columns(schema_db, table)
        if want not in actual.values():
            missing.append(f'{table}.{name} {list(want)}')
    assert not missing, f"缺少 {len(missing)} 个索引: {missing[:6]}"


def test_unique_constraints_enforced(db):
    """唯一约束必须真的生效，不能只是写进 DDL。"""
    t = 't_uq_probe'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "code VARCHAR(64) NOT NULL, UNIQUE KEY uk_code (code))")
    try:
        db.execute(f"INSERT INTO {t} (code) VALUES (%s)", ('A',))
        with pytest.raises(Exception):
            db.execute(f"INSERT INTO {t} (code) VALUES (%s)", ('A',))
        n = db.scalar(f"SELECT COUNT(*) FROM {t}")
        assert n == 1, f"重复插入后应有 1 行，实际 {n}"
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_multicolumn_unique(db, caps):
    """多列唯一约束（group_members 那种 group_id+user_id）必须生效。"""
    t = 't_uq_multi'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "a VARCHAR(32) NOT NULL, b VARCHAR(32) NOT NULL, "
               "UNIQUE KEY uk_ab (a, b))")
    try:
        db.execute(f"INSERT INTO {t} (a,b) VALUES (%s,%s)", ('x', 'y'))
        db.execute(f"INSERT INTO {t} (a,b) VALUES (%s,%s)", ('x', 'z'))
        with pytest.raises(Exception):
            db.execute(f"INSERT INTO {t} (a,b) VALUES (%s,%s)", ('x', 'y'))
        assert db.scalar(f"SELECT COUNT(*) FROM {t}") == 2
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")


def test_auto_increment_generates_ids(db):
    """自增主键在两种后端都要能自动生成（AUTOINCREMENT/AUTO_INCREMENT 翻译）。"""
    t = 't_autoinc'
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "v VARCHAR(16))")
    try:
        for i in range(3):
            db.execute(f"INSERT INTO {t} (v) VALUES (%s)", ('v%d' % i,))
        ids = [r['id'] for r in db.query(f"SELECT id FROM {t} ORDER BY id")]
        assert ids == [1, 2, 3], f"自增 ID 异常: {ids}"
    finally:
        db.execute(f"DROP TABLE IF EXISTS {t}")
