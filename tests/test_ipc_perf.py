# -*- coding: utf-8 -*-
"""IPC 性能优化回归测试（发送队列 + writer 线程 / 批量日志 / 断线快速失败）

覆盖：
1. notify 调用侧不阻塞（发送离线到 writer 线程，调用方 O(1) 入队）
2. call 往返正确性（跨线程并发下 res 匹配不丢）
3. 批量日志 batch 格式可被核心侧解析，500 条压缩为少量帧
4. 对端断开时等待中的 call 快速失败（不再干等 timeout）
5. close 后待发帧不再发送、等待者被唤醒
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import multiprocessing.connection as mpc

from framework.ipc.protocol import JsonRpcConnection, IpcClosed


def _pair():
    a, b = mpc.Pipe(duplex=True)
    ca = JsonRpcConnection(a)
    cb = JsonRpcConnection(b)
    ca.start()
    cb.start()
    return ca, cb


def test_notify_caller_side_nonblocking():
    """优化点1：notify 只入队，调用侧不随帧数线性增长（无 socket 写/序列化）"""
    ca, cb = _pair()
    got = {'n': 0}
    done = threading.Event()

    def on_ev(payload):
        got['n'] += 1
        if got['n'] >= 2000:
            done.set()

    cb.on('bench', on_ev)
    t0 = time.perf_counter()
    for i in range(2000):
        ca.notify('bench', {'i': i, 'x': 'y' * 64})
    caller_ms = (time.perf_counter() - t0) * 1000
    assert done.wait(10), f"事件未收满: {got['n']}/2000"
    # 调用侧只入队：2000 条应远低于同步 socket 写（历史基线 ~50ms+
    # 同步版本实测 >70ms；异步版本实测 <10ms）
    assert caller_ms < 50, f"调用侧耗时异常偏高: {caller_ms:.1f}ms"
    ca.close()
    cb.close()


def test_call_roundtrip_concurrent():
    """优化点2：并发 RPC 往返结果匹配正确（writer 串行化不丢帧/不错配）"""
    ca, cb = _pair()

    def echo(**kw):
        return kw

    cb.register('echo', echo)
    errors = []

    def worker(wid):
        try:
            for i in range(200):
                r = ca.call('echo', {'w': wid, 'i': i}, timeout=10)
                assert r['w'] == wid and r['i'] == i
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(w,), daemon=True)
               for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, f"并发 RPC 失败: {errors[:2]}"
    ca.close()
    cb.close()


def test_batch_log_single_frame_per_interval():
    """优化点3：批量日志把大量记录合并为少量帧，核心侧可解析"""
    from framework.ipc.host_log import IpcLogHandler

    ca, cb = _pair()  # ca=宿主侧发日志, cb=核心侧解析
    received = {'n': 0, 'frames': 0}
    done = threading.Event()

    def on_host_log(payload):
        batch = payload.get('batch') if isinstance(payload, dict) else None
        received['n'] += len(batch) if isinstance(batch, list) else 1
        received['frames'] += 1
        if received['n'] >= 500:
            done.set()

    cb.on('log', on_host_log)
    h = IpcLogHandler(ca)
    h.start()
    import logging as _logging
    for i in range(500):
        rec = _logging.LogRecord(
            name='bench', level=_logging.INFO, pathname=__file__,
            lineno=1, msg='line %d', args=(i,), exc_info=None)
        h.emit(rec)  # type: ignore[arg-type]
    done.wait(10)
    h.flush_logs()
    assert received['n'] == 500
    # 500 条未经批量的旧实现 = 500 帧；批量后至多 1(满批)+1(周期 flush)+1(终刷)
    # flush 定时线程可能把不足批的残余拆成几次发送，放宽到 10 帧以内
    assert received['frames'] <= 10, f"帧数过多: {received['frames']}"
    h.close()
    ca.close()
    cb.close()


def test_disconnect_fast_fail_waker():
    """优化点4：对端断开时等待中的 call 立即失败，不等到 timeout

    对端刻意用「未启动读线程的裸 Connection」：若对端自己也阻塞在同一个 socket 的
    recv() 上，它的 close() 在 Linux 上只摘掉 fd 表项——socket 本体要等 in-flight
    recv 返回才真正释放，本端收不到 FIN（Windows 的 closesocket 会立刻 abort，所以
    旧写法只在 Windows 能观察到断开，Linux 下本用例必然挂到 join 超时）。裸 Connection
    没有在途 recv，close() 即刻向本端发 FIN，与「对端进程退出」等价。
    """
    ca_conn, peer = mpc.Pipe(duplex=True)
    ca = JsonRpcConnection(ca_conn)
    ca.start()

    got_req = threading.Event()

    def peer_reader():
        try:
            msg = peer.recv()
        except Exception:
            return
        if isinstance(msg, dict) and msg.get('t') == 'req':
            got_req.set()
            time.sleep(60)  # 永不回响应：模拟对端卡住；此刻不在 recv，close 才发得出 FIN

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
    assert got_req.wait(10), "请求未送达对端"
    peer.close()  # 对端断开 → ca 读线程 recv 异常 → 唤醒 pending
    t.join(5)
    elapsed = time.perf_counter() - t0
    assert not t.is_alive(), "call 未快速失败（仍在等待）"
    assert result.get('r') != 'no error', result
    # 关键语义：远早于 timeout=30 就返回
    assert elapsed < 10, f"未快速失败，耗时 {elapsed:.1f}s（timeout=30）"
    ca.close()


def test_close_wakes_waiters_no_crash():
    """优化点5：close() 唤醒等待者且不崩溃（interruptible call）"""
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
    t0 = time.perf_counter()
    t.start()
    assert started.wait(10), "请求未送达对端（hang 未被调用）"
    ca.close()  # 关闭连接 → pending 被唤醒
    t.join(5)
    elapsed = time.perf_counter() - t0
    assert not t.is_alive(), "close 未唤醒等待者"
    assert result.get('r') != 'no error', result
    assert elapsed < 10, f"未快速失败，耗时 {elapsed:.1f}s（timeout=30）"
    cb.close()