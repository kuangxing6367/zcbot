"""
数据库模块

集中框架的数据访问能力：
- db:        Database 连接池、查询 / 执行、方言翻译、运行时迁移
- init_db:   按 init.sql 初始化 schema（MySQL 55 / SQLite 兼容）
"""

from .db import Database, init_db
from .init_db import auto_init_database

__all__ = [
    'Database',
    'init_db',
    'auto_init_database',
]
