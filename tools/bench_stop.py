# -*- coding: utf-8 -*-
"""定位 fw.stop() 20 秒卡点。用法：python tools/bench_stop.py [N]"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _write_cfg(tmp):
    cfg = os.path.join(tmp, 'bench.yaml')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "platform: zcbot\n"
            "database:\n"
            "  type: sqlite\n"
            f"  path: {os.path.join(tmp, 'bench.db')}\n"
            "log:\n"
            "  level: ERROR\n"
            "web:\n"
            "  host: 127.0.0.1\n"
            "  port: 0\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
            "event_queue:\n"
            "  workers: 1\n"
        )
    return cfg


def step(name, dt):
    print(f"    [{name}] {dt*1000:8.1f} ms")


async def main():
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcstop_')
    fw = Framework(config_path=_write_cfg(tmp), role='standard')
    t0 = time.perf_counter()
    await fw.start(wait_ready=True)
    t1 = time.perf_counter()
    step('start', t1 - t0)

    orig_wait_drained = fw._event_buffer.wait_drained

    async def spy_wait_drained(timeout):
        t = time.perf_counter()
        r = await orig_wait_drained(timeout)
        print(f"    [stop] wait_drained 实际耗时 {(time.perf_counter()-t)*1000:.1f} ms，返回 {r}")
        return r
    fw._event_buffer.wait_drained = spy_wait_drained

    orig_trigger = fw.hooks.trigger_async
    async def spy_trigger(*a, **kw):
        t = time.perf_counter()
        r = await orig_trigger(*a, **kw)
        if a and isinstance(a[0], str) and 'SHUTDOWN' in str(a[0]):
            print(f"    [stop] LIFECYCLE_SHUTDOWN hooks 耗时 {(time.perf_counter()-t)*1000:.1f} ms")
        return r
    fw.hooks.trigger_async = spy_trigger

    t0 = time.perf_counter()
    await fw.stop()
    t1 = time.perf_counter()
    step('stop 总计', t1 - t0)
    print(f"  stop 总耗时 {t1-t0:.3f} s")


if __name__ == '__main__':
    asyncio.run(main())