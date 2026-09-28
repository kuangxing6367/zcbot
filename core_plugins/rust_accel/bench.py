# -*- coding: utf-8 -*-
"""rust_accel 端到端性能基准（与 Python 基线同口径对照）

用量与 tools/bench_dispatch.py / tools/bench_workers.py 一致（N 缺省 2000）：
  A. 事件注入端到端：WS 注入 N 条 → Rust 解析 + IPC → Python dispatch 落点
     （等价口径见 bench_dispatch: fw_end2end / bench_workers: 稳态转发）
  B. 广播 acall 往返：Python acall → IPC → Rust 写 WS → echo 回帧 → call_resp
     并发 N 次测吞吐；另串行采样 100 次测单次端到端延迟

用法：python core_plugins/rust_accel/bench.py [N]
"""
import asyncio
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import websockets  # noqa: E402

from core_plugins.rust_accel.main import RustAccelService  # noqa: E402


class FakeFW:
    def __init__(self):
        self.config = {'rust_accel': {}, 'core_plugins': {}, 'onebot': {}}
        self.loop = None
        self.events = []

    async def dispatch_event(self, event, wait=False):
        self.events.append(event)


def _ev(i):
    return json.dumps({
        "post_type": "message", "message_type": "group", "group_id": 1,
        "user_id": 1000 + i,
        "message": [{"type": "text", "data": {"text": f"msg-{i}"}}],
        "sender": {"user_id": 1000 + i}})


async def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    BIN = os.environ.get('RUST_ACCEL_BIN',
                         r"C:/rust_accel_target/release/rust_accel.exe")
    fw = FakeFW()
    fw.loop = asyncio.get_running_loop()
    cfg = {'enabled': True, 'ws_host': '127.0.0.1', 'ws_port': 0,
           'stats_interval_secs': 1, 'binary_path': BIN, 'access_token': ''}
    fw.config['rust_accel'] = cfg
    svc = RustAccelService(fw, cfg)
    assert svc.start() is True, "start() 应返回 True"
    try:
        async with websockets.connect(
                f"ws://127.0.0.1:{svc.ws_port}/",
                additional_headers={"X-Self-ID": "bench1"},
                max_size=32 * 1024 * 1024) as ws:
            for _ in range(50):
                if 'bench1' in svc.get_connected_bots():
                    break
                await asyncio.sleep(0.1)
            assert svc.get_connected_bots(), "WS 客户端未连上"

            # ── A. 事件注入端到端（WS -> Rust -> IPC -> Python dispatch） ──
            t0 = time.perf_counter()
            for i in range(N):
                await ws.send(_ev(i))
            while len(fw.events) < N:
                await asyncio.sleep(0.001)
            dt = (time.perf_counter() - t0) * 1000
            print(f"A 事件注入端到端  {N} 条  总耗时 {dt:8.1f} ms  "
                  f"吞吐 {N / (dt / 1000):10.0f} ev/s  单事件 {dt * 1000 / N:6.1f} us")

            # ── B1. 广播 acall 并发吞吐（含 WS echo 回执） ──
            async def echo_loop(count):
                for _ in range(count):
                    frame = json.loads(await ws.recv())
                    await ws.send(json.dumps(
                        {"status": "ok", "retcode": 0, "echo": frame['echo']}))

            t0 = time.perf_counter()
            et = asyncio.create_task(echo_loop(N))
            resps = await asyncio.gather(*[
                svc.acall('send_group_msg', group_id=1, message=f'm-{i}')
                for i in range(N)])
            await et
            dt = (time.perf_counter() - t0) * 1000
            ok = sum(1 for r in resps if r.get('ok'))
            print(f"B 广播并发 acall  {N} 条  总耗时 {dt:8.1f} ms  "
                  f"吞吐 {N / (dt / 1000):10.0f} qps  成功 {ok}/{N}")

            # ── B2. 串行 acall 延迟采样（100 次） ──
            et = asyncio.create_task(echo_loop(100))
            delays = []
            for i in range(100):
                t0 = time.perf_counter()
                r = await svc.acall('send_group_msg', group_id=1, message='d')
                delays.append((time.perf_counter() - t0) * 1e6)
                assert r.get('ok'), r
            await et
            delays.sort()
            p50, p95 = delays[50], delays[95]
            print(f"C 广播串行延迟(100) P50 {p50:7.0f} us  P95 {p95:7.0f} us  "
                  f"均值 {statistics.mean(delays):7.0f} us")

        print("RUST_ACCEL_BENCH_PASS")
    finally:
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())