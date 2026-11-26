"""事件循环看门狗单测：空闲不误报 / 阻塞能测到 / 恢复计数 / 栈能指到阻塞点。

这个模块存在的意义就是「阻塞发生时有人吭声」。所以测试重点不是它能跑，
而是：空闲时**不许**误报（否则日志被刷烂、真问题被淹没），
阻塞时**必须**报且能指出卡在哪个文件哪一行。
"""
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.core.loop_watchdog import LoopWatchdog


def test_idle_loop_does_not_report():
    """正常忙碌的空闲循环不能报卡顿——误报比不报更糟"""
    wd = LoopWatchdog(interval=0.05, threshold=0.4)

    async def main():
        wd.start(loop=asyncio.get_running_loop())
        await asyncio.sleep(0.6)
        stats = wd.stats()
        wd.stop()
        return stats

    stats = asyncio.run(main())
    assert stats['samples'] >= 5, stats
    assert stats['stalls'] == 0, stats
    assert stats['blocked'] is False
    assert stats['max_lag_ms'] < 400, stats


def test_blocking_call_detected():
    """同步阻塞（time.sleep）必须被测出来，并计入累计时长"""
    wd = LoopWatchdog(interval=0.05, threshold=0.15)

    async def main():
        wd.start(loop=asyncio.get_running_loop())
        await asyncio.sleep(0.15)      # 先让看门狗采到几轮，拿到 loop 线程 id
        time.sleep(0.5)                # 阻塞事件循环
        await asyncio.sleep(0.2)       # 让看门狗观察到恢复
        stats = wd.stats()
        wd.stop()
        return stats

    stats = asyncio.run(main())
    assert stats['stalls'] >= 1, stats
    assert stats['max_lag_ms'] >= 150, stats
    assert stats['total_stall_ms'] > 0, stats
    assert stats['blocked'] is False, '恢复后不应仍处于阻塞标记'


def test_stack_points_to_blocking_line():
    """卡顿日志必须指到阻塞发生的那一行，否则等于没说"""
    wd = LoopWatchdog(interval=0.05, threshold=0.1)

    async def main():
        wd.start(loop=asyncio.get_running_loop())
        await asyncio.sleep(0.12)
        time.sleep(0.4)
        await asyncio.sleep(0.15)
        stats = wd.stats()
        wd.stop()
        return stats

    stats = asyncio.run(main())
    where = stats.get('reported_stack') or ''
    assert where, '必须记录阻塞位置'
    assert '无法获取' not in where
    assert 'test_loop_watchdog' in where, f'栈应指到测试文件里的阻塞行，实际:\n{where}'


def test_stop_is_idempotent_and_restartable():
    wd = LoopWatchdog(interval=0.05, threshold=0.5)

    async def main():
        wd.start(loop=asyncio.get_running_loop())
        await asyncio.sleep(0.15)
        wd.stop()
        wd.stop()                       # 幂等
        first_thread = wd._thread
        wd.start(loop=asyncio.get_running_loop())   # 可重新启动
        await asyncio.sleep(0.15)
        alive = wd._thread is not None and wd._thread.is_alive()
        wd.stop()
        return first_thread, alive

    first_thread, alive = asyncio.run(main())
    assert first_thread is None, 'stop 后应清空线程引用'
    assert alive, 'stop 后应能重新启动'


def test_stats_shape():
    wd = LoopWatchdog(interval=0.05, threshold=0.5)
    stats = wd.stats()
    for key in ('samples', 'stalls', 'total_stall_ms', 'max_lag_ms',
                'last_lag_ms', 'blocked', 'threshold_ms', 'interval_ms'):
        assert key in stats, f'缺少字段 {key}'
    assert stats['threshold_ms'] == 500.0
    assert stats['interval_ms'] == 50.0


def test_parameter_floor():
    """参数下限保护：interval/threshold 过小会让看门狗自己变成负担"""
    wd = LoopWatchdog(interval=0, threshold=0)
    assert wd.interval >= 0.05
    assert wd.threshold >= 0.05
    assert wd.stall_wait >= wd.threshold * 2


def test_suspend_suppresses_warnings_but_records_peak():
    """启动期不报警（否则每次启动都刷 warning，真卡顿就没人看了），
    但启动期延迟要单独记账，且不能污染运行期的 max。"""
    wd = LoopWatchdog(interval=0.05, threshold=0.1)
    # 全量并跑时其它测试留下的看门狗实例也会打 warning；若 logging 的 stderr
    # handler 被改坏，logging.handleError 会 traceback.print_stack 同步阻塞事件
    # 循环几百毫秒，污染本用例的运行期统计（实测 stalls=1 / max_lag=221ms，
    # 栈正是 handleError→print_stack）。本用例只关心自己的记账语义，临时把
    # zcbot logger 接到空 handler 上隔离外部日志噪音。
    zlog = logging.getLogger('zcbot')
    old_level, old_handlers = zlog.level, list(zlog.handlers)
    zlog.handlers = [logging.NullHandler()]
    zlog.setLevel(logging.CRITICAL)
    try:
        _run_suspend_case(wd)
    finally:
        zlog.handlers = old_handlers
        zlog.setLevel(old_level)


def _run_suspend_case(wd):
    async def main():
        wd.start(loop=asyncio.get_running_loop())
        # 只需让看门狗线程就绪；不能等到接近 threshold（0.1s），否则全量并跑时
        # 这次 sleep 本身就可能超阈值，在 suspend() 之前先记一次 stall，
        # 让「启动期不得计入卡顿次数」的断言随机失败。
        await asyncio.sleep(0.02)
        wd.suspend()
        time.sleep(0.35)                 # 启动期阻塞
        await asyncio.sleep(0.1)
        peak = wd.resume()
        # 运行期空闲要大于采样间隔(0.05s)以至少采一次，又必须明显短于
        # threshold(0.1s)：原来 sleep(0.15) 本身就超阈值，单跑时恢复快不触发，
        # 全量并跑时调度延迟稍大就记一次 stall（实测连续三轮全量都撞上）。
        await asyncio.sleep(0.06)
        stats = wd.stats()
        wd.stop()
        return peak, stats

    peak, stats = asyncio.run(main())
    assert peak >= 100, '启动期峰值必须被记录'
    assert stats['stalls'] == 0, '启动期不得计入卡顿次数'
    assert stats['startup_max_lag_ms'] == peak
    assert stats['max_lag_ms'] < 100, '运行期峰值应从 resume 后重新计'
    assert stats['blocked'] is False
