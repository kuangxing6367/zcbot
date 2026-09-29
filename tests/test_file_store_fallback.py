# -*- coding: utf-8 -*-
"""
存储降级 / 调试模式回归测试（去硬依赖）

覆盖"数据库不可用框架仍可启动"的降级方案（最低限度可用）：
  T1: init_db 在数据库驱动缺失/初始化失败时降级为 SqlSimEngine（本地 SQL 模拟），
      框架可初始化、SQL 调用安全不崩。
  T2: 显式配置 database.type: file 已被移除 → 清晰报错而非静默落到其他后端。
  T3: 终端交互在非 TTY 环境（CI/守护进程）不启动输入线程。
  T4: Framework 级降级启动（数据库不可用框架仍可初始化并暴露调试模式标志）。

纯 pytest 用例（无模块级 sys.exit），与 CI 的显式 pytest 文件列表配合。
运行：python -m pytest tests/test_file_store_fallback.py -q
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database.sql_sim import SqlSimEngine
from framework.database import db as db_module


# ── T1: init_db 降级为 SqlSimEngine ─────────────────────────

def test_init_db_degrades_on_missing_driver(monkeypatch):
    """数据库驱动缺失（Database 构造抛 ImportError）→ init_db 降级 SqlSimEngine"""
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
    assert isinstance(db, SqlSimEngine)
    assert db.db_type == 'debug'
    # SQL 调用安全（不支持语法走安全默认，不抛）
    assert db.query("SELECT 1") == []
    assert db.execute("DROP TABLE x") == 0
    db.close()


def test_init_db_explicit_file_type_raises():
    """database.type: file 已移除 → 显式配置应清晰报错"""
    with __import__('pytest').raises(ValueError):
        db_module.init_db({'type': 'file', 'fallback_dir': 'data/db'})


# ── T3: 终端交互非 TTY 不启动 ──────────────────────────────

def test_terminal_input_skips_non_tty(monkeypatch):
    import io
    from framework.terminal.input import TerminalInput

    class _NullFw:
        loop = None

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


# ── T4: Framework 级降级启动 ───────────────────────────────

def test_framework_init_with_degraded_db(tmp_path, monkeypatch):
    """Database 构造失败（缺驱动/连不上）时 Framework 仍可初始化（兜底为 SqlSimEngine）"""
    from framework.core import Framework

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
    assert isinstance(fw.db, SqlSimEngine)
    assert fw.db_debug_mode is True
    assert fw.db.query("SELECT 1") == []   # 内核 SQL 面安全

    # 全禁用插件加载不抛异常
    for _name in list(fw.config.get('core_plugins', {})):
        fw.config['core_plugins'][_name] = False
    fw._load_core_plugins()

    # 干净关停（避免遗留非 daemon 线程挂住 pytest）
    try:
        asyncio.run(fw.stop())
    except Exception:
        pass
