"""调度器优雅停机（Gap 9）回归测试。

铁律：不碰真实 config、不依赖网络；事件循环跑在后台线程。
直接运行：PYTHONPATH=. python tests/test_scheduler_graceful.py
"""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.scheduler import TaskScheduler, resolve_timezone


def _make_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True, name='test-loop')
    t.start()
    return loop, t


def _spawn_task(loop, coro):
    """在指定 loop 上创建一个 asyncio.Task 并返回它（跨线程安全拿到引用）。"""
    async def _wrap():
        return asyncio.ensure_future(coro)
    return asyncio.run_coroutine_threadsafe(_wrap(), loop).result()


class _FakeFW:
    def __init__(self, loop, cfg=None):
        self.loop = loop
        self.config = {'scheduler': cfg or {}}


def check(name, cond, detail=''):
    print(('通过 ' if cond else '失败 ') + name + (('：' + detail) if detail else ''))
    if not cond:
        raise SystemExit('断言失败：' + name)


def test_resolve_drain_timeout():
    loop, lt = _make_loop()
    try:
        # 单任务超时 > 30 → 取 30
        s = TaskScheduler(_FakeFW(loop, {'task_timeout': 300}))
        check('drain 默认上限封顶 30s（task_timeout 大）', s._resolve_drain_timeout(None) == 30.0)
        # 单任务超时 < 30 → 取它
        s = TaskScheduler(_FakeFW(loop, {'task_timeout': 10}))
        check('drain 跟随短 task_timeout', s._resolve_drain_timeout(None) == 10.0)
        # 显式参数优先
        s = TaskScheduler(_FakeFW(loop, {}))
        check('drain 显式参数优先', s._resolve_drain_timeout(5) == 5.0)
        # 配置 stop_drain_timeout 生效
        s = TaskScheduler(_FakeFW(loop, {'stop_drain_timeout': 7}))
        check('drain 读配置 stop_drain_timeout', s._resolve_drain_timeout(None) == 7.0)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        lt.join(timeout=2)


def test_graceful_drain_completes():
    """在途短任务应被 drain 完（stop 不超时、不丢任务）。"""
    loop, lt = _make_loop()
    try:
        s = TaskScheduler(_FakeFW(loop, {}))
        done = {'v': False}

        async def _job():
            await asyncio.sleep(0.2)
            done['v'] = True
        task = _spawn_task(loop, _job())
        with s._inflight_lock:
            s._inflight.add(task)

        start = time.monotonic()
        s.stop(graceful=True, drain_timeout=3)
        elapsed = time.monotonic() - start

        check('在途任务被 drain 完成', done['v'] is True)
        check('drain 在超时内返回（未误判超时）', elapsed < 2, f'{elapsed:.2f}s')
    finally:
        loop.call_soon_threadsafe(loop.stop)
        lt.join(timeout=2)


def test_graceful_abandon_on_timeout():
    """drain_timeout=0 时，长任务应被放弃（stop 快速返回，不阻塞 5s）。"""
    loop, lt = _make_loop()
    try:
        s = TaskScheduler(_FakeFW(loop, {}))
        done = {'v': False}

        async def _long():
            await asyncio.sleep(5)
            done['v'] = True
        task = _spawn_task(loop, _long())
        with s._inflight_lock:
            s._inflight.add(task)

        start = time.monotonic()
        s.stop(graceful=True, drain_timeout=0)
        elapsed = time.monotonic() - start

        check('超时时 stop 不阻塞（<3s 即返回）', elapsed < 3, f'{elapsed:.2f}s')
        check('超时后在途任务仍在后台跑（未被强杀）', done['v'] is False)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        lt.join(timeout=6)


def test_ungraceful_no_block():
    """graceful=False 等同旧行为：立即关，不 await 在途。"""
    loop, lt = _make_loop()
    try:
        s = TaskScheduler(_FakeFW(loop, {}))
        done = {'v': False}

        async def _long():
            await asyncio.sleep(5)
            done['v'] = True
        task = _spawn_task(loop, _long())
        with s._inflight_lock:
            s._inflight.add(task)

        start = time.monotonic()
        s.stop(graceful=False)
        elapsed = time.monotonic() - start
        check('非优雅停机瞬时返回', elapsed < 1, f'{elapsed:.2f}s')
    finally:
        loop.call_soon_threadsafe(loop.stop)
        lt.join(timeout=6)


def test_stop_idempotent_and_no_inflight():
    """无在途任务时 stop 不卡、可重复调用。"""
    loop, lt = _make_loop()
    try:
        s = TaskScheduler(_FakeFW(loop, {}))
        t0 = time.monotonic()
        s.stop()
        s.stop(graceful=True, drain_timeout=2)  # 二次调用不卡
        elapsed = time.monotonic() - t0
        check('无在途时两次 stop 在 1s 内返回（不阻塞、不空等 drain）',
              elapsed < 1.0, f'elapsed={elapsed:.2f}s')
    finally:
        loop.call_soon_threadsafe(loop.stop)
        lt.join(timeout=2)


def run_all():
    test_resolve_drain_timeout()
    test_graceful_drain_completes()
    test_graceful_abandon_on_timeout()
    test_ungraceful_no_block()
    test_stop_idempotent_and_no_inflight()
    print('\n全部通过：调度器优雅停机 5 组')


if __name__ == '__main__':
    run_all()
