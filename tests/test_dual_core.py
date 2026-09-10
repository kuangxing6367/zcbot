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
    # 标记为 process:'core' 的官方插件
    for name in ('onebot_adapter', 'http_inject', 'http_api', 'webui'):
        assert Framework._read_plugin_process_tag(_plugin_main(name)) == 'core', name
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
