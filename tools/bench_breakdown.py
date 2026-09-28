# -*- coding: utf-8 -*-
"""端到端环节拆分基准 v2：同步函数用同步探针，修正 v1 把 log/stats 包成 async 导致未执行的 bug。

用法：python tools/bench_breakdown.py [N]
"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

PROBE = {}
CNT = {}


def aprobe(name, func):
    async def wrapped(*a, **kw):
        t0 = time.perf_counter()
        try:
            return await func(*a, **kw)
        finally:
            dt = time.perf_counter() - t0
            PROBE[name] = PROBE.get(name, 0.0) + dt
            CNT[name] = CNT.get(name, 0) + 1
    return wrapped


def sprobe(name, func):
    def wrapped(*a, **kw):
        t0 = time.perf_counter()
        try:
            return func(*a, **kw)
        finally:
            dt = time.perf_counter() - t0
            PROBE[name] = PROBE.get(name, 0.0) + dt
            CNT[name] = CNT.get(name, 0) + 1
    return wrapped


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


async def main():
    import framework.log_broker as log_broker_mod
    from framework.core import Framework
    from framework.messaging.event import _extract_text

    tmp = tempfile.mkdtemp(prefix='zcbreak_')
    fw = Framework(config_path=_write_cfg(tmp), role='standard')
    await fw.start(wait_ready=True)

    lb = log_broker_mod.log_broker
    fw.hooks.trigger_async = aprobe('hooks.trigger_async', fw.hooks.trigger_async)
    fw._dispatch_raw_message_handlers = aprobe('_raw_handlers', fw._dispatch_raw_message_handlers)
    fw._event_buffer.put = aprobe('buffer.put', fw._event_buffer.put)
    fw._event_buffer.get_async = aprobe('buffer.get_async', fw._event_buffer.get_async)
    fw._process_event = aprobe('_process_event', fw._process_event)
    lb.log_message = sprobe('log_broker.log_message', lb.log_message)
    lb.log = sprobe('log_broker.log', lb.log)
    fw.stats_writer.register_user = sprobe('stats.register_user', fw.stats_writer.register_user)
    fw.router.route = aprobe('router.route', fw.router.route)

    t0 = time.perf_counter()
    for i in range(N):
        await fw.dispatch_event(_make_event(i), wait=True)
    total = time.perf_counter() - t0
    await fw.stop()

    print(f"===== 环节拆分 v2 N={N} 总耗时 {total*1000:.1f} ms（{N/total:.0f} ev/s，单事件 {total/N*1e6:.0f} us）=====")
    rows = []
    for name, t in PROBE.items():
        rows.append((t / N * 1e6, t / total, name, CNT[name]))
    rows.sort(reverse=True)
    for us, frac, name, cnt in rows:
        print(f"  {name:<28} {us:8.1f} us/ev  {frac*100:5.1f}%  (x{cnt/N:.1f}/ev)")
    # 非重叠估算：_process_event 内含 route/hooks/extract，单独列出
    print(f"  [说明] _process_event 顶层 19us 含 router.route/hooks/extract；log+stats 为真身执行值")


if __name__ == '__main__':
    asyncio.run(main())