"""SQLite 跨 Python 版本兼容层：显式日期转换与能力探测。

Python 3.12 起 sqlite3 默认 datetime adapter/converter 进入弃用期，3.13 下
DATETIME 与 TIMESTAMP 的返回类型已经不一致。这里把行为钉成显式契约。
"""
import datetime as dt
import sqlite3

from framework.database import sqlite_compat as sc


def test_parse_datetime_formats():
    assert sc.parse_datetime('2026-01-02 03:04:05') == dt.datetime(2026, 1, 2, 3, 4, 5)
    assert sc.parse_datetime('2026-01-02 03:04:05.123456') == dt.datetime(2026, 1, 2, 3, 4, 5, 123456)
    assert sc.parse_datetime('2026-01-02T03:04:05') == dt.datetime(2026, 1, 2, 3, 4, 5)
    assert sc.parse_datetime('2026-01-02T03:04:05.5') == dt.datetime(2026, 1, 2, 3, 4, 5, 500000)


def test_parse_datetime_bytes_and_garbage():
    assert sc.parse_datetime(b'2026-01-02 03:04:05') == dt.datetime(2026, 1, 2, 3, 4, 5)
    assert sc.parse_datetime('不是时间') == '不是时间'
    assert sc.parse_datetime('') == ''


def test_parse_date_formats():
    assert sc.parse_date('2026-01-02') == dt.date(2026, 1, 2)
    assert sc.parse_date(b'2026-01-02') == dt.date(2026, 1, 2)
    assert sc.parse_date('不是日期') == '不是日期'


def test_to_iso_runtime_types():
    assert sc.to_iso(dt.datetime(2026, 1, 2, 3, 4, 5)) == '2026-01-02 03:04:05'
    assert sc.to_iso(dt.date(2026, 1, 2)) == '2026-01-02'
    assert sc.to_iso('原样字符串') == '原样字符串'
    assert sc.to_iso(7) == 7


def test_ensure_registered_is_idempotent():
    sc.ensure_registered()
    sc.ensure_registered()
    assert sc._REGISTERED is True


def test_explicit_converters_apply_to_all_date_columns():
    sc.ensure_registered()
    con = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        con.execute('CREATE TABLE t (a DATETIME, b TIMESTAMP, c DATE, d TEXT)')
        con.execute("INSERT INTO t VALUES ('2026-01-02 03:04:05', '2026-01-02 03:04:05', '2026-01-02', '2026-01-02 03:04:05')")
        row = con.execute('SELECT a, b, c, d FROM t').fetchone()
        assert row[0] == row[1] == dt.datetime(2026, 1, 2, 3, 4, 5)
        assert row[2] == dt.date(2026, 1, 2)
        assert row[3] == '2026-01-02 03:04:05'
    finally:
        con.close()


def test_capabilities_shape():
    cap = sc.capabilities()
    assert isinstance(cap['python'], str) and cap['python']
    assert isinstance(cap['sqlite'], str) and cap['sqlite']
    assert isinstance(cap['version_tuple'], tuple) and len(cap['version_tuple']) == 3
    assert isinstance(cap['vacuum_into'], bool)
    assert isinstance(cap['recover_cli'], bool)
    assert cap['vacuum_into'] == (cap['version_tuple'] >= (3, 27, 0))
    assert cap['recover_cli'] == (cap['version_tuple'] >= (3, 32, 0))
    assert sc.has_vacuum_into() == cap['vacuum_into']
    assert sc.has_recover_cli() == cap['recover_cli']


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
