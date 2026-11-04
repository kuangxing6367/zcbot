# -*- coding: utf-8 -*-
"""回归测试：Hook 串行/并行声明（对齐事件分发架构设计）。

覆盖：
1. 默认 serial=True 保持按优先级顺序执行（零行为变化）。
2. serial=False 的 handler 与本扩展点其他非串行 handler 并发执行（asyncio.gather）。
3. 短路扩展点上，串行 handler 返回 False 会终止后续串行 handler，且并行组不再触发。
4. 非短路扩展点，串行组与并行组结果都被收集。
"""
import asyncio
import time

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.hooks import HookPriority, HookPoints, HookRegistry


def test_default_serial_preserves_priority_order():
    reg = HookRegistry()
    order = []

    def mk(p):
        def h(*a, **k):
            order.append(p)
            return True
        return h

    reg.register("x.test", "a", mk(30), priority=30)
    reg.register("x.test", "b", mk(10), priority=10)
    reg.register("x.test", "c", mk(50), priority=50)

    res = asyncio.run(reg.trigger_async("x.test"))
    assert order == [10, 30, 50], f"默认串行应按优先级顺序执行，实际 {order}"
    assert res == [True, True, True]


def _mk_slow(tag):
    async def slow(*a, **k):
        await asyncio.sleep(0.1)
        return tag
    return slow


def test_parallel_handlers_run_concurrently():
    reg = HookRegistry()

    # 两个非串行 handler：若并发，总耗时≈0.1s；若仍被串行化，≈0.2s
    reg.register("x.para", "p1", _mk_slow("p1"), serial=False)
    reg.register("x.para", "p2", _mk_slow("p2"), serial=False)

    t0 = time.monotonic()
    res = asyncio.run(reg.trigger_async("x.para"))
    elapsed = time.monotonic() - t0

    assert elapsed < 0.16, f"并行 handler 应并发执行（耗时≈0.1s），实际 {elapsed:.3f}s"
    assert set(res) == {"p1", "p2"}, f"并行结果应都被收集，实际 {res}"


def test_serial_handlers_still_sequential():
    reg = HookRegistry()

    # 两个串行 handler：必须顺序执行，总耗时≈0.2s
    reg.register("x.seq", "s1", _mk_slow("s1"), serial=True)
    reg.register("x.seq", "s2", _mk_slow("s2"), serial=True)

    t0 = time.monotonic()
    asyncio.run(reg.trigger_async("x.seq"))
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.19, f"串行 handler 应顺序执行（耗时≈0.2s），实际 {elapsed:.3f}s"


def test_serial_short_circuit_blocks_parallel_group():
    reg = HookRegistry()
    ran = []

    def blocker(*a, **k):
        ran.append("blocker")
        return False  # 短路：丢弃

    def para(*a, **k):
        ran.append("para")
        return True

    # 短路扩展点：串行 blocker（优先级高）先跑并返回 False，应阻止并行组启动
    reg.register(HookPoints.EVENT_BEFORE_DISPATCH, "blk", blocker, priority=10, serial=True)
    reg.register(HookPoints.EVENT_BEFORE_DISPATCH, "par", para, priority=90, serial=False)

    res = asyncio.run(reg.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, event=None, bot_name="x"))
    assert res == [False], f"短路后应仅含 False，实际 {res}"
    assert ran == ["blocker"], f"串行短路应阻止并行组执行，实际 {ran}"


def test_non_short_circuit_collects_both_groups():
    reg = HookRegistry()
    ran = []

    def s(*a, **k):
        ran.append("s")
        return "serial-res"

    def p(*a, **k):
        ran.append("p")
        return "parallel-res"

    reg.register(HookPoints.EVENT_AFTER_DISPATCH, "s", s, serial=True)
    reg.register(HookPoints.EVENT_AFTER_DISPATCH, "p", p, serial=False)

    res = asyncio.run(reg.trigger_async(HookPoints.EVENT_AFTER_DISPATCH))
    assert "serial-res" in res and "parallel-res" in res, f"两组结果都应收集，实际 {res}"
    assert set(ran) == {"s", "p"}


def test_priority_tiers_order_is_guaranteed():
    """段位约定：跨段位先后有保证，同段位内保持注册顺序（稳定排序）。"""
    reg = HookRegistry()
    order = []

    def mk(tag):
        def h(*a, **k):
            order.append(tag)
            return True
        return h

    # 故意按乱序注册，验证排序按段位而非注册时机
    reg.register("x.tier", "a_late", mk("late"), priority=HookPriority.LATE)
    reg.register("x.tier", "b_normal1", mk("normal1"), priority=HookPriority.NORMAL)
    reg.register("x.tier", "c_guard", mk("guard"), priority=HookPriority.GUARD)
    reg.register("x.tier", "d_early", mk("early"), priority=HookPriority.EARLY)
    reg.register("x.tier", "e_normal2", mk("normal2"), priority=HookPriority.NORMAL)

    asyncio.run(reg.trigger_async("x.tier"))
    assert order == ["early", "guard", "normal1", "normal2", "late"],         f"应按段位执行且同段位按注册顺序，实际 {order}"


def test_guard_tier_blocks_later_tiers():
    """GUARD 段位返回 False 会拦掉 LATE 段位（拦截方永远先于收尾方）。"""
    reg = HookRegistry()
    ran = []

    def guard(*a, **k):
        ran.append("guard")
        return False

    def late(*a, **k):
        ran.append("late")
        return True

    reg.register(HookPoints.EVENT_BEFORE_DISPATCH, "g", guard,
                 priority=HookPriority.GUARD)
    reg.register(HookPoints.EVENT_BEFORE_DISPATCH, "l", late,
                 priority=HookPriority.LATE)

    res = asyncio.run(reg.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {}, "bot"))
    assert ran == ["guard"], f"GUARD 拦截后 LATE 不应执行，实际 {ran}"
    assert False in res


if __name__ == "__main__":
    test_default_serial_preserves_priority_order()
    test_parallel_handlers_run_concurrently()
    test_serial_handlers_still_sequential()
    test_serial_short_circuit_blocks_parallel_group()
    test_non_short_circuit_collects_both_groups()
    test_priority_tiers_order_is_guaranteed()
    test_guard_tier_blocks_later_tiers()
    print("OK: 7/7 Hook 串行/并行与优先级段位语义全部符合架构设计")
