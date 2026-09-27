"""
数据库模块

集中框架的数据访问能力：
- db:        Database 连接池、查询 / 执行、事务、全局单例
- dialect:   SQL 方言翻译（MySQL 方言 → SQLite / MySQL，纯函数）
- schema:    自动建表 + 运行时迁移
- init_db:   按 init.sql 初始化 schema（MySQL 55 / SQLite 兼容）
- file_store: 降级文件存储（数据库不可用时 data/db 下 JSON/YAML 读写）
- sql_sim:   调试模式 SQL 模拟引擎（低性能模式，本地 JSON 行集模拟 SQL 语义）
- storage:   存储抽象层工厂（sqlite / mysql / file / debug 按配置选后端）

适用边界：SQLite 默认仅适合小环境/开发环境；大环境（多群/高并发/多进程）用 MySQL；
调试模式（type: debug）仅适合无数据库的开发/联调环境。
"""

from .db import Database, init_db
from .file_store import FileStore
from .init_db import auto_init_database
from .sql_sim import SqlSimEngine
from .storage import create_storage

__all__ = [
    'Database',
    'FileStore',
    'SqlSimEngine',
    'create_storage',
    'init_db',
    'auto_init_database',
]
