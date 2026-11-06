# -*- coding: utf-8 -*-
"""数据类型往返一致性。

框架把业务值统一走 `%s` 参数绑定，但两种后端对类型宽度/NULL/编码的处理并不
相同（SQLite 松散类型 + 64 位整数；MySQL 严格模式 + 32 位 INT）。这些用例断言
「写进去什么，读出来还是什么」，避免出现 SQLite 能存、MySQL 截断的静默损失。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def typed(empty_db, request):
    import re
    name = 't_ty_' + re.sub(r'[^a-z0-9]+', '_',
                            request.node.name.lower())[:28].strip('_')
    empty_db.execute("DROP TABLE IF EXISTS {}".format(name))
    empty_db.execute(
        "CREATE TABLE {} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "i_int INTEGER, i_big BIGINT, f_val REAL, s_txt VARCHAR(500), "
        "s_long TEXT, dt VARCHAR(32))".format(name))
    yield name
    empty_db.execute("DROP TABLE IF EXISTS {}".format(name))


def _roundtrip(db, table, **kw):
    cols = list(kw)
    ph = ', '.join(['%s'] * len(cols))
    db.execute("INSERT INTO {} ({}) VALUES ({})".format(table, ', '.join(cols), ph),
               tuple(kw[c] for c in cols))
    row = db.query_one(
        "SELECT {} FROM {} ORDER BY id DESC LIMIT 1".format(', '.join(cols), table))
    return {k: row[k] for k in cols}


def test_small_int(typed, empty_db):
    r = _roundtrip(empty_db, typed, i_int=42)
    assert r['i_int'] == 42


def test_negative_int(typed, empty_db):
    r = _roundtrip(empty_db, typed, i_int=-2147483648)
    assert r['i_int'] == -2147483648


def test_int64_max(typed, empty_db):
    """BIGINT 必须保住 64 位（QQ 号/时间戳量级）。

    MySQL 的 INT 只有 32 位，若列类型写成 INT 会被截断或报 Out of range；
    SQLite 的 INTEGER 天生 64 位。这里断言 BIGINT 列两边都是精确值。
    """
    v = 9223372036854775807
    r = _roundtrip(empty_db, typed, i_big=v)
    assert int(r['i_big']) == v


def test_float_precision(typed, empty_db):
    r = _roundtrip(empty_db, typed, f_val=1.0 / 3.0)
    assert abs(float(r['f_val']) - 1.0 / 3.0) < 1e-12


def test_null_vs_empty_string(typed, empty_db):
    """NULL 与空串必须能区分（很多业务逻辑靠这个判「未设置」）。"""
    r = _roundtrip(empty_db, typed, s_txt=None, dt=None)
    assert r['s_txt'] is None, "NULL 变成了 {!r}".format(r['s_txt'])
    r2 = _roundtrip(empty_db, typed, s_txt='', dt='')
    assert r2['s_txt'] == '', "空串变成了 {!r}".format(r2['s_txt'])


def test_unicode_chinese(typed, empty_db):
    r = _roundtrip(empty_db, typed, s_txt='中文测试·全角（括号）')
    assert r['s_txt'] == '中文测试·全角（括号）'


def test_emoji_utf8mb4(typed, empty_db):
    """utf8mb4：4 字节字符（emoji）不能变成 ? 或乱码。"""
    s = '\U0001F600\U0001F680\U0001F1E8\U0001F1F3'
    r = _roundtrip(empty_db, typed, s_txt=s)
    assert r['s_txt'] == s, "emoji 往返损坏: {!r}".format(r['s_txt'])


def test_long_text(typed, empty_db):
    s = 'x' * 60000
    r = _roundtrip(empty_db, typed, s_long=s)
    assert len(r['s_long']) == 60000
    assert r['s_long'] == s


def test_sql_injection_string_stored_verbatim(typed, empty_db):
    """参数绑定：引号注入串必须原样存原样读，且表还在。"""
    s = "a'; DROP TABLE {}; --".format(typed)
    r = _roundtrip(empty_db, typed, s_txt=s)
    assert r['s_txt'] == s
    assert empty_db.table_exists(typed), "表被注入串删掉了"


def test_datetime_string_roundtrip(typed, empty_db):
    s = '2026-11-06 22:40:00'
    r = _roundtrip(empty_db, typed, dt=s)
    assert str(r['dt']).startswith('2026-11-06'), r['dt']


def test_quote_and_backslash_strings(typed, empty_db):
    for s in ["O'Reilly", 'back\\slash', 'quo"te', "mixed ' and \" both"]:
        r = _roundtrip(empty_db, typed, s_txt=s)
        assert r['s_txt'] == s, "{!r} 往返成 {!r}".format(s, r['s_txt'])
