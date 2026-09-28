# -*- coding: utf-8 -*-
"""cProfile 定位 bench_dispatch 的 fw_end2end 为何 2108us/事件（vs 探针 30us）。
用法：python tools/bench_profile.py [N]
"""
import asyncio
import cProfile
import os
import pstats
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1500


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
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcprof_')
    fw = Framework(config_path=_write_cfg(tmp), role='standard')
    await fw.start(wait_ready=True)

    def run():
        async def feed():
            for i in range(N):
                await fw.dispatch_event(_make_event(i), wait=True)
        return feed()

    t0 = time.perf_counter()
    pr = cProfile.Profile()
    pr.enable()
    await run()
    pr.disable()
    dt = time.perf_counter() - t0
    await fw.stop()
    print(f"===== cProfile N={N} 总耗时 {dt*1000:.1f} ms（{N/dt:.0f} ev/s，单事件 {dt/N*1e6:.0f} us）=====")
    ps = pstats.Stats(pr)
    ps.strip_dirs().sort_stats('cumulative').print_stats(28)


if __name__ == '__main__':
    asyncio.run(main())