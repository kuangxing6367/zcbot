# -*- coding: utf-8 -*-
"""实测：1MB 事件包在 l1_max_bytes=512KB 下必然溢出，验证落层与 IO 耗时。"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from framework.core.event_buffer import EventBuffer


class BigEvent:
    def __repr__(self):
        return '{"event":"test","data":"' + 'A' * (1024 * 1024) + '"}'


async def probe(use_sqlite, repeat=5):
    tmp = tempfile.mkdtemp()
    cfg = {
        'maxsize': 2000,
        'buffer': {
            'l1_max_bytes': 512 * 1024,
            'sqlite_enabled': use_sqlite,
            'sqlite_path': os.path.join(tmp, 'event_buffer.db'),
            'sqlite_write_timeout': 0.5,
            'l3_max_bytes': 4 * 1024 * 1024,
        },
    }
    buf = EventBuffer(cfg, tmp)
    ev = BigEvent()
    size = buf._size_of(ev)
    print(f"sqlite={use_sqlite}: _size_of(1MB)={size} B  L1上限={buf.l1_max_bytes} B")

    # 入队耗时（每条）
    costs = []
    for i in range(repeat):
        t0 = time.perf_counter()
        ok = await buf.put(ev, None)
        costs.append((time.perf_counter() - t0) * 1000)
        # 取出，避免 L3 填满
        if not buf._l3.empty():
            buf._l3.get_nowait()
    avg = sum(costs) / len(costs)
    stats = buf.stats()
    print(f"  avg put 耗时: {avg:.2f} ms/条  溢出sqlite={stats.get('overflow_to_sqlite')} "
          f"L3={stats.get('l3_items')} dropped={stats.get('dropped')}")
    if use_sqlite:
        print(f"  sqlite 行数: {buf.sqlite_count()}  sqlite文件: {os.path.getsize(buf.sqlite_path)} B")
    buf.close()
    return avg


async def main():
    print("===== A. 当前配置 sqlite_enabled=false =====")
    await probe(False)
    print("===== B. 若开启 sqlite_enabled=true =====")
    await probe(True)


asyncio.run(main())