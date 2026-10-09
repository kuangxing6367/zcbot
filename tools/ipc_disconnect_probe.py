# -*- coding: utf-8 -*-
"""对照验证：模拟「对端断开时唤醒等待中的 call」在 Linux 上的两种写法差异。

用法：在 zcbot 仓库根目录下 python tools/ipc_disconnect_probe.py
不依赖 pytest，只用标准库 + framework.ipc.protocol。
（自 tmp/ 迁入：诊断 IPC 断开唤醒语义时可直接复用。）
"""
import os
import sys
import threading
import time
import multiprocessing.connection as mpc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from framework.ipc.protocol import JsonRpcConnection  # noqa: E402


def _pair():
    a, b = mpc.Pipe(duplex=True)
    ca = JsonRpcConnection(a)
    cb = JsonRpcConnection(b)
    ca.start()
    cb.start()
    return ca, cb


def probe_old():
    """旧写法：对端（已启动读线程）用 cb._conn.close() 模拟断开。"""
    ca, cb = _pair()
    started = threading.Event()

    def hang(**kw):
        started.set()
        time.sleep(30)

    cb.register('hang', hang)
    result = {}

    def waiter():
        try:
            ca.call('hang', {}, timeout=30)
            result['r'] = 'no error'
        except Exception as e:
            result['r'] = type(e).__name__

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    started.wait(10)
    cb._conn.close()
    t.join(5)
    alive = t.is_alive()
    ca.close()
    try:
        cb.close()
    except Exception:
        pass
    return {'woken': not alive, 'exc': result.get('r')}


def probe_new():
    """新写法：对端用未启动读线程的裸 Connection，close() 立即发 FIN。"""
    ca_conn, peer = mpc.Pipe(duplex=True)
    ca = JsonRpcConnection(ca_conn)
    ca.start()
    got = threading.Event()

    def peer_reader():
        try:
            msg = peer.recv()
        except Exception:
            return
        if isinstance(msg, dict) and msg.get('t') == 'req':
            got.set()
            time.sleep(60)

    threading.Thread(target=peer_reader, daemon=True).start()
    result = {}

    def waiter():
        try:
            ca.call('hang', {}, timeout=30)
            result['r'] = 'no error'
        except Exception as e:
            result['r'] = type(e).__name__

    t = threading.Thread(target=waiter, daemon=True)
    t0 = time.perf_counter()
    t.start()
    got.wait(10)
    peer.close()
    t.join(5)
    elapsed = time.perf_counter() - t0
    alive = t.is_alive()
    ca.close()
    return {'woken': not alive, 'exc': result.get('r'), 'elapsed': round(elapsed, 3)}


if __name__ == '__main__':
    print(f"platform: {sys.platform}")
    print(f"python: {sys.version.split()[0]}")
    print(f"start method: {__import__('multiprocessing').get_start_method(allow_none=True)}")
    print(f"OLD (cb._conn.close(), peer has blocked reader): {probe_old()}")
    print(f"NEW (raw peer close -> immediate FIN)          : {probe_new()}")
