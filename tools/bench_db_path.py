# -*- coding: utf-8 -*-
"""双核心 DB 写路径对比基准：同步 RPC 逐条 vs 异步广播攒批 vs 同步 RPC 攒批

用真实 JsonRpcConnection（与双核心同一套协议）+ 真实 sqlite3（文件库 / WAL），
模拟「宿主=插件侧、核心=DB 侧」。度量 N 次写的总耗时 / 单次均摊 / 吞吐。

用法: python tools/bench_db_path.py [N]
"""
import os
import sys
import time
import threading
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import multiprocessing.connection as mpc
from framework.ipc.protocol import JsonRpcConnection

BATCH = 256


def _mk_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS u (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)")
    conn.commit()
    return conn


class CoreSide:
    """核心侧：真实 sqlite3 + 攒批"""

    def __init__(self, conn, rpc):
        self._conn = conn
        self._lock = threading.Lock()
        self._buf = []
        self._total = 0
        rpc.register('db.insert', self._sync_insert)
        rpc.register('db.insert_many', self._sync_insert_many)
        rpc.register('drain', self._drain)
        rpc.on('db.write', self._on_write)     # 异步广播（fire-and-forget，逐条）
        rpc.on('db.write_batch', self._on_write_batch)   # 异步广播（fire-and-forget，批量）

    def _sync_insert(self, **p):
        with self._lock:
            self._conn.execute("INSERT INTO u (name, n) VALUES (?,?)", (p['name'], p['n']))
            self._conn.commit()
        self._total += 1
        return 1

    def _sync_insert_many(self, rows=None, **p):
        rows = rows or []
        with self._lock:
            self._conn.executemany("INSERT INTO u (name, n) VALUES (?,?)", rows)
            self._conn.commit()
        self._total += len(rows)
        return len(rows)

    def _on_write(self, payload):
        self._buf.append((payload['name'], payload['n']))
        if len(self._buf) >= BATCH:
            self._flush()

    def _on_write_batch(self, payload):
        rows = payload.get('rows') or []
        with self._lock:
            self._conn.executemany("INSERT INTO u (name, n) VALUES (?,?)", rows)
            self._conn.commit()
        self._total += len(rows)

    def _flush(self):
        if not self._buf:
            return
        batch = self._buf
        self._buf = []
        with self._lock:
            self._conn.executemany("INSERT INTO u (name, n) VALUES (?,?)", batch)
            self._conn.commit()
        self._total += len(batch)

    def _drain(self, **p):
        self._flush()
        return self._total


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    d = tempfile.mkdtemp(prefix='dbpath_')
    db_path = os.path.join(d, 'u.db')

    a, b = mpc.Pipe(duplex=True)
    host = JsonRpcConnection(a)
    core = JsonRpcConnection(b)
    conn = _mk_db(db_path)
    CoreSide(conn, core)
    host.start()
    core.start()

    rows = [{'name': 'user%d' % i, 'n': i} for i in range(N)]

    def report(name, dt):
        print(f"{name:34s} {N} 次  {dt*1000:8.1f} ms  {dt/N*1e6:7.1f} us/次  {N/dt:9.0f} 次/s",
              flush=True)

    # A 现状：同步 RPC 逐条（核心每条 INSERT+commit）
    t0 = time.perf_counter()
    for r in rows:
        host.call('db.insert', r)
    report("A 同步 RPC 逐条（现状）", time.perf_counter() - t0)

    # C 对照：同步 RPC 但宿主攒批（每 BATCH 一次 insert_many）
    t0 = time.perf_counter()
    for k in range(0, N, BATCH):
        host.call('db.insert_many', {'rows': [(r['name'], r['n']) for r in rows[k:k + BATCH]]})
    report("C 同步 RPC + 宿主攒批", time.perf_counter() - t0)

    # B 方向：异步广播逐条 + 核心攒批（fire-and-forget，最后 drain 等落库）
    t0 = time.perf_counter()
    for r in rows:
        host.notify('db.write', r)
    done = host.call('drain')
    report("B 异步广播逐条 + 核心攒批", time.perf_counter() - t0)

    # D 理想：批量传输 + 异步（宿主攒 BATCH 条 → 一次 notify 批量帧 → 核心 executemany）
    t0 = time.perf_counter()
    for k in range(0, N, BATCH):
        host.notify('db.write_batch',
                    {'rows': [(r['name'], r['n']) for r in rows[k:k + BATCH]]})
    done = host.call('drain')
    report("D 异步批量广播 + 核心攒批（理想）", time.perf_counter() - t0)
    assert done >= 4 * N, f"落库计数异常: {done}"

    host.close()
    core.close()
    conn.close()
    print(f"\n(落库总计 {done} 条；DB: {db_path})", flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
