# -*- coding: utf-8 -*-
"""
双核心（core/host 双进程）回归测试
==================================
覆盖分层：
1. 插件进程归属解析（_read_plugin_process_tag）与分派（_core_plugin_is_core_side）；
2. IPC 协议层：进程内起 IpcServer + IpcClient，验证 RPC 往返、未知方法异常、事件 notify/on；
3. 远程数据库代理：双进程下 db.get_connection() 必须抛 NotImplementedError。

运行：
    python tests/test_dual_core.py
    python -m pytest tests/test_dual_core.py -v
"""
import os
import sys
import time
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.core import Framework
from framework.ipc.ipc_server import IpcServer
from framework.ipc.ipc_client import IpcClient
from framework.ipc.protocol import RemoteError
from framework.ipc.remote_db import RemoteDatabase


def _plugin_main(name):
    return os.path.join(ROOT, 'core_plugins', name, 'main.py')


# ── 1. 插件进程归属解析 ──────────────────────────────────────

def test_process_tag_parser():
    # 标记为 process:'core' 的官方插件（协议接入类必须在核心进程）
    core_names = ('onebot_adapter', 'http_inject', 'http_api', 'webui',
                  'ws_client', 'qq_official', 'telegram', 'discord')
    for name in core_names:
        p = _plugin_main(name)
        if os.path.isfile(p):
            assert Framework._read_plugin_process_tag(p) == 'core', name
    # 其余官方插件（含用户侧能力）不应标记 core
    for name in ('scheduler', 'session', 'image_renderer'):
        p = _plugin_main(name)
        if os.path.isfile(p):
            assert Framework._read_plugin_process_tag(p) != 'core', name


def test_core_side_dispatch():
    # _core_plugin_is_core_side 是实例方法，用裸实例（不触发 __init__）调用
    fw = Framework.__new__(Framework)
    # 无显式白名单时，回退到 process 标记
    assert fw._core_plugin_is_core_side(
        'onebot_adapter', _plugin_main('onebot_adapter'), None) is True
    assert fw._core_plugin_is_core_side(
        'scheduler', _plugin_main('scheduler'), None) is False
    # 显式白名单优先
    wl = {'onebot_adapter', 'webui'}
    assert fw._core_plugin_is_core_side(
        'onebot_adapter', _plugin_main('onebot_adapter'), wl) is True
    # 白名单内未列出的插件（即便有 core 标记）按白名单判定
    assert fw._core_plugin_is_core_side(
        'http_inject', _plugin_main('http_inject'), wl) is False


# ── 2. IPC 协议层 ────────────────────────────────────────────

def test_ipc_roundtrip():
    token = b'\x01' * 32
    server = IpcServer(token)
    server.start_accept_thread()
    client = IpcClient(server.address, token)
    try:
        client.connect(timeout=10)

        # 注册一个核心侧方法，宿主经 RPC 调用
        def math_add(a, b):
            return a + b
        server.register('math.add', math_add)
        assert client.call('math.add', {'a': 2, 'b': 3}, timeout=10) == 5

        # 单向事件：宿主 notify → 核心 on
        received = []
        server.on('evt', lambda payload: received.append(payload))
        client.notify('evt', {'x': 1})
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.05)
        assert received and received[0] == {'x': 1}, received

        # 未知方法必须抛 RemoteError
        raised = False
        try:
            client.call('nope.method', {}, timeout=5)
        except RemoteError:
            raised = True
        assert raised, "未知方法未抛 RemoteError"

        # 同步 handler 在 executor 执行，异步 handler 直接 await
        async def upper(text):
            return text.upper()
        server.register('str.upper', upper)
        import asyncio
        assert asyncio.run(
            client.acall('str.upper', {'text': 'abc'}, timeout=10)) == 'ABC'
    finally:
        client.close()
        server.close()


# ── 3. 远程数据库代理约束 ───────────────────────────────────

def test_remote_db_connection_unsupported():
    db = RemoteDatabase(None)  # get_connection 不依赖真实 client
    raised = False
    try:
        db.get_connection()
    except NotImplementedError:
        raised = True
    assert raised, "双进程下 db.get_connection() 应抛 NotImplementedError"


# ── 4. 事件批量（core→host，批量传输优化） ────────────────────

from framework.ipc.core_services import _EventBatcher


class _FakeServer:
    """记录 asend_event_batch 调用（不落真实连接）"""

    def __init__(self):
        self.batches = []

    async def asend_event_batch(self, events):
        self.batches.append(list(events))


def test_event_batcher_threshold():
    import asyncio

    async def run():
        srv = _FakeServer()
        b = _EventBatcher(srv, max_batch=4, max_delay=0)
        for i in range(9):
            await b.push({'i': i})
        # 阈值触发 2 批（4+4），余 1 条在途
        assert [len(x) for x in srv.batches] == [4, 4], srv.batches
        await b.flush()
        assert [len(x) for x in srv.batches] == [4, 4, 1], srv.batches
        flat = [e['i'] for x in srv.batches for e in x]
        assert flat == list(range(9)), flat          # 顺序保持

    asyncio.run(run())


def test_event_batcher_passthrough():
    import asyncio

    async def run():
        srv = _FakeServer()
        b = _EventBatcher(srv, max_batch=1, max_delay=0)   # <=1 → 逐条
        for i in range(5):
            await b.push({'i': i})
        assert [len(x) for x in srv.batches] == [1, 1, 1, 1, 1], srv.batches

    asyncio.run(run())


def test_event_batcher_window_flush():
    import asyncio

    async def run():
        srv = _FakeServer()
        b = _EventBatcher(srv, max_batch=1000, max_delay=0.01)
        for i in range(5):
            await b.push({'i': i})
        assert srv.batches == []                     # 未满批 → 等时间窗口
        await asyncio.sleep(0.06)
        assert [len(x) for x in srv.batches] == [5], srv.batches
        await b.flush()

    asyncio.run(run())


def test_event_batcher_wait_immediate():
    import asyncio

    async def run():
        srv = _FakeServer()
        b = _EventBatcher(srv, max_batch=1000, max_delay=10.0)
        await b.push({'i': 0})
        await b.push({'i': 1}, wait=True)            # 同步语义：立即冲刷并单发
        assert [len(x) for x in srv.batches] == [1, 1], srv.batches

    asyncio.run(run())


def test_event_batch_roundtrip():
    """真实 IpcServer/IpcClient：批量帧端到端，宿主按序还原全部事件"""
    import asyncio
    token = b'\x02' * 32
    server = IpcServer(token)
    server.start_accept_thread()
    client = IpcClient(server.address, token)
    try:
        client.connect(timeout=10)
        got = []
        got_ev = threading.Event()

        def on_ev(payload):
            batch = payload.get('batch') if isinstance(payload, dict) else None
            items = batch if isinstance(batch, list) else [payload]
            got.extend(items)
            if len(got) >= 10:
                got_ev.set()

        client.on('event', on_ev)
        b = _EventBatcher(server, max_batch=4, max_delay=0.02)

        async def push_all():
            for i in range(10):
                await b.push({'i': i})
            await b.flush()

        asyncio.run(push_all())
        assert got_ev.wait(5), f"未收满: {got}"
        assert [e['i'] for e in got] == list(range(10)), got
    finally:
        client.close()
        server.close()


# ── 5. 跨进程事务 op 分派（回归：元数据 / 批量 / 池状态） ──────

def test_remote_tx_run_all_ops():
    """RemoteTxManager.run 必须覆盖 db.* 的全部 op。

    旧实现固定签名 (tx_id, op, sql, params=None) 且 op 分派缺 table_* / pool_status：
    事务内调用 table_exists / table_info / table_has_column / pool_status 会 TypeError，
    execute_many 会因多出 params_list 而 TypeError。本用例逐项回归。
    """
    import sqlite3
    import tempfile
    from framework.ipc.remote_tx import RemoteTxManager

    class _Db:
        db_type = 'sqlite'

        @property
        def pool_status(self):
            return {'pool': 'stub'}

    d = tempfile.mkdtemp(prefix='txtest_')
    path = os.path.join(d, 'tx.db')
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()

    db = _Db()
    db._db_path = path
    mgr = RemoteTxManager(db)
    tx = mgr.begin()
    try:
        assert mgr.run(tx, 'table_exists', table_name='t') is True
        assert mgr.run(tx, 'table_exists', table_name='nope') is False
        info = mgr.run(tx, 'table_info', table_name='t')
        assert any(r.get('name') == 'name' for r in info), info
        assert mgr.run(tx, 'table_has_column', table_name='t', column_name='name') is True
        assert mgr.run(tx, 'table_has_column', table_name='t', column_name='zzz') is False
        assert mgr.run(tx, 'pool_status') == {'pool': 'stub'}
        n = mgr.run(tx, 'execute_many', sql="INSERT INTO t (name) VALUES (%s)",
                    params_list=[('a',), ('b',), ('c',)])
        assert n == 3, n
        assert mgr.run(tx, 'count', sql="SELECT COUNT(*) FROM t") == 3
        assert mgr.run(tx, 'exists', sql="SELECT 1 FROM t WHERE name=%s", params=('a',)) is True
        assert mgr.run(tx, 'scalar', sql="SELECT name FROM t WHERE id=%s", params=(1,)) == 'a'
        assert mgr.run(tx, 'insert', sql="INSERT INTO t (name) VALUES (%s)", params=('d',)) == 4
        mgr.commit(tx)
    except Exception:
        try:
            mgr.rollback(tx)
        except Exception:
            pass
        raise
    c = sqlite3.connect(path)
    cnt = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    c.close()
    assert cnt == 4, cnt


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
