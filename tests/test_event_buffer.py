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
        # 溢出先进 L4 写缓冲（不再是「L1 满直接落 sqlite」）
        st0 = buf.stats()
        assert st0['l4_items'] > 0 or st0['sqlite_pending'] > 0
        # 启动 L4 flush → 批量落 L2，验证持久化路径仍可达、计数一致
        buf.start_l4()
        await asyncio.sleep(0.2)
        await buf.stop_l4()
        st = buf.stats()
        assert st['overflow_to_sqlite'] > 0
        assert st['sqlite_pending'] == st['overflow_to_sqlite']
        n = 0
        while buf.stats()['l1_items'] or buf.stats()['sqlite_pending']:
            ev, done, src = await buf.get_async()
            buf.task_done(src)
            n += 1
        assert n == 20
        # 全部消化后计数归零 → get_async 不再对 sqlite 发起任何查询
        assert buf.stats()['sqlite_pending'] == 0
        buf.close()
    asyncio.run(run())


def test_restart_inherits_persisted_rows(tmp_path):
    async def run():
        # l1_max_bytes 须大于单事件 size，使其走正常路径（满则溢 L4 写缓冲）；
        # L4 由后台 flush 落 L2 sqlite，重启进程从 sqlite 承接上次遗留。
        b1 = EventBuffer(_cfg(l1_max_bytes=2500), str(tmp_path))
        for i in range(5):
            await b1.put(_ev(i, 'y' * 300), None)
        b1.start_l4()
        await asyncio.sleep(0.2)               # 等 L4 flush 落 L2，才能被重启承接
        n_left = b1.stats()['sqlite_pending']
        assert n_left > 0
        await b1.stop_l4()
        assert b1.stats()['sqlite_pending'] == n_left
        b1.close()

        b2 = EventBuffer(_cfg(l1_max_bytes=2500), str(tmp_path))
        assert b2.stats()['sqlite_pending'] == n_left   # 启动时清点上次进程遗留
        drained = 0
        while not b2.empty_all():                        # 回填 L1 后 pending 计数先归零，
            ev, done, src = await b2.get_async()         # 须排空内存层才能数全
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


def test_l4_overflow_wakes_blocked_consumer(tmp_path):
    """复现压测 probe 场景：消费者睡在空缓冲上，溢出事件落 L4 写缓冲、后台 flush 落
    L2 sqlite，必须把它唤醒。事件 ~4KB：进不了 600B 的 L1 → 落 L4 → flush → L2 sqlite，
    阻塞中消费者应被信号唤醒并从 L2 取回。"""
    async def run():
        buf = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))
        buf.start_l4()
        sleeper = asyncio.create_task(buf.get_async())
        await asyncio.sleep(0.05)                 # 让消费者先睡进去
        assert await buf.put(_ev(0, 'x' * 2000), None) is True  # → L4 → 唤醒消费者
        ev, done, src = await asyncio.wait_for(sleeper, timeout=2.0)
        # 消费者既可能直接从 L4 取走（后台 flush 尚未落盘），也可能从 L2 回取，二者皆合法
        assert src in ('l4', 'sqlite') and ev['seq'] == 0
        buf.task_done(src)
        assert buf.stats()['sqlite_pending'] == 0
        await buf.stop_l4()
        buf.close()
    asyncio.run(run())


def test_l3_overflow_wakes_blocked_consumer(tmp_path):
    """L3 兜底分支同样必须唤醒：关掉 sqlite，且 l4_max_bytes 极小（溢出事件装不下 L4），
    超大事件只能落 L3，睡着的消费者必须被唤醒。"""
    async def run():
        buf = EventBuffer({'maxsize': 2000,
                           'buffer': {'l1_max_bytes': 600, 'sqlite_enabled': False,
                                      'l4_max_bytes': 100}}, str(tmp_path))
        sleeper = asyncio.create_task(buf.get_async())
        await asyncio.sleep(0.05)
        assert await buf.put(_ev(0, 'x' * 2000), None) is True   # L1满→L4装不下→L2 off→L3
        ev, done, src = await asyncio.wait_for(sleeper, timeout=2.0)
        assert src == 'l3' and ev['seq'] == 0
        buf.task_done(src)
        buf.close()
    asyncio.run(run())


def test_overflow_then_small_event_order(tmp_path):
    """probe 完整时序：大事件先落 L4 写缓冲、flush 后回取；小事件后进 L1 → 大事件不再
    "有回显时有时无"。新契约下溢出先 L4、再由 flush 落 L2，消费顺序仍为 L1 优先。"""
    async def run():
        buf = EventBuffer(_cfg(l1_max_bytes=600), str(tmp_path))
        buf.start_l4()
        assert await buf.put(_ev(0, 'x' * 2000), None) is True   # 大 → L4
        assert await buf.put(_ev(1, 'hi'), None) is True          # → L1
        await asyncio.sleep(0.2)   # L4 flush 到 L2
        got = []
        for _ in range(2):
            ev, done, src = await asyncio.wait_for(buf.get_async(), timeout=2.0)
            buf.task_done(src)
            got.append((src, ev['seq']))
        assert got == [('l1', 1), ('sqlite', 0)]                  # 优先级 L1→L2
        await buf.stop_l4()
        buf.close()
    asyncio.run(run())


def test_drop_when_all_full(tmp_path):
    async def run():
        size = EventBuffer._size_of(_ev(0, 'a' * 300))
        # l2_backend=off：无 L2 可回落，L1/L4/L3 全满才丢弃。
        # l4_max_bytes=单事件尺寸：第1条进 L1，第2条进 L4（装1条），第3条 L4 满后回落 L3，
        # 第4条 L3 也满 → 丢弃。复现「所有层满 → 保老弃新」路径。
        buf = EventBuffer({'maxsize': 1,
                           'buffer': {'l1_max_bytes': 10_000_000, 'sqlite_enabled': False,
                                      'l2_backend': 'off',
                                      'l4_max_bytes': size,
                                      'l3_max_bytes': int(size * 1.5)}}, str(tmp_path))
        assert await buf.put(_ev(0, 'a' * 300), None) is True    # → L1（条数满）
        assert await buf.put(_ev(1, 'a' * 300), None) is True    # → L4（装 1 条）
        assert await buf.put(_ev(2, 'a' * 300), None) is True    # → L4 满 → L3
        assert await buf.put(_ev(3, 'a' * 300), None) is False   # 全满 → 丢弃
        assert buf.stats()['dropped'] == 1
        buf.close()
    asyncio.run(run())


def test_size_of_catches_heavy_tail():
    small = _ev()
    big = _ev(text='A' * 100_000)
    assert 0 < EventBuffer._size_of(small) < 4096
    assert EventBuffer._size_of(big) > 50_000       # base64 大图必须被记账抓住
    assert EventBuffer._size_of(object()) > 0       # 异常形状有保守回退


# ── 会话分片并行（v1.8.x：分发器 + 每 worker 分片队列）──────────

from framework.core.dispatch import FrameworkDispatchMixin


class _ShardStub(FrameworkDispatchMixin):
    """最小宿主：_event_distributor_loop / _shard_of / _event_pipeline_empty
    只依赖 _event_buffer / _shard_queues / _event_seq 三个实例字段（mixin 无 __init__）"""
    _event_buffer = None
    _shard_queues = None
    _event_seq = 0


def _msg_ev(group_id=None, user_id=None, seq=0):
    ev = {'type': 'message', 'bot_name': 'b', 'message': 'hi',
          'raw_message': 'hi', 'seq': seq}
    if group_id is not None:
        ev['group_id'] = group_id
    if user_id is not None:
        ev['user_id'] = user_id
    return ev


def test_shard_of_same_session_same_shard(tmp_path):
    stub = _ShardStub()
    stub._shard_queues = [None] * 4
    for _ in range(50):
        gid = 100000 + _
        assert stub._shard_of(_msg_ev(group_id=gid)) == \
            stub._shard_of(_msg_ev(group_id=gid))
        assert stub._shard_of(_msg_ev(user_id=gid)) == \
            stub._shard_of(_msg_ev(user_id=gid))
    # 不同会话应分布到多个分片（不完全坍缩）
    shards = {stub._shard_of(_msg_ev(group_id=200000 + i)) for i in range(50)}
    assert len(shards) > 1
    # 同余群号不得坍缩到同一分片（回归：裸 hash(int)=int，111/222/333 % 4 全等
    # 会让并行退化回单 worker）
    congruent = {stub._shard_of(_msg_ev(group_id=g))
                 for g in (111, 222, 333, 444, 555, 666, 777, 888)}
    assert len(congruent) > 1, congruent
    # 非法形状安全回退分片 0
    stub._shard_of(object())


def test_distributor_preserves_session_order_and_stamps_seq(tmp_path):
    """同会话事件 FIFO 保序 + 每事件盖内部唯一代号 _seq（轮转）"""
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        stub = _ShardStub()
        stub._event_buffer = buf
        stub._shard_queues = [asyncio.Queue() for _ in range(4)]
        groups = [11, 22, 33, 44]
        for i in range(40):  # 4 个群各 10 条，交错入队
            await buf.put(_msg_ev(group_id=groups[i % 4], seq=i), None)
        dist = asyncio.create_task(
            FrameworkDispatchMixin._event_distributor_loop(stub))
        await asyncio.sleep(0.1)
        dist.cancel()
        try:
            await dist
        except asyncio.CancelledError:
            pass
        assert buf.empty_all()  # 已全部出 L1
        # 按分片回收：同群事件必须保持入队顺序，_seq 已盖戳且全局唯一
        collected = {}
        all_seqs = []
        for q in stub._shard_queues:
            while not q.empty():
                ev, _done, _src = q.get_nowait()
                collected.setdefault(ev['group_id'], []).append(ev['seq'])
                assert isinstance(ev.get('_seq'), int)
                all_seqs.append(ev['_seq'])
        for g in groups:
            assert collected[g] == [i for i in range(40) if groups[i % 4] == g]
        assert len(set(all_seqs)) == len(all_seqs)  # _seq 唯一
        buf.close()
    asyncio.run(run())


def test_distributor_backpressure_not_lost(tmp_path):
    """分片队列满时分发器背压等待而非丢弃；最终全部可达"""
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        stub = _ShardStub()
        stub._event_buffer = buf
        stub._shard_queues = [asyncio.Queue(maxsize=2) for _ in range(2)]
        # 同一群 10 条 → 全进同一分片（容量 2）→ 分发器必然 await put 背压
        for i in range(10):
            await buf.put(_msg_ev(group_id=77, seq=i), None)
        dist = asyncio.create_task(
            FrameworkDispatchMixin._event_distributor_loop(stub))
        await asyncio.sleep(0.05)
        # 背压生效：分片队列满（2 条在途），其余 8 条仍留在 L1（不丢弃）
        q = stub._shard_queues[stub._shard_of(_msg_ev(group_id=77))]
        assert q.qsize() == 2
        assert dist.done() is False
        # 其余留在 L1（可能有一条已被取出、正阻塞在 put 上的在途事件）
        assert 0 < buf.stats()['l1_items'] <= 8
        # 边消费边放行：分发器跟随投递，最终 10 条全部按序可达
        got = []
        while len(got) < 10:
            try:
                ev, _d, _s = q.get_nowait()
                got.append(ev['seq'])
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.005)
        assert got == list(range(10))        # 顺序不乱、一条不少
        assert buf.empty_all()
        dist.cancel()
        try:
            await dist
        except asyncio.CancelledError:
            pass
        buf.close()
    asyncio.run(run())
