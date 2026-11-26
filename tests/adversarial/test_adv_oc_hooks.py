"""对抗性测试：HookRegistry 的短路语义在同步路径/异常路径下是否可靠。

只针对 framework/hooks.py 的公开行为，不依赖网络、真实 QQ 或仓库 data/。
这两条由 opencode 挖出并经主仓库实跑确认。
"""
import asyncio

from framework.hooks import HookPoints, HookRegistry


def test_adv_oc_trigger_sync_ignores_short_circuit():
    """trigger_sync 必须和 trigger_async 一样尊重返回 False 的短路语义。

    Web / 线程请求路径（Flask 请求钩子等）走的是 trigger_sync。若短路失效，
    被 Guard 插件拒绝的请求/事件仍会被后续 handler 处理，属于静默穿透。
    """
    registry = HookRegistry()
    calls = []

    def guard(event):
        calls.append('guard')
        return False

    def downstream(event):
        calls.append('downstream')

    registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'evil:guard', guard, priority=10)
    registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'core:consumer', downstream, priority=20)

    registry.trigger_sync(HookPoints.EVENT_BEFORE_DISPATCH, 'event-payload')

    assert calls == ['guard'], (
        "返回 False 的 before 处理器应当短路，但 trigger_sync 仍执行了后续 handler: "
        f"{calls}"
    )


def test_adv_oc_guard_exception_fails_open():
    """安全 Guard 抛异常时不能静默放行事件。

    trigger_async 捕获 handler 异常后只打日志（logger.error），既不向外抛出，
    也不向 results 写入否决信号。调用方按文档约定的 `False in results` 判断
    是否丢弃事件，于是"守卫崩溃"等价于"守卫放行"，且没有任何返回值告警。
    """
    async def scenario():
        registry = HookRegistry()
        processed = []

        def guard(event):
            raise RuntimeError('guard plugin crashed')

        def consumer(event):
            processed.append(event)

        registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'evil:guard', guard, priority=10)
        registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'core:consumer', consumer, priority=20)

        results = await registry.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, 'malicious-event')
        return processed, results

    processed, results = asyncio.run(scenario())

    assert processed == [], (
        "Guard 异常被吞掉后事件仍被下游处理（fail-open）: "
        f"processed={processed}, results={results}"
    )
