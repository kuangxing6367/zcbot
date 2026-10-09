# -*- coding: utf-8 -*-
"""框架内置调试控制台 + SQL 方言修复 回归测试（离线）。

覆盖：
1. 方言修复：`PRIMARY KEY CHECK (...)` 不被「KEY <名> (<列>)」清理规则吃掉
   （否则 DDL 被截断成 `id INTEGER PRIMARY`，建表报 near "," syntax error）。
2. ConsoleServer / ConsoleClient：端口+token 自动生成并落库、错误 token 被拒、
   命令执行回传、重启复用端口与 token。
3. load_credentials：从 SQLite 读凭证。

运行：python -m pytest tests/test_console_server.py -q
"""
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.console_server import (  # noqa: E402
    ConsoleClient, ConsoleServer, load_credentials, _CREATE_SQL)


class _FakeDb:
    """极简 Database 替身：走真实 sqlite3，并像框架一样把 %s 翻成 ?（SQLite 方言）。"""

    def __init__(self):
        self.c = sqlite3.connect(':memory:')

    @staticmethod
    def _t(sql):
        # 框架对 SQLite 会把 %s 翻译成 ?；这里照做，才能真实反映占位符是否正确
        return sql.replace('%s', '?')

    def execute(self, sql, params=None):
        cur = self.c.execute(self._t(sql), params or ())
        self.c.commit()
        return cur.rowcount

    def query(self, sql, params=None):
        cur = self.c.execute(self._t(sql), params or ())
        return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None


class _FakeFw:
    def __init__(self, with_agent=True):
        from framework.messaging.protocol import ServiceRegistry
        self.db = _FakeDb()
        self.services = ServiceRegistry()
        if with_agent:
            self.services.register('aiwriter', _FakeAgent())


class _FakeAgent:
    def run(self, text, on_event=None):
        if on_event:
            on_event('tool_start', {'name': 'read', 'args': {'path': 'a.txt'}})
            on_event('tool_end', {'name': 'read', 'ok': True, 'result': 'hi'})
        return '答复：%s' % text


# ── 1. 方言修复 ────────────────────────────────────────────────────────

def test_dialect_keeps_primary_key_check():
    from framework.database.dialect import _translate_sql_for_sqlite
    sql = ("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY CHECK (id = 1),"
           " name TEXT NOT NULL)")
    out = _translate_sql_for_sqlite(sql)
    assert 'PRIMARY KEY' in out.upper(), out
    assert 'CHECK (id = 1)' in out, out
    # 真正能建表（不再 syntax error）
    con = sqlite3.connect(':memory:')
    con.execute(out)
    con.close()


def test_dialect_still_strips_mysql_index_defs():
    from framework.database.dialect import _translate_sql_for_sqlite
    sql = ("CREATE TABLE t (id INTEGER, name TEXT,"
           " KEY idx_name (name), UNIQUE KEY uk_id (id), INDEX idx_x (id))")
    out = _translate_sql_for_sqlite(sql)
    assert 'idx_name' not in out and 'idx_x' not in out      # 索引定义被清理
    assert 'UNIQUE(id)' in out.replace(' ', '')              # UNIQUE KEY → UNIQUE(col)


# ── 2. 控制台服务端 / 客户端 ───────────────────────────────────────────

def _mk_server(**kw):
    fw = _FakeFw()
    srv = ConsoleServer(fw, port=0, token_bits=512, **kw)
    assert srv.start() is True
    return fw, srv


def test_console_roundtrip_and_persist():
    from framework.terminal import terminal_commands
    terminal_commands.register('con_echo', lambda a: print('ARGS:%s' % a), 'test')
    fw, srv = _mk_server()
    try:
        assert srv.port > 0
        assert len(srv.token) == 512 // 4                   # token_hex → 每字节 2 字符
        row = fw.db.query_one("SELECT port, token_bits FROM console_access WHERE id=1")
        assert row and row['port'] == srv.port and row['token_bits'] == 512

        c = ConsoleClient('127.0.0.1', srv.port, srv.token)
        c.connect()
        assert c.banner.get('ok') is True
        assert 'con_echo' in c.banner.get('commands', [])
        assert c.run('con_echo hello') == 'ARGS:hello\n'
        assert '未知命令' in c.run('no_such_cmd')
        c.close()
    finally:
        srv.stop()
        terminal_commands.remove('con_echo')


def test_console_rejects_bad_token():
    fw, srv = _mk_server()
    try:
        bad = ConsoleClient('127.0.0.1', srv.port, 'wrong-token')
        try:
            bad.connect()
        except Exception as e:
            assert 'token' in str(e)
        else:
            raise AssertionError('错误 token 必须被拒绝')
    finally:
        srv.stop()


def test_console_reuses_port_and_token():
    fw, srv = _mk_server()
    port, token = srv.port, srv.token
    srv.stop()
    # Linux 上监听套接字 close 后端口进入 TIME_WAIT，需等其真正不可连再复用，
    # 否则复用探测会误判「已有控制台在跑」（Windows 上 close 立即释放无此问题）。
    import socket as _sock
    for _ in range(60):
        try:
            _sock.create_connection((srv.host, port), timeout=0.3)
        except Exception:
            break
        time.sleep(0.1)
    srv2 = ConsoleServer(fw, port=0, token_bits=512)        # 同一库 → 复用
    assert srv2.start() is True
    try:
        assert srv2.port == port and srv2.token == token
    finally:
        srv2.stop()


def test_console_second_instance_does_not_take_over():
    """已有控制台在跑时，第二个实例不得覆盖凭证（否则先跑那个的 attach 入口失效）。"""
    fw, srv = _mk_server()
    try:
        port, token = srv.port, srv.token
        srv2 = ConsoleServer(fw, port=0, token_bits=512)
        assert srv2.start() is False                      # 不重复监听
        row = fw.db.query_one("SELECT port, token FROM console_access WHERE id=1")
        assert row['port'] == port and row['token'] == token   # 凭证未被覆盖
    finally:
        srv.stop()


def test_console_run_marks_remote_session():
    """控制台执行命令期间必须标记为「远程会话」（全屏界面类命令据此拒绝）。"""
    from framework.terminal import terminal_commands
    from framework.terminal.context import is_remote_session
    terminal_commands.register(
        'con_remote', lambda a: print('REMOTE:%s' % is_remote_session()), 'test')
    fw, srv = _mk_server()
    try:
        assert 'REMOTE:True' in srv.run_command('con_remote')
        assert is_remote_session() is False          # 非远程路径不受影响
    finally:
        srv.stop()
        terminal_commands.remove('con_remote')


def test_console_agent_channel():
    """文本通道也能用智能体：run_agent 返回「工具过程 + 答复」。"""
    fw, srv = _mk_server()
    try:
        c = ConsoleClient('127.0.0.1', srv.port, srv.token)
        c.connect()
        out = c.run_agent('写个 hello')
        assert '[tool] read' in out and '答复：写个 hello' in out
        c.close()
    finally:
        srv.stop()


def test_console_agent_without_service_reports():
    fw = _FakeFw(with_agent=False)
    srv = ConsoleServer(fw, port=0, token_bits=512)
    assert srv.start() is True
    try:
        assert '未启用 aiwriter' in srv.run_agent('hi')
    finally:
        srv.stop()


def test_console_sql_uses_percent_s_placeholders():
    """占位符必须是 %s（框架方言约定）：用 ? 会在 MySQL 下静默落库失败。"""
    import framework.console_server as cs
    src = open(cs.__file__, encoding='utf-8').read()
    assert 'port=%s' in src and 'VALUES (%s' in src
    assert 'port=?' not in src and 'VALUES (1, ?' not in src


def test_credentials_any_dispatch():
    """load_credentials_any 按 database 配置分派；mysql 缺驱动时安全返回 (None, None)。"""
    from framework.console_server import load_credentials_any
    d = tempfile.mkdtemp(prefix='console_cred_any_')
    path = os.path.join(d, 't.db')
    con = sqlite3.connect(path)
    con.execute(_CREATE_SQL)
    con.execute("INSERT INTO console_access (id, port, token, token_bits, created_at, updated_at)"
                " VALUES (1, 4321, 'tok2', 256, 0, 0)")
    con.commit()
    con.close()
    # sqlite：dict 形式 + 字符串形式 + project_root 相对路径
    assert load_credentials_any({'type': 'sqlite', 'path': path}) == (4321, 'tok2')
    assert load_credentials_any(path) == (4321, 'tok2')
    assert load_credentials_any({'type': 'sqlite', 'path': 't.db'}, project_root=d) == (4321, 'tok2')
    # mysql：连不上时必须安全返回，不抛
    p, t = load_credentials_any({'type': 'mysql', 'host': '127.0.0.1', 'port': 1,
                                 'user': 'x', 'password': 'y', 'database': 'z'})
    assert (p, t) == (None, None)


def test_load_credentials_from_sqlite():
    d = tempfile.mkdtemp(prefix='console_cred_')
    path = os.path.join(d, 't.db')
    con = sqlite3.connect(path)
    con.execute(_CREATE_SQL)
    con.execute("INSERT INTO console_access (id, port, token, token_bits, created_at, updated_at)"
                " VALUES (1, 12345, 'tok', 256, 0, 0)")
    con.commit()
    con.close()
    assert load_credentials(path) == (12345, 'tok')
    # 表不存在时返回 (None, None)，不抛
    p2 = os.path.join(d, 'empty.db')
    sqlite3.connect(p2).close()
    assert load_credentials(p2) == (None, None)


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
