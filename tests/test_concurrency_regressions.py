# -*- coding: utf-8 -*-
"""消息通道层并发竞态回归测试（真实并发 + 可控时钟）。

逐条对应本轮审查的四个问题（核实结论见汇报正文）：

1. 入站去重表 ``_seen`` 与限流计数 ``_hits`` 是两份独立状态，「共用同一份表」
   不成立；但「被限流丢弃的消息预写去重表」这一时序污染成立——其重放会在
   dedup_window 内被误判成 dedup 静默吞掉。修复后只有放行的事件才登记去重。
2. 入站限流 group/user 桶 key 未带 bot_name，跨接入端串桶成立；修复后按 bot
   隔离。global 桶刻意保持整机共享（与出站闸门一致），此点不该改，用护栏锁住。
3. 出站节流等待路径「检查(_plan) 与消费(_commit) 跨锁且不预留槽位」成立：
   并发等待者算出同一个唤醒时刻形成惊群、瞬时超配额；修复后在锁内 _reserve
   预约未来槽位，等待终点依次错开。
4. dict.popitem(last=) 淘汰事故未回潮；并发压到容量上限仍不抛异常、去重仍生效。

铁律：路径由 __file__ / tmp_path 推导，不写死时区，不依赖 cwd，日志数据用
临时绝对路径，不污染真实项目目录；不依赖真实时间，全部用可控时钟 / 单调计数。
"""
import asyncio
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from framework.messaging import inbound_guard as ibmod
from framework.messaging import outbound_guard as obmod
from framework.messaging.inbound_guard import InboundGuard
from framework.messaging.outbound_guard import OutboundGate


class _Clock:
    """可控时钟：只替换守卫模块内的 time 名字，不污染全局 time / asyncio。"""

    def __init__(self, now=10_000.0):
        self.now = float(now)

    def time(self):
        return self.now

    def sleep(self, sec):
        # 不真的睡；仅推进逻辑时刻（本文件用例都不需要真的阻塞）
        self.now += float(sec)


def _msg(bot='default', mid=1, uid=1001, gid=None, post='message'):
    e = {'bot_name': bot, 'message_id': mid, 'user_id': uid, 'post_type': post}
    if gid is not None:
        e['group_id'] = gid
    return e


def _outbound(**rate):
    g = OutboundGate()
    cfg = {
        'rate_limit': {'enabled': True, 'window': 60, 'global_per_min': 0,
                       'group_per_min': 0, 'user_per_min': 0, 'max_wait': 5.0},
        'retry': {'enabled': False, 'attempts': 0, 'backoff': 0},
    }
    cfg['rate_limit'].update(rate)
    g.configure(cfg)
    return g


OK = {'status': 'ok', 'retcode': 0}


# ── 1. 入站：去重表与限流计数独立，被限流消息不得写脏去重表 ──────────

def test_inbound_dedup_state_is_independent_and_throttled_event_not_recorded(
        monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(ibmod, 'time', clock)

    # 限流 warning 落在 tmp_path 推导的临时绝对路径，不写进真实项目目录
    log_path = tmp_path / 'inbound_guard.log'
    handler = logging.FileHandler(str(log_path), encoding='utf-8')
    logger = logging.getLogger('zcbot')
    logger.addHandler(handler)

    try:
        cfg = {'dedup_window': 300,
               'rate_limit': {'enabled': True, 'window': 60, 'user_per_min': 1}}
        g = InboundGuard(cfg)

        # 核实「共用同一份状态/表」：三份 dict 各自独立、key 空间与容量上限都不同
        assert g._seen is not g._hits
        assert g._seen is not g._notified
        assert g._SEEN_CAP != g._HITS_CAP

        n = 12
        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(
                lambda i: g.check(_msg(bot='A', mid=i, uid=7)),
                range(1, n + 1)))

        allowed = [r for r in results if r[0]]
        blocked = [r for r in results if not r[0]]
        # user_per_min=1 且 check 全程持锁：并发下恰好 1 条放行，其余按 user 限流
        assert len(allowed) == 1, ('并发下应恰好 1 条放行: %r' % results)
        assert all(r[1] == 'rate_limit:user' for r in blocked), str(blocked)

        # 只有放行的那条写进去重表：被限流的消息不得污染 _seen
        assert len(g._seen) == 1, ('被限流消息写脏了去重表: %r' % sorted(g._seen))
        # 限流计数独立累计在 _hits，没有被去重记录反向污染
        user_buckets = [dq for k, dq in g._hits.items()
                        if len(k) == 3 and k[0] == 'A' and k[1] == 'user']
        assert len(user_buckets) == 1 and len(user_buckets[0]) == 1, (
            '限流计数未独立累计: %r' % dict(g._hits))

        # 日志数据确实落在临时绝对路径
        assert os.path.isabs(str(log_path)) and log_path.exists()
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_inbound_throttled_event_does_not_poison_later_replay(monkeypatch):
    """时序污染的后果：被限流消息的重放不能被 dedup 静默吞掉。"""
    clock = _Clock()
    monkeypatch.setattr(ibmod, 'time', clock)
    cfg = {'dedup_window': 300,
           'rate_limit': {'enabled': True, 'window': 60, 'user_per_min': 1}}
    g = InboundGuard(cfg)

    assert g.check(_msg(bot='A', mid=1, uid=7)) == (True, '', False)
    r = g.check(_msg(bot='A', mid=2, uid=7))
    assert r == (False, 'rate_limit:user', False), str(r)
    assert ('A', '2') not in g._seen, '被限流的消息不该占用去重槽位'

    # 限流窗口(60s)已过、去重窗口(300s)未过：该消息重放应放行。
    # 旧实现这里返回 dedup（因为被限流时已写脏 _seen），修复后返回放行。
    clock.now += 61
    assert g.check(_msg(bot='A', mid=2, uid=7)) == (True, '', False)


# ── 2. 入站：限流桶按 bot 隔离，global 刻意保持整机共享 ────────────────

def test_inbound_rate_limit_buckets_are_isolated_per_bot(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ibmod, 'time', clock)
    # 关掉去重，只观察限流桶；A、B 两端在相同 group_id / user_id 上并发刷屏
    cfg = {'dedup_window': 0,
           'rate_limit': {'enabled': True, 'window': 60,
                          'group_per_min': 2, 'user_per_min': 2}}
    g = InboundGuard(cfg)

    def hit(bot, i):
        return g.check(_msg(bot=bot, mid=i, uid=5, gid=99))

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(hit, 'A', i) for i in range(1, 5)]
        futs += [ex.submit(hit, 'B', 100 + i) for i in range(1, 5)]
        results = [f.result() for f in futs]

    a_allowed = sum(1 for r in results[:4] if r[0])
    b_allowed = sum(1 for r in results[4:] if r[0])
    # 每端各自的配额=2，互不串桶：两端各恰好 2 条放行。
    # 旧实现下两端共用 ('group','99') / ('user','5') 桶，总共只放行 2 条。
    assert (a_allowed, b_allowed) == (2, 2), (
        '跨 bot 串桶: A=%d B=%d' % (a_allowed, b_allowed))

    group_keys = [k for k in g._hits if len(k) == 3 and k[1] == 'group']
    assert any(k[0] == 'A' for k in group_keys), dict(g._hits)
    assert any(k[0] == 'B' for k in group_keys), dict(g._hits)


def test_inbound_global_bucket_stays_shared_on_purpose(monkeypatch):
    """护栏：global 是「整机/全接入端合计」上限，刻意不按 bot 拆分（不该改）。

    与出站闸门 _keys() 的 ('global',) 一致；拆了会把「整机上限」降级成
    「每接入端上限」，N 个端合计 N 倍配额，防全局打爆的能力就没了。
    """
    clock = _Clock()
    monkeypatch.setattr(ibmod, 'time', clock)
    cfg = {'dedup_window': 0,
           'rate_limit': {'enabled': True, 'window': 60, 'global_per_min': 1}}
    g = InboundGuard(cfg)

    assert g.check(_msg(bot='A', mid=1, uid=1)) == (True, '', False)
    assert g.check(_msg(bot='B', mid=2, uid=2)) == (
        False, 'rate_limit:global', False)
    # 只有一个全局桶，没有被按 bot 拆成两份
    assert len(g._hits) == 1, dict(g._hits)


# ── 3. 出站：等待路径必须在锁内预留槽位 ───────────────────────────────

def test_outbound_concurrent_plans_reserve_distinct_slots(monkeypatch):
    """多线程同时抵达 _plan：修复后每个请求都预留自己的未来槽位，等待终点错开。

    旧实现无预留：第 1 个登记后，其余全部看到同一个 dq[0]，算出同一个 wait，
    唤醒时一起冲过闸门（瞬时超配额）。
    """
    clock = _Clock()
    monkeypatch.setattr(obmod, 'time', clock)
    g = _outbound(global_per_min=1, window=10, max_wait=1000)

    n = 8
    barrier = threading.Barrier(n)  # 让所有线程真正同时抵达 _plan

    def plan(_i):
        barrier.wait()
        return g._plan('bot', {'user_id': 1})

    with ThreadPoolExecutor(max_workers=n) as ex:
        waits = [f.result()[0] for f in
                 [ex.submit(plan, i) for i in range(n)]]

    # 8 个并发请求的等待终点互不相同：0,10,20,...,70
    assert len(set(waits)) == n, ('等待终点未错开（无预留槽位）: %r' % waits)
    assert max(waits) > min(waits)

    # 每个请求都在锁内占了槽位：1 个已发送 + (n-1) 个预约
    assert len(g._hits[('global',)]) == n, (
        '预留槽位缺失: %r' % list(g._hits[('global',)]))


def test_outbound_asyncio_gather_staggers_waits(monkeypatch):
    """asyncio.gather 端到端：并发 execute_async 的等待时长必须错开，不能惊群。

    time.time 固定、asyncio.sleep 只让出控制权不真等，全程不依赖真实时间。
    """
    clock = _Clock()
    monkeypatch.setattr(obmod, 'time', clock)

    real_sleep = asyncio.sleep
    slept = []

    async def fake_sleep(sec, *args, **kwargs):
        slept.append(sec)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)

    g = _outbound(global_per_min=1, window=10, max_wait=1000)
    calls = []

    async def scenario():
        async def call():
            calls.append(1)
            return OK
        return await asyncio.gather(*[
            g.execute_async('send_msg', 'bot', {'user_id': 1}, call)
            for _ in range(6)
        ])

    results = asyncio.run(scenario())

    # 节流是等待不是丢弃：所有请求最终都发出
    assert all(r.get('retcode') == 0 for r in results), str(results)
    assert len(calls) == 6
    # 5 个等待者的 wait 依次是 10,20,30,40,50；旧实现全是 10（惊群）
    assert len(slept) == 5, str(slept)
    assert len(set(slept)) == 5, ('并发等待终点未错开（惊群）: %r' % slept)


# ── 4. 容量淘汰：popitem 事故不得回潮 ─────────────────────────────────

def test_capacity_eviction_never_raises_under_concurrency(monkeypatch):
    """dict.popitem(last=) 曾抛 TypeError，被外层 try 吞成放行（去重静默失效）。"""
    clock = _Clock()
    monkeypatch.setattr(ibmod, 'time', clock)
    g = InboundGuard({'dedup_window': 300})
    g._SEEN_CAP = 5

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda i: g.check(_msg(bot='A', mid=i, uid=i)),
                    range(1, 60)))

    assert len(g._seen) <= 5, str(len(g._seen))
    # 淘汰没把去重打挂：仍在表里的键，重放必须被拦
    alive = next(iter(g._seen))
    assert g.check(_msg(bot='A', mid=int(alive[1]), uid=999)) == (
        False, 'dedup', False)
