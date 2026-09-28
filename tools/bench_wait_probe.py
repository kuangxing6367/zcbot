# -*- coding: utf-8 -*-
"""定位 bench_dispatch 中 fw_end2end 异常慢的原因：分段计时 put 与 await done。
用法：python tools/bench_wait_probe.py [N]
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


def _write_cfg(tmp, workers=1):
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
            f"event_queue:\n"
            f"  workers: {workers}\n"
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

    tmp = tempfile.mkdtemp(prefix='zcprobe_')
    fw = Framework(config_path=_write_cfg(tmp), role='standard')
    await fw.start(wait_ready=True)

    t_put = t_done = 0.0
    n_put = max_wait = 0
    tp = time.perf_counter()
    for i in range(N):
        t0 = time.perf_counter()
        ev = _make_event(i)
        done = asyncio.get_running_loop().create_future()
        await fw._event_buffer.put(ev, done)
        t1 = time.perf_counter()
        await done
        t2 = time.perf_counter()
        t_put += (t1 - t0)
        t_done += (t2 - t1)
        max_wait = max(max_wait, t2 - t1)
        n_put += 1
    loop_elapsed = time.perf_counter() - tp
    print(f"N={N} 循环耗时 {loop_elapsed*1000:.1f}ms  每条 {loop_elapsed/N*1e6:.1f}us")
    print(f"  put 段   合计 {t_put*1000:6.1f}ms  每条 {t_put/N*1e6:6.1f}us")
    print(f"  await done 合计 {t_done*1000:6.1f}ms  每条 {t_done/N*1e6:6.1f}us  最大 {max_wait*1e6:.1f}us")
    print(f"  buffer L1 当前 bytes={fw._event_buffer._l1_bytes} rows 内 {fw._event_buffer._l1.qsize() if hasattr(fw._event_buffer._l1,'qsize') else '?'}")
    await fw.stop()


if __name__ == '__main__':
    asyncio.run(main())