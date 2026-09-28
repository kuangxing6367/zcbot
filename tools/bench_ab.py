# -*- coding: utf-8 -*-
"""A/B 对照：找出 bench_dispatch 的 fw_end2end 2ms/事件 的干扰因素。
假说：asyncio.run(feed()) 多层 loop 嵌套 / wait_for 包一层 / 前置 event_bus 测试污染。
用法：python tools/bench_ab.py [N]
"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000


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


def _make_event(i: int):
    return {
        'type': 'message',
        'post_type': 'message',
        'message_type': 'group',
        'user_id': 10000 + i,
        'group_id': 20000,
        'message_id': i,
        'message': [{'type': 'text', 'data': {'text': f'/ping hello world {i}'}}],
        'sender': {'user_id': 10000 + i, 'nickname': 'bench', 'role': 'member'},
        'bot_name': 'bench_bot',
        'adapter': 'onebot',
    }


async def _feed(fw):
    for i in range(N):
        await fw.dispatch_event(_make_event(i), wait=True)


async def single_loop():
    """单一事件循环内直跑"""
    from framework.core import Framework
    tmp = tempfile.mkdtemp(prefix='zcab_')
    fw = Framework(config_path=_write_cfg(tmp), role='standard')
    await fw.start(wait_ready=True)
    t0 = time.perf_counter()
    await _feed(fw)
    dt = time.perf_counter() - t0
    await fw.stop()
    return dt


def with_wait_for():
    """asyncio.run 内 wait_for 包一层（复刻 bench_dispatch 结构，无前置测试）"""
    from framework.core import Framework

    async def inner():
        tmp = tempfile.mkdtemp(prefix='zcab_')
        fw = Framework(config_path=_write_cfg(tmp), role='standard')
        await fw.start(wait_ready=True)

        async def feed():
            await _feed(fw)
        try:
            await asyncio.wait_for(feed(), timeout=300)
        finally:
            await fw.stop()

    t0 = time.perf_counter()
    asyncio.run(inner())
    return time.perf_counter() - t0


def with_prior_buses():
    """先跑 event_bus 测试再跑 fw（复刻 bench_dispatch 完整顺序）"""
    from framework.core import Framework
    from framework.messaging.event_bus import EventBus

    async def inner():
        # 前置：event_bus 测试（与 bench_dispatch 相同）
        bus = EventBus()
        for i in range(10):
            async def h(p, _i=i):  # noqa: ARG001
                return None
            bus.subscribe('message', f'p{i}', h)
        ev = {'type': 'message'}
        for _ in range(2000):
            await bus.aemit('message', ev)

        tmp = tempfile.mkdtemp(prefix='zcab_')
        fw = Framework(config_path=_write_cfg(tmp), role='standard')
        await fw.start(wait_ready=True)
        t0 = time.perf_counter()
        await _feed(fw)
        dt = time.perf_counter() - t0
        await fw.stop()
        return dt

    return asyncio.run(inner())


if __name__ == '__main__':
    print(f"===== A/B 对照 N={N} =====")
    for name, fn in [('single_loop 直跑', single_loop),
                     ('wait_for 包裹', with_wait_for),
                     ('前置event_bus再跑', with_prior_buses)]:
        dt = asyncio.run(fn()) if asyncio.iscoroutinefunction(fn) else fn()
        print(f"  {name:<18} {dt*1000:8.1f} ms  每条 {dt/N*1e6:7.1f} us  ({N/dt:.0f} ev/s)")