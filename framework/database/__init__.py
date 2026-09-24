"""
数据库模块

集中框架的数据访问能力：
- db:        Database 连接池、查询 / 执行、事务、全局单例
- dialect:   SQL 方言翻译（MySQL 方言 → SQLite / MySQL，纯函数）
- schema:    自动建表 + 运行时迁移
- init_db:   按 init.sql 初始化 schema（MySQL 55 / SQLite 兼容）

适用边界：SQLite 默认仅适合小环境/开发环境；大环境（多群/高并发/多进程）用 MySQL。
"""

from .db import Database, init_db
from .init_db import auto_init_database

__all__ = [
    'Database',
    'init_db',
    'auto_init_database',
]
