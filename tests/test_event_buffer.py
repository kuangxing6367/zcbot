# -*- coding: utf-8 -*-
"""EventBuffer 三层缓冲单元测试（v1.7.1 热路径修复防回归：
记账单次化 / sqlite 空转轮询消除 / 重启遗留承接 / 全满丢弃）"""
import asyncio

import pytest

from framework.core.event_buffer import EventBuffer


def _cfg(**over):
    buf = {'l1_max_bytes': 512 * 1024, 'sqlite_enabled': True}
    buf.update(over)
    return {'maxsize': 2000, 'buffer': buf}


def _ev(i=0, text='hello'):
    return {'type': 'message', 'bot_name': 'b', 'user_id': 1,
            'message': text, 'raw_message': text, 'seq': i}


def test_put_get_roundtrip_and_accounting(tmp_path):
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        for i in range(10):
            assert await buf.put(_ev(i), None) is True
        assert buf.stats()['l1_items'] == 10
        assert buf.stats()['l1_bytes'] > 0
        got = []
        while buf.stats()['l1_items']:
            ev, done, src = await buf.get_async()
            buf.task_done(src)
            got.append(ev['seq'])
        assert got == list(range(10))
        # 出队复用入队时携带的尺寸：记账必须精确清零、sqlite 不留积压
        assert buf.stats()['l1_bytes'] == 0
        assert buf.stats()['sqlite_pending'] == 0
        buf.close()
    asyncio.run(run())


def test_wait_true_resolves_done(tmp_path):
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        done = asyncio.get_running_loop().create_future()
        assert await buf.put(_ev(), done) is True
        ev, d, src = await buf.get_async()
        assert src == 'l1' and not d.done()
        d.set_result(True)
        buf.task_done(src)
        assert done.done()
        buf.close()
    asyncio.run(run())


def test_overflow_to_sqlite_and_drain(tmp_path):
    async def run():
        buf = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))  # 极小 L1 逼溢出
        for i in range(20):
            assert await buf.put(_ev(i, 'x' * 200), None) is True
        st = buf.stats()
        assert st['overflow_to_sqlite'] > 0
        assert st['sqlite_pending'] == st['overflow_to_sqlite']
        n = 0
        while st['l1_items'] or st['sqlite_pending']:
            ev, done, src = await buf.get_async()
            buf.task_done(src)
            n += 1
            st = buf.stats()
        assert n == 20
        # 全部消化后计数归零 → get_async 不再对 sqlite 发起任何查询
        assert buf.stats()['sqlite_pending'] == 0
        buf.close()
    asyncio.run(run())


def test_restart_inherits_persisted_rows(tmp_path):
    async def run():
        b1 = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))
        for i in range(5):
            await b1.put(_ev(i, 'y' * 300), None)
        n_left = b1.stats()['sqlite_pending']
        assert n_left > 0
        b1.close()

        b2 = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))
        assert b2.stats()['sqlite_pending'] == n_left   # 启动时清点上次进程遗留
        drained = 0
        while b2.stats()['sqlite_pending'] > 0:
            ev, done, src = await b2.get_async()
            b2.task_done(src)
            drained += 1
        assert drained == n_left
        b2.close()
    asyncio.run(run())


def test_no_sqlite_poll_when_counter_zero(tmp_path):
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        await buf.put(_ev(), None)
        ev, done, src = await buf.get_async()
        buf.task_done(src)
        # 全空且 sqlite 计数为 0 → get_async 阻塞在等待信号（不被空查询打断），超时即证明
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(buf.get_async(), timeout=0.15)
        buf.close()
    asyncio.run(run())


def test_sqlite_overflow_wakes_blocked_consumer(tmp_path):
    """复现压测 probe 场景：消费者睡在空 L1 上，超大事件落 sqlite 必须把它唤醒。
    修复前 worker 阻塞在 L1.get()，溢出事件静默滞留到下一条小消息才被顺带吐出。"""
    async def run():
        buf = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))
        sleeper = asyncio.create_task(buf.get_async())
        await asyncio.sleep(0.05)                 # 让消费者先睡进去
        # 1MB 级事件进不了 600B 的 L1 → 落 sqlite；阻塞中的消费者应被信号唤醒
        assert await buf.put(_ev(0, 'x' * 2000), None) is True
        ev, done, src = await asyncio.wait_for(sleeper, timeout=2.0)
        assert src == 'sqlite' and ev['seq'] == 0
        buf.task_done(src)
        assert buf.stats()['sqlite_pending'] == 0
        buf.close()
    asyncio.run(run())


def test_l3_overflow_wakes_blocked_consumer(tmp_path):
    """L3 兜底分支同样必须唤醒（CI 3.10 曾因 sqlite 写超时走 L3 而漏信号）：
    关掉 sqlite，超大事件只能落 L3，睡着的消费者必须被唤醒。"""
    async def run():
        buf = EventBuffer({'maxsize': 2000,
                           'buffer': {'l1_max_bytes': 600, 'sqlite_enabled': False}},
                          str(tmp_path))
        sleeper = asyncio.create_task(buf.get_async())
        await asyncio.sleep(0.05)
        assert await buf.put(_ev(0, 'x' * 2000), None) is True   # 只能落 L3
        ev, done, src = await asyncio.wait_for(sleeper, timeout=2.0)
        assert src == 'l3' and ev['seq'] == 0
        buf.task_done(src)
        buf.close()
    asyncio.run(run())


def test_overflow_then_small_event_order(tmp_path):
    """probe 完整时序：大事件先落 sqlite，小事件后进 L1 → 大事件不再"有回显时有时无"。"""
    async def run():
        buf = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))
        assert await buf.put(_ev(0, 'x' * 2000), None) is True   # → sqlite
        assert await buf.put(_ev(1, 'hi'), None) is True          # → L1
        got = []
        for _ in range(2):
            ev, done, src = await asyncio.wait_for(buf.get_async(), timeout=2.0)
            buf.task_done(src)
            got.append((src, ev['seq']))
        assert got == [('l1', 1), ('sqlite', 0)]                  # 优先级 L1→sqlite
        buf.close()
    asyncio.run(run())


def test_drop_when_all_full(tmp_path):
    async def run():
        size = EventBuffer._size_of(_ev(0, 'a' * 300))
        buf = EventBuffer({'maxsize': 1,
                           'buffer': {'l1_max_bytes': 10_000_000, 'sqlite_enabled': False,
                                      'l3_max_bytes': int(size * 1.5)}}, str(tmp_path))
        assert await buf.put(_ev(0, 'a' * 300), None) is True    # → L1（条数满）
        assert await buf.put(_ev(1, 'a' * 300), None) is True    # → L3（1.5x 容一条）
        assert await buf.put(_ev(2, 'a' * 300), None) is False   # L3 二条超限 → 丢弃
        assert buf.stats()['dropped'] == 1
        buf.close()
    asyncio.run(run())


def test_size_of_catches_heavy_tail():
    small = _ev()
    big = _ev(text='A' * 100_000)
    assert 0 < EventBuffer._size_of(small) < 4096
    assert EventBuffer._size_of(big) > 50_000       # base64 大图必须被记账抓住
    assert EventBuffer._size_of(object()) > 0       # 异常形状有保守回退
