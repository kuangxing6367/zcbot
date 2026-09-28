# -*- coding: utf-8 -*-
"""框架端到端性能基准（与 NoneBot 式分发模型同口径对比）

同口径定义：都是「收到 N 条 message 事件 → 空插件/空 handler 场景」，
测每事件固定框架开销（吞吐 ev/s + 单事件端到端耗时 us）。

场景：
  1. pure_await   NoneBot 式：asyncio 直接 await 空 handler（无中间队列/线程交换）
  2. event_bus    EventBus.aemit 空订阅 / 10 订阅（无操作 handler）
  3. buffer_put   EventBuffer 入队吞吐（put 直返 + worker 消费）
  4. fw_end2end   真实 Framework：dispatch_event 端到端（含 hooks/Event 构造/router 空路由）

指标：总耗时、吞吐(ev/s)、单事件均耗时(us)。
用法：python tools/bench_dispatch.py [N]
"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10000


def bench(name, coro):
    t0 = time.perf_counter()
    asyncio.run(coro())
    dt = (time.perf_counter() - t0) * 1e6
    # dt 单位为 us；打印统一转为 ms
    print(f"{name:<18} {N:>7} 条  总耗时 {dt/1e3:8.3f} ms  "
          f"吞吐 {N/(dt/1e6):8.0f} ev/s  单事件 {dt/N:7.1f} us")


async def _pure_await():
    """NoneBot 式基线：空 async handler 直接 await"""
    async def handler(ev):  # noqa: ARG001
        return None
    for _ in range(N):
        await handler({'type': 'message'})


async def _event_bus_empty():
    from framework.messaging.event_bus import EventBus
    bus = EventBus()
    ev = {'type': 'message'}
    for _ in range(N):
        await bus.aemit('msg.never_subscribed', ev)


async def _event_bus_10subs():
    from framework.messaging.event_bus import EventBus
    bus = EventBus()
    for i in range(10):
        async def h(p, _i=i):  # noqa: ARG001
            return None
        bus.subscribe('message', f'p{i}', h)
    ev = {'type': 'message'}
    for _ in range(N):
        await bus.aemit('message', ev)


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


async def _fw_end2end():
    """真实 Framework 端到端：dispatch_event → 缓冲 → worker → 空路由 → 完成"""
    from framework.core import Framework

    t_c = time.perf_counter()
    tmp = tempfile.mkdtemp(prefix='zcbench_')
    cfg = _write_cfg(tmp)
    fw = Framework(config_path=cfg, role='standard')
    t_s = time.perf_counter()
    await fw.start(wait_ready=True)
    t_e = time.perf_counter()

    # 直接循环 dispatch+wait，不包 wait_for（wait_for 会产生额外调度开销污染测量）
    # wait=True 语义做端到端（入队 → worker 处理 → future 完成）
    for i in range(N):
        await fw.dispatch_event(_make_event(i), wait=True)
    t_f = time.perf_counter()

    await fw.stop()
    t_stop = time.perf_counter()
    print(f"  [fw_end2end 细分] 构造 {(t_s-t_c)*1000:.1f}ms start {(t_e-t_s)*1000:.1f}ms "
          f"循环 {(t_f-t_e)*1000:.1f}ms（每条 {(t_f-t_e)/N*1e6:.1f}us）stop {(t_stop-t_f)*1000:.1f}ms")


if __name__ == '__main__':
    print(f"===== 框架端到端基准（同口径：空 handler / 空路由，N={N}）=====")
    bench('pure_await(NoneBot式)', _pure_await)
    bench('event_bus_空订阅', _event_bus_empty)
    bench('event_bus_10订阅', _event_bus_10subs)
    bench('fw_end2end(1worker)', _fw_end2end)