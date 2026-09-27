# -*- coding: utf-8 -*-
"""
降级文件存储回归测试（去硬依赖）

覆盖"数据库不可用框架仍可启动"的降级方案（最垃计划）：
  T1: FileStore 在 data/db 下 JSON/YAML 文件读写、列表、删除、原子写。
  T2: FileStore 与 Database 公开接口同构 —— SQL 类调用返回安全默认值不抛异常。
  T3: init_db 在数据库驱动缺失/初始化失败时降级 FileStore，框架可初始化。
  T4: 终端交互在非 TTY 环境（CI/守护进程）不启动输入线程。

纯 pytest 用例（无模块级 sys.exit），与 CI 的显式 pytest 文件列表配合。
运行：python -m pytest tests/test_file_store_fallback.py -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database.file_store import FileStore
from framework.database import db as db_module


# ── T1: 文件存储读写 ----------------------------------------

def test_file_store_put_get_list_delete(tmp_path):
    store = FileStore({'fallback_dir': str(tmp_path)})
    assert store.db_type == 'file'
    assert store.degraded is True
    assert os.path.isdir(str(tmp_path))

    store.put('t_meta', 'name', 'zcbot')
    store.put('t_meta', 'tags', ['a', 'b'])
    assert store.get('t_meta', 'name') == 'zcbot'
    assert store.get('t_meta', 'missing', 'dft') == 'dft'
    assert 'name' in store.keys('t_meta')

    # 落盘为 JSON 文件且可被独立读取
    jpath = os.path.join(str(tmp_path), 't_meta.json')
    assert os.path.isfile(jpath)
    import json
    with open(jpath, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    assert raw['name'] == 'zcbot'

    # list 返回副本
    data = store.list('t_meta')
    data['name'] = 'hacked'
    assert store.get('t_meta', 'name') == 'zcbot'

    assert store.delete('t_meta', 'name') is True
    assert store.delete('t_meta', 'name') is False
    assert store.get('t_meta', 'name') is None


def test_file_store_yaml_and_save_doc_alias(tmp_path):
    store = FileStore({'fallback_dir': str(tmp_path)})
    store.save_doc('t_cfg', 'group:99', {'enabled': True, 'level': 3, '别名': '测试'})
    assert store.load_doc('t_cfg', 'group:99')['level'] == 3
    assert store.load_doc('t_cfg', 'group:99')['别名'] == '测试'

    # 显式 yaml 写入：文件以 .yaml 结尾，内容可用 pyyaml 读取
    store.put('t_cfg', 'y_doc', {'k': 1}, fmt='yaml')
    ypath = os.path.join(str(tmp_path), 't_cfg.yaml')
    assert os.path.isfile(ypath)
    try:
        import yaml
    except ImportError:
        return  # 环境未装 pyyaml 时跳过 YAML 内容断言
    with open(ypath, 'r', encoding='utf-8') as f:
        doc = yaml.safe_load(f)
    assert doc.get('y_doc', {}).get('k') == 1

    # 列表删除全家桶
    assert store.list_docs('t_cfg').get('group:99')
    assert store.delete_doc('t_cfg', 'group:99') is True
    assert 'group:99' not in store.keys('t_cfg')


def test_file_store_clear_and_invalid_fmt(tmp_path):
    store = FileStore({'fallback_dir': str(tmp_path)})
    store.put('t_a', 'x', 1)
    store.clear('t_a')
    assert store.list('t_a') == {}
    assert not os.path.exists(os.path.join(str(tmp_path), 't_a.json'))

    import pytest
    with pytest.raises(ValueError):
        store.put('t_b', 'x', 1, fmt='xml')


def test_file_store_table_files_isolated(tmp_path):
    store = FileStore({'fallback_dir': str(tmp_path)})
    store.put('t1', 'k', 'v1')
    store.put('t2', 'k', 'v2')
    assert store.get('t1', 'k') == 'v1'
    assert store.get('t2', 'k') == 'v2'
    assert os.path.isfile(os.path.join(str(tmp_path), 't1.json'))
    assert os.path.isfile(os.path.join(str(tmp_path), 't2.json'))


# ── T2: SQL 兼容接口安全默认 -------------------------------

def test_file_store_sql_api_safe_defaults(tmp_path):
    store = FileStore({'fallback_dir': str(tmp_path)})
    assert store.query("SELECT * FROM t") == []
    assert store.query_one("SELECT * FROM t") is None
    assert store.execute("INSERT INTO t VALUES (%s)", (1,)) == 0
    assert store.execute_many("INSERT INTO t VALUES (%s)", [(1,), (2,)]) == 0
    assert store.insert("INSERT INTO t VALUES (%s)", (1,)) == 0
    assert store.scalar("SELECT COUNT(*) FROM t") is None
    assert store.count("SELECT COUNT(*) FROM t") == 0
    assert store.exists("SELECT 1 FROM t") is False
    assert store.table_exists('t') is False
    assert store.table_info('t') == []
    assert store.table_has_column('t', 'id') is False

    import pytest
    with pytest.raises(NotImplementedError):
        store.get_connection()
    with pytest.raises(NotImplementedError):
        with store.transaction():
            pass

    status = store.pool_status
    assert status['type'] == 'file'
    assert status['degraded'] is True
    # 幂等关闭不抛
    store.close()
    store.close()


# ── T3: init_db 降级 ----------------------------------------

def test_init_db_degrades_on_missing_driver(tmp_path, monkeypatch):
    """数据库驱动缺失（Database 构造抛 ImportError）→ init_db 降级 FileStore"""
    def _boom(config):
        raise ImportError("No module named 'pymysql'")

    monkeypatch.setattr(db_module, 'Database', _boom)
    db = db_module.init_db({
        'type': 'mysql',
        'host': '127.0.0.1',
        'user': 'x',
        'password': 'x',
        'name': 'x',
    })
    assert isinstance(db, FileStore)
    assert db.degraded is True
    assert db.query("SELECT 1") == []
    assert db.execute("DROP TABLE x") == 0


def test_init_db_explicit_file_type(tmp_path):
    """显式配置 database.type: file → 直接走文件存储"""
    import framework.database.db as d
    db = d.init_db({'type': 'file', 'fallback_dir': str(tmp_path)})
    assert isinstance(db, FileStore)
    db.put('t', 'k', 'v')
    assert db.get('t', 'k') == 'v'


# ── T4: 终端交互非 TTY 不启动 ---------------------------------

def test_terminal_input_skips_non_tty(monkeypatch):
    import io
    from framework.terminal.input import TerminalInput

    class _NullFw:
        loop = None

    captured = {}

    class _FakeStdin(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr('sys.stdin', _FakeStdin(''))
    term = TerminalInput(_NullFw())
    term.start()
    assert term._thread is None        # 未启动输入线程
    assert term._running is False


def test_terminal_input_starts_on_tty(monkeypatch):
    import io
    from framework.terminal.input import TerminalInput

    class _NullFw:
        loop = None

    class _FakeStdin(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr('sys.stdin', _FakeStdin(''))
    term = TerminalInput(_NullFw())
    term.start()
    assert term._thread is not None    # TTY 环境照常启动
    assert term._running is True
    term.stop()
    # daemon 线程不阻塞退出
    assert term._thread.daemon is True


# ── T5: Framework 级降级启动（数据库不可用框架仍可初始化） ──

def test_framework_init_with_degraded_db(tmp_path, monkeypatch):
    """Database 构造失败（缺驱动/连不上）时 Framework 仍可初始化并暴露降级标志"""
    import asyncio
    from framework.core import Framework
    from framework.database.file_store import FileStore

    def _boom(config):
        raise ImportError("No module named 'pymysql'")

    monkeypatch.setattr(db_module, 'Database', _boom)

    cfg_path = os.path.join(str(tmp_path), 'config.yaml')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n"
            "  type: mysql\n"
            "  host: 127.0.0.1\n"
            "  user: x\n"
            "  password: x\n"
            "  name: x\n"
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
    fw = Framework(config_path=cfg_path, role='standard')
    assert isinstance(fw.db, FileStore)
    assert fw.db_degraded is True
    assert fw.db.query("SELECT 1") == []   # 内核 SQL 面安全降级

    # 全禁用插件加载不抛异常
    for _name in list(fw.config.get('core_plugins', {})):
        fw.config['core_plugins'][_name] = False
    fw._load_core_plugins()

    # 干净关停（避免遗留非 daemon 线程挂住 pytest）
    try:
        asyncio.run(fw.stop())
    except Exception:
        pass