# -*- coding: utf-8 -*-
"""后端能力声明。

参照 SQLAlchemy 的 SuiteRequirements：能力由「声明」给出，测试用
`require(caps, 'xxx')` 断言前提，不支持就 skip。这样新增后端只需要补一张
能力表，不必去改测试里的 if/else —— 也就不会出现「为某后端写死逻辑」。

注意：这里声明的是**翻译层最终能达到的效果**，不是「原生支持」。比如
`if_function` 在 SQLite 上是 False 侧原生不支持、但方言层会翻成 CASE WHEN，
所以这里仍然标 True —— 因为框架的契约是「同一份 SQL 两边都能跑」。
真正没法做到的能力（如 SQLite 的 ALTER COLUMN）才标 False。
"""
import pytest

CAPABILITIES = {
    'sqlite': {
        # 方言层能翻译的（源码按 MySQL 风格写）
        'if_function': True,          # IF() → CASE WHEN
        'ifnull': True,               # IFNULL() → COALESCE()
        'unix_timestamp': True,       # → strftime('%s','now')
        'now_function': True,         # NOW() → 字面量时间戳
        'on_duplicate_key': True,     # → ON CONFLICT DO UPDATE
        'insert_ignore': True,        # → INSERT OR IGNORE
        'auto_increment': True,       # AUTOINCREMENT
        'backtick': True,             # `col` 标识符
        'percent_placeholder': True,  # %s → ?
        # 数据库自身能力
        'alter_column': False,        # SQLite 不支持 ALTER COLUMN
        'drop_column': True,          # 3.35+
        'add_column': True,
        'enum_type': False,           # ENUM → TEXT
        'unsigned_type': False,
        'prefix_index': False,        # 不需要
        'fulltext_index': False,
        'multicolumn_unique': True,
        'foreign_keys_enforced': True,   # PRAGMA foreign_keys=ON 已开
        'savepoint': True,
        'transactional_ddl': True,
        'concurrent_writers': True,      # 单写者，靠 busy_timeout 排队
    },
    'mysql': {
        'if_function': True,
        'ifnull': True,
        'unix_timestamp': True,
        'now_function': True,
        'on_duplicate_key': True,
        'insert_ignore': True,
        'auto_increment': True,
        'backtick': True,
        'percent_placeholder': True,
        'alter_column': True,
        'drop_column': True,
        'add_column': True,
        'enum_type': True,
        'unsigned_type': True,
        'prefix_index': True,
        'fulltext_index': True,
        'multicolumn_unique': True,
        'foreign_keys_enforced': True,
        'savepoint': True,
        'transactional_ddl': False,      # DDL 隐式提交
        'concurrent_writers': True,
    },
}


def caps_of(backend):
    return CAPABILITIES.get(backend.name, {})


def require(caps, *names):
    """断言后端具备这些能力，缺任一则 skip 并说明原因。"""
    missing = [n for n in names if not caps.get(n)]
    if missing:
        pytest.skip("后端不支持: {}".format(', '.join(missing)))
