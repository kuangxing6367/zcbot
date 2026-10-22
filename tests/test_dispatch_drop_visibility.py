# -*- coding: utf-8 -*-
"""回归测试：事件分发主干上「Hook 一票否决」必须可观测（不再静默吞）。

覆盖三处此前的隐性丢弃点：
- event.before_dispatch 返回 False → 丢弃事件（原 logger.debug，运维不可见）
- message.before_send 返回 False → 取消发送（原 logger.debug）
- router.before_route 返回 False → 跳过路由（原无任何日志）

这些点会真正丢弃/跳过消息，但此前在生产 INFO/WARNING 级别下零痕迹，
正是用户关切的"消息凭空消失却查不到线索"的同源问题。
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from framework.core.dispatch import FrameworkDispatchMixin
from framework.messaging.router import MessageRouter
from framework.messaging.event_bus import EventBus
from framework.hooks import HookRegistry, HookPoints


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.setLevel(logging.DEBUG)

    def emit(self, record):
        self.records.append(record)

    def warnings(self, needle=None):
        out = [r for r in self.records
               if r.levelno >= logging.WARNING]
        if needle:
            out = [r for r in out if needle in r.getMessage()]
        return out


def _attach():
    cap = _LogCapture()
    logging.getLogger('zcbot').addHandler(cap)
    logging.getLogger('zcbot').setLevel(logging.DEBUG)
    return cap


async def test_event_before_dispatch_drop_is_visible():
    cap = _attach()
    inst = FrameworkDispatchMixin()
    inst.hooks = HookRegistry(inst)
    inst._event_workers = []
    inst._running = False
    inst.services = None
    inst._process_event = AsyncMock()  # 若未被丢弃则会被调用

    inst.hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 't:drop',
                        lambda *a, **k: False, 50)

    await inst.dispatch_event({'type': 'message', 'message': 'hi',
                               'bot_name': 'default'})

    assert inst._process_event.await_count == 0, \
        "event.before_dispatch 返回 False 必须丢弃事件，不应继续处理"
    assert cap.warnings('event.before_dispatch'), \
        "事件被丢弃必须打 WARNING，否则运维不可见"


async def test_message_before_send_cancel_is_visible():
    cap = _attach()

    class FakeServices:
        def get(self, k, default=None):
            return default

        def adapter_for_source(self, src):
            return None

    inst = FrameworkDispatchMixin()
    inst.hooks = HookRegistry(inst)
    inst.services = FakeServices()

    inst.hooks.register(HookPoints.MESSAGE_BEFORE_SEND, 't:cancel',
                        lambda *a, **k: False, 50)

    res = await inst.reply_text({'group_id': 123, 'user_id': 1,
                                 'is_group': True}, 'hello')

    assert res is None, "message.before_send 返回 False 必须取消发送"
    assert cap.warnings('message.before_send'), \
        "发送被取消必须打 WARNING，否则运维不可见"


async def test_router_before_route_skip_is_visible():
    cap = _attach()

    class FakeFW:
        def __init__(self):
            self.hooks = HookRegistry(self)
            self.event_bus = EventBus()
            self.db = None
            self.plugin_loader = SimpleNamespace(
                is_plugin_enabled_for_group_cached=lambda *a, **k: True)

    fw = FakeFW()
    router = MessageRouter(fw)
    router._routes = {}
    router._plugin_order = []

    fw.hooks.register(HookPoints.ROUTER_BEFORE_ROUTE, 't:skip',
                      lambda *a, **k: False, 50)

    event = {'post_type': 'message', 'message': [{'type': 'text', 'text': 'hi'}],
             'bot_name': 'default'}
    await router.route(event)

    assert cap.warnings('router.before_route'), \
        "路由被跳过必须打 WARNING（此前此处无任何日志，消息静默不走命令匹配）"


def main():
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(test_event_before_dispatch_drop_is_visible())
        loop.run_until_complete(test_message_before_send_cancel_is_visible())
        loop.run_until_complete(test_router_before_route_skip_is_visible())
    finally:
        loop.close()
    print("OK: 3/3 分发拦截点均可观测（WARNING）")


if __name__ == '__main__':
    main()
