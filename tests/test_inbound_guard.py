# -*- coding: utf-8 -*-
"""入站可靠性守卫单测：去重 / 限流三级 / 配置关闭 / 异常放行。

铁律：路径用 __file__ 推导、时间戳不写死时区、不污染真实目录。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.messaging.inbound_guard import InboundGuard


def _msg(bot='default', mid=1, uid=1001, gid=None, post='message'):
    e = {'bot_name': bot, 'message_id': mid, 'user_id': uid, 'post_type': post}
    if gid is not None:
        e['group_id'] = gid
    return e


def check(name, cond, detail=''):
    print(('通过 ' if cond else '失败 ') + name + (('：' + detail) if detail else ''))
    if not cond:
        raise SystemExit('断言失败：' + name)


def test_dedup_basic():
    g = InboundGuard({'dedup_window': 300})
    e = _msg(mid=42)
    a1, r1, _ = g.check(e)
    a2, r2, _ = g.check(_msg(mid=42))          # 同一 message_id 重放
    check('去重：首条放行', a1 and r1 == '', str((a1, r1)))
    check('去重：重放拦截', (not a2) and r2 == 'dedup', str((a2, r2)))


def test_dedup_window_expiry():
    g = InboundGuard({'dedup_window': 1})
    e = _msg(mid=7)
    a1, _, _ = g.check(e)
    time.sleep(1.1)
    a2, r2, _ = g.check(_msg(mid=7))            # 窗口已过，应放行
    check('去重：窗口过期后放行', a1 and a2 and r2 == '', str((a1, a2, r2)))


def test_dedup_only_message_events():
    g = InboundGuard({'dedup_window': 300})
    n1, _, _ = g.check(_msg(mid=5, post='notice'))   # notice 不去重
    n2, _, _ = g.check(_msg(mid=5, post='notice'))
    check('去重：notice 不去重（两次都放行）', n1 and n2, str((n1, n2)))


def test_dedup_distinguish_bot():
    g = InboundGuard({'dedup_window': 300})
    a1, _, _ = g.check(_msg(bot='A', mid=9))
    a2, r2, _ = g.check(_msg(bot='B', mid=9))        # 不同 bot 同 message_id 不误拦
    check('去重：区分接入端', a1 and a2 and r2 == '', str((a1, a2, r2)))


def test_rate_limit_user_scope():
    cfg = {'rate_limit': {'enabled': True, 'window': 60, 'user_per_min': 2}}
    g = InboundGuard(cfg)
    e1 = _msg(mid=1, uid=2001)
    e2 = _msg(mid=2, uid=2001)
    e3 = _msg(mid=3, uid=2001)
    a1, _, _ = g.check(e1); a2, _, _ = g.check(e2); a3, r3, _ = g.check(e3)
    check('限流：用户前两条放行', a1 and a2, str((a1, a2)))
    check('限流：用户第三条拦截', (not a3) and r3 == 'rate_limit:user', str((a3, r3)))


def test_rate_limit_group_and_global():
    cfg = {'rate_limit': {'enabled': True, 'window': 60,
                          'global_per_min': 1, 'group_per_min': 5}}
    g = InboundGuard(cfg)
    # 全局上限 1：第一条任意事件放行，第二条全局拦截
    a_g1, _, _ = g.check(_msg(mid=1, uid=1, gid=10))
    a_g2, r_g2, _ = g.check(_msg(mid=2, uid=2, gid=11))
    check('限流：全局第二条拦截', (not a_g2) and r_g2 == 'rate_limit:global',
          str((a_g2, r_g2)))
    # 群上限 5：在群内连发 5 条后第 6 条拦（跨用户同群也计入）
    g2 = InboundGuard({'rate_limit': {'enabled': True, 'window': 60,
                                      'group_per_min': 5}})
    res = [g2.check(_msg(mid=i, uid=i, gid=99))[0] for i in range(1, 8)]
    check('限流：群前5条放行、第6-7条拦截',
          all(res[:5]) and not any(res[5:]), str(res))


def test_rate_limit_disabled():
    g = InboundGuard({'rate_limit': {'enabled': False, 'window': 60,
                                     'user_per_min': 1}})
    a1, _, _ = g.check(_msg(mid=1, uid=5))
    a2, _, _ = g.check(_msg(mid=2, uid=5))
    check('限流：关闭时不拦截', a1 and a2, str((a1, a2)))


def test_rate_limit_notify_once_per_window():
    cfg = {'rate_limit': {'enabled': True, 'window': 60, 'user_per_min': 1,
                          'action': 'notify', 'notify_text': '慢点'}}
    g = InboundGuard(cfg)
    g.check(_msg(mid=1, uid=77))                  # 第1条放行
    a2, r2, n2 = g.check(_msg(mid=2, uid=77))     # 第2条拦截 + 应通知
    a3, r3, n3 = g.check(_msg(mid=3, uid=77))     # 第3条拦截 + 同窗口不应再通知
    check('限流：拦截且首次通知', (not a2) and r2 == 'rate_limit:user' and n2,
          str((a2, r2, n2)))
    check('限流：同窗口不重复通知', (not a3) and (not n3), str((a3, n3)))


def test_dedup_disabled():
    g = InboundGuard({'dedup_window': 0})
    a1, _, _ = g.check(_msg(mid=11))
    a2, r2, _ = g.check(_msg(mid=11))
    check('去重：窗口为0时不拦截', a1 and a2 and r2 == '', str((a2, r2)))


def test_seen_capacity_eviction():
    """容量上限必须能淘汰，且淘汰走的是「最早插入」而非抛异常。

    回归点：dict.popitem 不接受 last 参数（只有 OrderedDict 有），
    超容量时曾会抛 TypeError，而守卫外层 try 会把事件全部放行 —— 表现为
    去重静默失效，比崩溃更难发现。
    """
    g = InboundGuard({'dedup_window': 300})
    g._SEEN_CAP = 3
    try:
        for i in range(1, 9):
            g.check(_msg(mid=i))
    except TypeError as e:
        raise SystemExit('断言失败：超容量淘汰抛异常 → %s' % e)
    check('容量：超限不抛异常且被压到上限', len(g._seen) <= 3, str(len(g._seen)))
    # 被淘汰的是最早的键，最新的仍能拦重复
    last = max(g._seen, key=lambda k: g._seen[k])
    a, r, _ = g.check(_msg(mid=int(last[1])))
    check('容量：最新键仍可去重', (not a) and r == 'dedup', str((a, r)))


def test_sweep_releases_memory():
    """sweep() 清空过期条目，且不误删未过期条目。"""
    g = InboundGuard({'dedup_window': 1,
                      'rate_limit': {'enabled': True, 'window': 1, 'user_per_min': 1}})
    g.check(_msg(mid=1, uid=1))
    g.check(_msg(mid=2, uid=1))              # 触发 user 限流记录
    g.check(_msg(mid=3, uid=2))              # 另一个用户，仍放行
    before = (len(g._seen), len(g._hits))
    time.sleep(1.1)
    g.sweep()
    after = (len(g._seen), len(g._hits))
    check('清理：过期条目被释放', before[0] > 0 and after == (0, 0),
          '%s -> %s' % (before, after))


def test_guard_never_raises_on_odd_events():
    """字段缺失 / 类型异常的事件不能把守卫打挂（否则外层放行=限流失效）。"""
    g = InboundGuard({'dedup_window': 300,
                      'rate_limit': {'enabled': True, 'window': 60,
                                     'user_per_min': 1, 'group_per_min': 1}})
    odd = [{}, {'post_type': 'message'}, {'post_type': 'message', 'message_id': None},
           {'post_type': 'message', 'message_id': 1, 'user_id': None},
           {'post_type': 'message', 'message_id': 2, 'user_id': 'abc', 'group_id': 'x'}]
    for e in odd:
        try:
            ok, reason, _ = g.check(e)
        except Exception as ex:  # noqa: BLE001
            raise SystemExit('断言失败：异常事件把守卫打挂 → %r %s' % (e, ex))
        check('健壮：异常事件返回判定而非抛出 %r' % (e,), isinstance(ok, bool), reason)


def test_dispatch_wiring_dedup_and_notify():
    """集成：走真实 dispatch_event，确认守卫确实在入队前生效。

    回归点：守卫若被挪到入队之后（或异常被吞成放行），去重会静默失效；
    这里用最小 stub 避开整框架启动，直接断言「第二条重放不进 _process_event」。
    """
    import asyncio
    from framework.core.dispatch import FrameworkDispatchMixin

    class _Svc:
        def adapter_for_source(self, source):
            raise LookupError('stub')      # 无接入端 → 归一化走默认分支

    class _Hooks:
        async def trigger_async(self, *a, **kw):
            return []

    class _Stub:
        dispatch_event = FrameworkDispatchMixin.dispatch_event

        def __init__(self, cfg):
            self.config = cfg
            self.services = _Svc()
            self.hooks = _Hooks()
            self._event_workers = []       # 未启动 worker → 同步处理分支
            self._running = False
            self.processed = []
            self.replied = []

        async def _process_event(self, event):
            self.processed.append(event)

        async def reply_text(self, target, text):
            self.replied.append(text)

    async def _run():
        cfg = {'reliability': {'dedup_window': 300,
                               'rate_limit': {'enabled': True, 'window': 60,
                                              'user_per_min': 1, 'action': 'notify',
                                              'notify_text': '慢点'}}}
        st = _Stub(cfg)
        await st.dispatch_event({'post_type': 'message', 'message_id': 100,
                                 'user_id': 1, 'bot_name': 'onebot', 'message': 'a'})
        await st.dispatch_event({'post_type': 'message', 'message_id': 100,
                                 'user_id': 1, 'bot_name': 'onebot', 'message': 'a'})
        check('接线：重放事件在入队前被丢弃', len(st.processed) == 1,
              'processed=%d' % len(st.processed))

        # 限流 + notify：第二条新消息应被拦并回一条提示（只回一次）
        st2 = _Stub(cfg)
        for i in range(1, 4):
            await st2.dispatch_event({'post_type': 'message', 'message_id': 200 + i,
                                      'user_id': 9, 'bot_name': 'onebot',
                                      'message': 'x%d' % i})
        check('接线：限流生效（仅首条进入处理）', len(st2.processed) == 1,
              'processed=%d' % len(st2.processed))
        check('接线：限流提示只回一次', st2.replied == ['慢点'], str(st2.replied))

        # 去重关闭时全部放行（配置真的被读到）
        st3 = _Stub({'reliability': {'dedup_window': 0}})
        for _ in range(2):
            await st3.dispatch_event({'post_type': 'message', 'message_id': 300,
                                      'user_id': 3, 'bot_name': 'onebot',
                                      'message': 'y'})
        check('接线：dedup_window=0 时不拦截', len(st3.processed) == 2,
              'processed=%d' % len(st3.processed))

    asyncio.run(_run())


def run_all():
    for fn in (test_dedup_basic, test_dedup_window_expiry, test_dedup_only_message_events,
               test_dedup_distinguish_bot, test_rate_limit_user_scope,
               test_rate_limit_group_and_global, test_rate_limit_disabled,
               test_rate_limit_notify_once_per_window, test_dedup_disabled,
               test_seen_capacity_eviction, test_sweep_releases_memory,
               test_guard_never_raises_on_odd_events, test_dispatch_wiring_dedup_and_notify):
        fn()
    print('\n全部通过：入站可靠性守卫 %d 组' % 13)


if __name__ == '__main__':
    run_all()
