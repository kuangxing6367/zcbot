# -*- coding: utf-8 -*-
"""
IPC 性能基准（优化前后可比度量）

用法: python tools/bench_ipc.py [N]
三个场景，语义不随实现变化：
  1. event 端到端：A 端 notify N 条 → B 端事件线程/读线程收满 N 条才算完成
     （caller-side 耗时 vs end-to-end 耗时，反映"发送是否阻塞调用方线程"）
  2. RPC 往返：同步 call/response N 条，平均单次端到端延迟
  3. 单帧直发（参考）：不经过 JsonRpcConnection，直接对底层 Connection.send 发 N 帧
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import multiprocessing.connection as mpc

from framework.ipc.protocol import JsonRpcConnection


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    mpc.Pipe()  # 预热 multiprocessing.connection

    # ---- 场景3 参考：裸 Connection.send（独立线程 recv，避免管道缓冲阻塞） ----
    a_raw, b_raw = mpc.Pipe(duplex=True)
    raw_got = {'n': 0}
    raw_done = threading.Event()

    def _raw_recv():
        while raw_got['n'] < N:
            b_raw.recv()
            raw_got['n'] += 1
        raw_done.set()

    threading.Thread(target=_raw_recv, daemon=True).start()
    t0 = time.perf_counter()
    for i in range(N):
        a_raw.send({'i': i, 'x': 'y' * 64})
    t1 = time.perf_counter()
    if not raw_done.wait(15):
        print(f"!! raw timeout: 只收到 {raw_got['n']}/{N}")
        return 1
    t2 = time.perf_counter()
    a_raw.close()
    b_raw.close()
    raw_send = (t1 - t0) * 1000
    raw_e2e = (t2 - t0) * 1000
    print(f"[raw conn] {N} frames: send-side {raw_send:.1f}ms e2e {raw_e2e:.1f}ms")

    # ---- 场景1 event 端到端 ----
    a, b = mpc.Pipe(duplex=True)
    ca = JsonRpcConnection(a)
    cb = JsonRpcConnection(b)
    got = {'n': 0}
    done = threading.Event()

    def on_ev(payload):
        got['n'] += 1
        if got['n'] >= N:
            done.set()

    cb.on('bench', on_ev)
    ca.start()
    cb.start()
    # 收满 N 后需要时间让事件线程执行，等 done 即可

    t0 = time.perf_counter()
    for i in range(N):
        ca.notify('bench', {'i': i, 'x': 'y' * 64})
    t_caller = time.perf_counter() - t0
    if not done.wait(15):
        print(f"!! timeout: 只收到 {got['n']}/{N}")
        return 1
    t_e2e = time.perf_counter() - t0
    print(f"[event]   {N} notify: caller-side {t_caller*1000:.1f}ms "
          f"e2e {t_e2e*1000:.1f}ms  {N/t_e2e:.0f} ev/s")

    # ---- 场景2 RPC 往返 ----
    def echo(**kw):
        return kw

    cb.register('echo', echo)
    M = max(N // 20, 50)
    t0 = time.perf_counter()
    for i in range(M):
        r = ca.call('echo', {'i': i}, timeout=5)
        if r['i'] != i:
            print('!! rpc mismatch')
            return 1
    t_rpc = time.perf_counter() - t0
    print(f"[rpc]     {M} calls: total {t_rpc*1000:.1f}ms  "
          f"avg {t_rpc/M*1e6:.0f} us/call")

    ca.close()
    cb.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())