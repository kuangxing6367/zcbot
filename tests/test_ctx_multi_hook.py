"""回归测试：同一插件在同一扩展点可注册多个 handler，且清理语义不回退。

A1：旧版 ctx.hook() 固定用 `plugin:point` 当注册名，第二个 handler
静默覆盖第一个。现在第一个仍用原名（兼容日志/外部观察），后续带 #2/#3 后缀；
同一 handler 重复注册继续去重。
"""
import asyncio

from framework.ctx import PluginContext
from framework.hooks import HookPoints, HookRegistry


class _FakeFramework:
    def __init__(self):
        self.hooks = HookRegistry(self)
        self.db = None
        self.services = None


def _ctx(name='demo'):
    fw = _FakeFramework()
    return fw, PluginContext(name, fw)


def test_two_handlers_same_point_both_fire():
    fw, ctx = _ctx()
    hits = []

    def first(event, bot_name=None):
        hits.append('first')

    def second(event, bot_name=None):
        hits.append('second')

    assert ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, first) is True
    assert ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, second) is True

    asyncio.run(fw.hooks.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {'id': 1}, 'default'))
    assert hits == ['first', 'second']


def test_same_handler_registered_twice_is_deduped():
    fw, ctx = _ctx()
    hits = []

    def only(event, bot_name=None):
        hits.append(1)

    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, only)
    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, only)

    asyncio.run(fw.hooks.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {'id': 2}, 'default'))
    assert hits == [1], '同一个 handler 重复注册必须仍去重'


def test_unhook_removes_all_handlers_on_point():
    fw, ctx = _ctx()
    hits = []

    def a(event, bot_name=None):
        hits.append('a')

    def b(event, bot_name=None):
        hits.append('b')

    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, a)
    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, b)

    assert ctx.unhook(HookPoints.EVENT_BEFORE_DISPATCH) is True
    asyncio.run(fw.hooks.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {'id': 3}, 'default'))
    assert hits == [], 'unhook 应清掉该插件在该点位的全部 handler'


def test_clear_plugin_still_works_with_suffixed_names():
    fw, ctx = _ctx()
    hits = []

    def a(event, bot_name=None):
        hits.append('a')

    def b(event, bot_name=None):
        hits.append('b')

    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, a)
    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, b)

    fw.hooks.clear_plugin('demo')
    asyncio.run(fw.hooks.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {'id': 4}, 'default'))
    assert hits == []


def test_other_plugin_is_untouched():
    fw = _FakeFramework()
    a = PluginContext('plugin_a', fw)
    b = PluginContext('plugin_b', fw)
    hits = []

    def ha(event, bot_name=None):
        hits.append('a')

    def hb(event, bot_name=None):
        hits.append('b')

    a.hook(HookPoints.EVENT_BEFORE_DISPATCH, ha)
    b.hook(HookPoints.EVENT_BEFORE_DISPATCH, hb)
    a.unhook(HookPoints.EVENT_BEFORE_DISPATCH)

    asyncio.run(fw.hooks.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {'id': 5}, 'default'))
    assert hits == ['b'], 'unhook 不得误删别的插件的 handler'
