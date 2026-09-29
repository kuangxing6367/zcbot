# -*- coding: utf-8 -*-
"""
存储抽象层：按 database.type 配置选择后端实现（统一工厂）。

四种后端：
    sqlite  → Database（默认，零配置单文件数据库）
    mysql   → Database（大环境，多写高并发）
    debug   → SqlSimEngine（调试模式 / 低性能模式：本地模拟 SQL，行集
              JSON 落盘，查询有语义而非空值——见 sql_sim.py）

工厂统一入口 create_storage(config)，内核其它模块只依赖各后端暴露的
同构接口（query/query_one/execute/execute_many/insert/scalar/count/
exists/table_exists/table_info/table_has_column/get_connection/
transaction/pool_status），因此上层无需感知具体后端。
"""
import logging

logger = logging.getLogger('zcbot')


def normalize_config(config):
    """
    与 framework.database.db._parse_sqlite_type 等价（此处判定供工厂使用）。
    支持简写：database: path 或 database: {type: ..., path: ...}
    """
    if isinstance(config, str):
        return {'type': 'sqlite', 'path': config}
    if isinstance(config, dict):
        cfg = dict(config)
        cfg.setdefault('type', 'sqlite')
        if cfg['type'] == 'sqlite':
            cfg.setdefault('path', 'data/zcbot.db')
        cfg['type'] = str(cfg['type']).lower()
        return cfg
    return {'type': 'sqlite', 'path': 'data/zcbot.db'}


def create_storage(config):
    """
    存储抽象层工厂：按 database.type 选择后端。

    返回对象提供统一的 SQL 兼容接口（见模块 docstring）。
    sqlite/mysql 分支返回 Database（未执行 auto_init，由 init_db 负责
    建表与迁移流程，并在失败时降级为本地 SQL 模拟引擎（SqlSimEngine）。
    """
    cfg = normalize_config(config)
    dtype = cfg['type']

    if dtype == 'file':
        raise ValueError(
            "database.type 'file' 已移除：该降级文件存储无实际 SQL 能力。"
            "请改用 sqlite / mysql（常规/生产）或 debug（无数据库的开发联调）。"
        )

    if dtype == 'debug':
        from .sql_sim import SqlSimEngine
        return SqlSimEngine(cfg)

    from .db import Database
    return Database(cfg)