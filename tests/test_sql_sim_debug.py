# -*- coding: utf-8 -*-
"""
调试模式（低性能模式 / 本地模拟 SQL）测试

覆盖 storage.py 抽象层 + SqlSimEngine：
  T1: 抽象层工厂选型（debug / sqlite 按配置选择后端；file 类型已移除应报错）。
  T2: 调试模式 SQL 模拟引擎核心语句：
      建表、INSERT/INSERT IGNORE、WHERE 过滤、COUNT/scalar/exists、
      空表聚合、UPDATE 自增、ORDER+LIMIT+OFFSET、DISTINCT、
      GROUP BY DATE 聚合、持久化重载、多列/数值 ORDER BY、
      LIMIT offset,count、内存行集缓冲（合并落盘/close 兜底/外部改动重载）。
  T3: 不支持语法安全兜底：JOIN / 子查询 / INSERT..SELECT 等
      query→[]、execute→0，绝不静默返回错误数据或抛异常。
  T4: init_db 接入：database.type: debug → SqlSimEngine。
  T5: Framework 级接入：storage_mode == 'debug'、db_debug_mode is True。

纯 pytest 用例（无模块级 sys.exit），与 CI 显式 pytest 列表配合。
运行：python -m pytest tests/test_sql_sim_debug.py -q
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from framework.database import db as db_module
from framework.database.sql_sim import SqlSimEngine
from framework.database.storage import create_storage, normalize_config


# ── T1: 抽象层工厂 -------------------------------------------

def test_normalize_config_string_to_sqlite():
    cfg = normalize_config('data/x.db')
    assert cfg['type'] == 'sqlite'
    assert cfg['path'] == 'data/x.db'
    # 缺省 type → sqlite，缺省 path → data/zcbot.db
    assert normalize_config({})['type'] == 'sqlite'


def test_create_storage_rejects_removed_file_type(tmp_path):
    # file 类型已移除：显式配置应清晰报错，而非静默落到别的后端
    with pytest.raises(ValueError):
        create_storage({'type': 'file', 'fallback_dir': str(tmp_path)})


def test_create_storage_selects_debug(tmp_path):
    # debug → SqlSimEngine（调试模式，SQL 有语义）
    eng = create_storage({'type': 'debug', 'fallback_dir': str(tmp_path)})
    assert isinstance(eng, SqlSimEngine)
    assert eng.db_type == 'debug'
    assert eng.degraded is False
    assert eng.debug_mode is True


def test_create_storage_sqlite(monkeypatch):
    # sqlite → Database（真实库；构造后行为断言由既有 Database 测试覆盖）
    from framework.database.db import Database
    monkeypatch.setattr(db_module, 'Database', _FakeDatabase)
    with pytest.raises(NotImplementedError):  # 兜底防呆：确认返回对象非仿真引擎
        create_storage({'type': 'sqlite'})


class _FakeDatabase:
    """占位：sqlite 分支仅要求返回 Database 类实例（不执行连接）"""

    def __init__(self, cfg):
        raise NotImplementedError("不应被真实连接")


# ── T2: SQL 模拟引擎核心语句 ---------------------------------

def _make_engine(tmp_path):
    return SqlSimEngine({'type': 'debug', 'fallback_dir': str(tmp_path)})


def test_sim_create_insert_select(tmp_path):
    eng = _make_engine(tmp_path)
    assert eng.execute(
        "CREATE TABLE IF NOT EXISTS users (id INT AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(50), age INT)") in (0, 1)

    assert eng.table_exists('users') is True
    info = eng.table_info('users')
    assert any(c['name'] == 'age' for c in info)

    # 指定列插入 + 自增 id
    rid = eng.insert("INSERT INTO users (name, age) VALUES (%s, %s)", ('a', 20))
    assert rid == 1
    rid2 = eng.insert("INSERT INTO users (name, age) VALUES ('b', 30)")
    assert rid2 == 2

    rows = eng.query("SELECT * FROM users ORDER BY id")
    assert len(rows) == 2
    assert rows[0]['name'] == 'a' and rows[0]['age'] == 20 and rows[0]['id'] == 1


def test_sim_insert_ignore_skips_duplicates(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE group_members (id INT AUTO_INCREMENT, gid INT, uid INT)")
    eng.execute("INSERT IGNORE INTO group_members (gid, uid) VALUES (%s, %s)", (7, 100))
    eng.execute("INSERT IGNORE INTO group_members (gid, uid) VALUES (%s, %s)", (7, 100))
    eng.execute("INSERT IGNORE INTO group_members (gid, uid) VALUES (%s, %s)", (7, 101))
    assert eng.count("SELECT COUNT(*) FROM group_members") == 2


def test_sim_where_in_and_not_in(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE t (id INT, tag VARCHAR(20))")
    for i in range(5):
        eng.execute("INSERT INTO t VALUES (%s, %s)", (i, f't{i % 2}'))
    assert eng.count("SELECT COUNT(*) FROM t WHERE id IN (1, 3)") == 2
    assert eng.count("SELECT COUNT(*) FROM t WHERE id NOT IN (1, 3)") == 3
    assert eng.count("SELECT COUNT(*) FROM t WHERE id > 2 AND tag = 't0'") == 1


def test_sim_count_scalar_exists(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE t (id INT, name VARCHAR(20))")
    assert eng.count("SELECT COUNT(*) FROM t") == 0       # 空表聚合 → 0
    assert eng.scalar("SELECT COUNT(*) FROM t") == 0

    eng.execute("INSERT INTO t VALUES (1, 'x')")
    assert eng.exists("SELECT 1 FROM t WHERE name = 'x'") is True
    assert eng.exists("SELECT 1 FROM t WHERE name = 'y'") is False
    row = eng.query_one("SELECT * FROM t WHERE id = 1")
    assert row is not None and row['name'] == 'x'
    assert eng.query_one("SELECT * FROM t WHERE id = 99") is None


def test_sim_on_duplicate_key_update(tmp_path):
    """ODKU：唯一键命中已有行 → 就地更新且 id 稳定，不再追加新行
    （曾退化为核心同步命令重复插入/覆盖失效）。VALUES(col) 取待插入值。"""
    eng = _make_engine(tmp_path)
    eng.execute(
        "CREATE TABLE commands (id INT AUTO_INCREMENT PRIMARY KEY, "
        "plugin_name VARCHAR(64), handler VARCHAR(64), pattern VARCHAR(64), "
        "alias VARCHAR(64), is_active TINYINT DEFAULT 1, "
        "UNIQUE KEY uk_plugin_handler (plugin_name, handler))")
    ins = ("INSERT INTO commands (plugin_name, handler, pattern, alias) "
           "VALUES (%s, %s, %s, %s) "
           "ON DUPLICATE KEY UPDATE pattern = VALUES(pattern), "
           "alias = VALUES(alias)")
    eng.execute(ins, ('p1', 'h1', '/start', '启动'))
    first_id = eng.query_one("SELECT id FROM commands WHERE handler='h1'")['id']
    eng.execute(ins, ('p1', 'h1', '/restart', '重启'))  # 命中 uk → 更新，id 不变
    assert eng.count("SELECT COUNT(*) FROM commands") == 1
    row = eng.query_one("SELECT * FROM commands WHERE handler='h1'")
    assert row['pattern'] == '/restart' and row['alias'] == '重启'
    assert row['id'] == first_id
    assert row['is_active'] == 1  # 缺省列不被 ODKU 破坏

    # 多行 VALUES 部分冲突：h1 更新、h2 新插入
    eng.execute_many(
        "INSERT INTO commands (plugin_name, handler, pattern) "
        "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE pattern = VALUES(pattern)",
        [('p1', 'h1', '/x'), ('p2', 'h2', '/y')])
    assert eng.count("SELECT COUNT(*) FROM commands") == 2
    assert eng.query_one("SELECT pattern FROM commands WHERE handler='h2'")['pattern'] == '/y'
    eng.close()


def test_sim_update_increment_and_now(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE c (id INT, cnt INT, ts DATETIME)")
    eng.execute("INSERT INTO c VALUES (1, 1, NULL)")
    eng.execute("INSERT INTO c VALUES (2, 10, NULL)")
    affected = eng.execute("UPDATE c SET cnt = cnt + 1, ts = NOW() WHERE id = %s", (1,))
    assert affected == 1
    row = eng.query_one("SELECT cnt, ts FROM c WHERE id = 1")
    assert row is not None
    assert row['cnt'] == 2
    assert len(row['ts']) >= 19  # 'YYYY-MM-DD HH:MM:SS'
    # 条件未命中 → 0 行受影响
    assert eng.execute("UPDATE c SET cnt = 0 WHERE id = 99") == 0


def test_sim_order_limit_offset_distinct(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE t (id INT, tag VARCHAR(20))")
    for i in range(5):
        eng.execute("INSERT INTO t VALUES (%s, %s)", (i, f't{i % 2}'))

    rows = eng.query("SELECT id, tag FROM t ORDER BY id DESC LIMIT 2 OFFSET 1")
    assert [r['id'] for r in rows] == [3, 2]

    tags = eng.query("SELECT DISTINCT tag FROM t ORDER BY tag")
    assert sorted(r['tag'] for r in tags) == ['t0', 't1']

    # NULL 排序恒排最后
    eng.execute("INSERT INTO t VALUES (9, NULL)")
    rows = eng.query("SELECT id, tag FROM t ORDER BY tag ASC")
    assert rows[-1]['id'] == 9


def test_sim_order_by_numeric_not_lexicographic(tmp_path):
    """数值列排序按数值序而非字典序（曾按 str 排：'99' > '100'）"""
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE s (id INT, hit_count INT)")
    for h in (100, 99, 9, 1000):
        eng.execute("INSERT INTO s (hit_count) VALUES (%s)", (h,))
    rows = eng.query("SELECT hit_count FROM s ORDER BY hit_count DESC")
    assert [r['hit_count'] for r in rows] == [1000, 100, 99, 9]


def test_sim_order_by_multi_column(tmp_path):
    """多列 ORDER BY（曾静默丢弃第 2+ 列，WebUI 列表排序错乱）"""
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE c (id INT, plugin_name VARCHAR(20), priority INT)")
    # 同 plugin 内 priority 乱序、跨 plugin 交错
    data = [('b', 2), ('a', 5), ('b', 1), ('a', 1), ('b', 3)]
    for i, (p, pri) in enumerate(data):
        eng.execute("INSERT INTO c (plugin_name, priority) VALUES (%s, %s)", (p, pri))
    rows = eng.query(
        "SELECT plugin_name, priority FROM c ORDER BY plugin_name ASC, priority ASC")
    assert [(r['plugin_name'], r['priority']) for r in rows] == [
        ('a', 1), ('a', 5), ('b', 1), ('b', 2), ('b', 3)]
    rows = eng.query(
        "SELECT plugin_name, priority FROM c ORDER BY plugin_name ASC, priority DESC")
    assert [(r['plugin_name'], r['priority']) for r in rows] == [
        ('a', 5), ('a', 1), ('b', 3), ('b', 2), ('b', 1)]


def test_sim_limit_offset_count_form(tmp_path):
    """MySQL 变体 LIMIT offset, count（曾把 offset 当 count 用）"""
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE t (id INT)")
    for i in range(10):
        eng.execute("INSERT INTO t (id) VALUES (%s)", (i,))
    rows = eng.query("SELECT id FROM t ORDER BY id LIMIT 3, 4")
    assert [r['id'] for r in rows] == [3, 4, 5, 6]


def test_sim_memory_buffer_coalesced_flush(tmp_path):
    """内存行集缓冲：写入先进内存（读一致、表存在），flush/close 才落盘"""
    eng = SqlSimEngine({'type': 'debug', 'fallback_dir': str(tmp_path),
                        'debug_flush_ms': 60000})  # 60s：进程内绝不自动刷
    eng.execute("CREATE TABLE t (id INT, name VARCHAR(20))")
    eng.execute("INSERT INTO t VALUES (1, 'a')")
    # 缓冲生效：内存读一致、table_exists 为真，但文件尚未落盘
    assert eng.count("SELECT COUNT(*) FROM t") == 1
    assert eng.table_exists('t') is True
    assert not os.path.isfile(os.path.join(str(tmp_path), 'sql', 't.json'))
    # 显式 flush → 落盘；新实例可从盘上重载
    eng.flush()
    assert os.path.isfile(os.path.join(str(tmp_path), 'sql', 't.json'))
    eng.close()
    eng2 = _make_engine(tmp_path)
    assert eng2.count("SELECT COUNT(*) FROM t") == 1
    eng2.close()


def test_sim_memory_buffer_close_flushes_and_default_on(tmp_path):
    """close() 兜底落盘 + 默认开启缓冲（未配置 debug_flush_ms）"""
    eng = _make_engine(tmp_path)  # 默认 debug_flush_ms=1000
    eng.execute("CREATE TABLE t (id INT, name VARCHAR(20))")
    eng.execute("INSERT INTO t VALUES (1, 'a')")
    eng.close()  # close 兜底全量落盘
    with open(os.path.join(str(tmp_path), 'sql', 't.json'), 'r', encoding='utf-8') as f:
        assert json.load(f)['rows'][0]['name'] == 'a'


def test_sim_sync_write_through_mode(tmp_path):
    """debug_flush_ms: 0 → 每次写同步落盘（旧行为）"""
    eng = SqlSimEngine({'type': 'debug', 'fallback_dir': str(tmp_path),
                        'debug_flush_ms': 0})
    eng.execute("CREATE TABLE t (id INT, name VARCHAR(20))")
    eng.execute("INSERT INTO t VALUES (1, 'a')")
    assert os.path.isfile(os.path.join(str(tmp_path), 'sql', 't.json'))
    eng.close()


def test_sim_external_file_edit_reloads(tmp_path):
    """外部/手工改动 JSON 文件：按 mtime 检测自动重载（脏表以内存为准）"""
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE t (id INT, name VARCHAR(20))")
    eng.execute("INSERT INTO t VALUES (1, 'a')")
    eng.flush()
    # 外部直改文件（模拟另一进程/手工编辑）
    p = os.path.join(str(tmp_path), 'sql', 't.json')
    with open(p, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    raw['rows'][0]['name'] = 'edited'
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(raw, f)
    st = os.stat(p)  # 确定性步进 mtime，防文件系统时间戳粒度撞车
    os.utime(p, (st.st_atime + 5, st.st_mtime + 5))
    assert eng.query_one("SELECT name FROM t WHERE id = 1")['name'] == 'edited'
    eng.close()


def test_sim_group_by_date_aggregate(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE logs (id INT, created_at DATETIME, lv INT)")
    eng.execute("INSERT INTO logs VALUES (1, '2026-09-01 10:00:00', 1)")
    eng.execute("INSERT INTO logs VALUES (2, '2026-09-01 11:00:00', 2)")
    eng.execute("INSERT INTO logs VALUES (3, '2026-09-02 08:00:00', 1)")
    rows = eng.query(
        "SELECT DATE(created_at) AS day, COUNT(*) AS cnt FROM logs "
        "GROUP BY DATE(created_at) ORDER BY day")
    assert rows == [{'day': '2026-09-01', 'cnt': 2},
                    {'day': '2026-09-02', 'cnt': 1}]


def test_sim_persistence_reload(tmp_path):
    eng1 = _make_engine(tmp_path)
    eng1.execute("CREATE TABLE t (id INT, name VARCHAR(20))")
    eng1.execute("INSERT INTO t VALUES (1, 'a')")
    eng1.close()

    # 新实例重新加载同一目录：行集仍在（JSON / sql 子目录）
    eng2 = _make_engine(tmp_path)
    assert eng2.count("SELECT COUNT(*) FROM t") == 1
    sx = os.path.join(str(tmp_path), 'sql', 't.json')
    assert os.path.isfile(sx)
    with open(sx, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    assert raw['rows'][0]['name'] == 'a'
    # 自增 id 续接
    eng2.execute("INSERT INTO t VALUES (2, 'b')")
    assert eng2.count("SELECT COUNT(*) FROM t") == 2


def test_sim_unsupported_sql_safe_fallback(tmp_path):
    eng = _make_engine(tmp_path)
    eng.execute("CREATE TABLE a (id INT, name VARCHAR(20))")
    eng.execute("CREATE TABLE b (id INT, tag VARCHAR(20))")
    eng.execute("INSERT INTO a VALUES (1, 'x')")
    eng.execute("INSERT INTO b VALUES (1, 't')")

    # JOIN → [] 而非静默返回单表全量（曾出现的缺陷）
    assert eng.query("SELECT * FROM a JOIN b ON a.id = b.id") == []
    assert eng.query("SELECT * FROM a, b WHERE a.id = b.id") == []
    assert eng.query("SELECT * FROM a LEFT JOIN b ON a.id = b.id") == []

    # 子查询（SELECT 内 / WHERE 内 / UPDATE SET / DELETE WHERE）→ 安全默认值
    assert eng.query(
        "SELECT * FROM a WHERE id IN (SELECT id FROM b)", None) == []
    assert eng.execute(
        "UPDATE a SET name = (SELECT tag FROM b WHERE id = 1) WHERE id = 1") == 0
    assert eng.execute(
        "DELETE FROM a WHERE id IN (SELECT id FROM b)") == 0

    # INSERT..SELECT / ALTER 等不支持语句
    assert eng.execute("INSERT INTO a SELECT id FROM b") == 0
    assert eng.execute("ALTER TABLE a ADD COLUMN x INT") == 0
    assert eng.query("SELECT * FROM a") == [{'id': 1, 'name': 'x'}]  # 数据未被破坏


def test_sim_transaction_noop_and_conn(tmp_path):
    eng = _make_engine(tmp_path)
    with eng.transaction() as tx:
        assert tx is not None
    with pytest.raises(NotImplementedError):
        eng.get_connection()
    st = eng.pool_status
    assert st['type'] == 'debug'
    assert st['debug_mode'] is True
    assert st['degraded'] is False
    eng.close()  # 幂等关闭不抛


# ── T3: init_db 接入 -----------------------------------------

def test_init_db_debug_returns_sql_sim(tmp_path, monkeypatch):
    """database.type: debug → init_db 返回 SqlSimEngine，不触发真实建表"""
    import framework.database.init_db as init_db_mod
    called = []
    real_auto = init_db_mod.auto_init_database
    monkeypatch.setattr(init_db_mod, 'auto_init_database',
                        lambda *a, **k: called.append(True))
    db = db_module.init_db({'type': 'debug', 'fallback_dir': str(tmp_path)})
    assert isinstance(db, SqlSimEngine)
    assert called == []            # debug 不经 auto_init_database
    assert db.db_type == 'debug'
    assert db.debug_mode is True
    assert db.query("SELECT 1 FROM dual") == []  # 表不存在 → 空
    db.close()


# ── T4: Framework 级接入 -------------------------------------

def _write_debug_config(tmp_path):
    db_path = os.path.join(str(tmp_path), 'data', 'zcbot_debug.db')
    cfg_path = os.path.join(str(tmp_path), 'config.yaml')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n"
            "  type: debug\n"
            f"  path: {db_path}\n"
            "plugin:\n"
            "  heartbeat_interval: 60\n"
            "log:\n"
            "  level: ERROR\n"
            "web:\n"
            "  host: 127.0.0.1\n"
            "  port: 0\n"
            "onebot:\n"
            "  enabled: false\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n"
            "  webui: false\n"
            "  http_api: false\n"
            "  http_inject: false\n"
            "  ws_client: false\n"
            "  qq_official: false\n"
            "  telegram: false\n"
            "  discord: false\n"
            "  session: false\n"
            "  scheduler: false\n"
            "  image_renderer: false\n"
        )
    return cfg_path


def test_framework_debug_mode_flow(tmp_path):
    """Framework 配置 database.type: debug → storage_mode=='debug'、
    db_debug_mode is True；SQL 真实模拟而非空降级"""
    import asyncio
    from framework.core import Framework

    cfg_path = _write_debug_config(tmp_path)
    fw = Framework(config_path=cfg_path, role='standard')
    assert isinstance(fw.db, SqlSimEngine)
    assert fw.storage_mode == 'debug'
    assert fw.db_debug_mode is True
    assert fw.db_degraded is False

    # 模拟内核真实 SQL：建表后查询有语义
    fw.db.execute("CREATE TABLE IF NOT EXISTS guilds (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50))")
    gid = fw.db.insert("INSERT INTO guilds (name) VALUES (%s)", ('测试公会',))
    assert gid == 1
    rows = fw.db.query("SELECT * FROM guilds WHERE name = %s", ('测试公会',))
    assert len(rows) == 1 and rows[0]['id'] == 1
    assert fw.db.count("SELECT COUNT(*) FROM guilds") == 1

    # 全禁用插件加载不抛异常
    for _name in list(fw.config.get('core_plugins', {})):
        fw.config['core_plugins'][_name] = False
    fw._load_core_plugins()

    # 干净关停（避免遗留非 daemon 线程挂住 pytest）
    try:
        asyncio.run(fw.stop())
    except Exception:
        pass