# -*- coding: utf-8 -*-
"""稳态吞吐基准：批量注入 N 条（不逐条 wait），对比 event_queue.workers = 1/2/4。
用法：python tools/bench_workers.py [N]"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000


def _write_cfg(tmp, workers):
    cfg = os.path.join(tmp, 'bench.yaml')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "platform: zcbot\n"
            "database:\n"
            "  type: sqlite\n"
            f"  path: {os.path.join(tmp, 'bench.db')}\n"
            "log:\n"
            "  level: ERROR\n"
            "  log_raw_message: false\n"
            "web:\n"
            "  host: 127.0.0.1\n"
            "  port: 0\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
            "event_queue:\n"
            f"  workers: {workers}\n"
        )
    return cfg


async def run_once(workers: int):
    from framework.core import Framework
    tmp = tempfile.mkdtemp(prefix='zcw_')
    fw = Framework(config_path=_write_cfg(tmp, workers), role='standard')
    await fw.start(wait_ready=True)

    ev = {
        'type': 'message', 'message_type': 'group', 'user_id': 1, 'group_id': 1,
        'message_id': 1, 'message': 'hello', 'sender': {'nickname': 'u'},
        'bot_name': 'default',
    }
    # 批量注入：不逐条 wait（真实生产形态：WS 接收循环连续入队）
    t0 = time.perf_counter()
    for _ in range(N):
        await fw.dispatch_event(ev)
    t_inject = time.perf_counter() - t0

    # 等待缓冲排空（worker 处理完）
    await fw._event_buffer.wait_drained(30)
    t_done = time.perf_counter()
    total = t_done - t0
    print(f"  workers={workers}: 注入 {t_inject*1000:.1f}ms + 处理至排空 {total*1000:.1f}ms "
          f"→ 吞吐 {N/total:,.0f} ev/s（均摊 {total/N*1e6:.1f} us/ev）")

    t_stop = time.perf_counter()
    await fw.stop()
    print(f"    stop: {(time.perf_counter()-t_stop)*1000:.1f} ms")


async def main():
    for w in (1, 2, 4):
        await run_once(w)
        print()


if __name__ == '__main__':
    asyncio.run(main())