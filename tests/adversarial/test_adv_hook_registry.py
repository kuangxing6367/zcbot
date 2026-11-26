"""底层破坏性测试（adversarial）。

这些用例断言的是**期望的正确行为**，而当前代码还做不到，所以每条都标了
``xfail(strict=True)``：现在整条测试流仍然绿，但一旦某条问题被修好，
它会变成 XPASS 并**让 CI 变红**——强制维护者回来把这个标记去掉。
这样既不会把主门禁钉死，也不会让已知缺陷被慢慢忘掉。

注：每条用例对应的现象、根因和影响面见同目录 FINDINGS.md。
"""
import asyncio

import pytest

from framework.hooks import HookPoints, HookRegistry


def _fire(registry, point, *args):
    asyncio.run(registry.trigger_async(point, *args))


def test_two_handlers_same_point_same_plugin_both_fire():
    """插件作者写两行 ctx.hook(..., h1) / (..., h2) 是极自然的需求。

    旧版 ctx.hook() 固定用 f'{plugin}:{point}' 当注册名，h1 被 h2 静默覆盖；
    修复后走真实 ctx 路径，两个 handler 都应触发。
    """
    from framework.ctx import PluginContext

    class _FW:
        db = None
        services = None

    fw = _FW()
    fw.hooks = HookRegistry(fw)
    ctx = PluginContext('p1', fw)
    hits = []

    def first(event, bot_name=None):
        hits.append('first')

    def second(event, bot_name=None):
        hits.append('second')

    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, first)
    ctx.hook(HookPoints.EVENT_BEFORE_DISPATCH, second)

    _fire(fw.hooks, HookPoints.EVENT_BEFORE_DISPATCH, {'id': 1}, 'default')

    assert hits == ['first', 'second'], (
        f'期望两个处理器都执行，实际只执行了 {hits}；第一个被同名注册静默覆盖'
    )


@pytest.mark.xfail(
    strict=True,
    reason="缺陷 A2：HookRegistry 无 owner 概念，clear_plugin 可清空任意插件的处理器；见 FINDINGS.md",
)
def test_plugin_cannot_clear_another_plugins_hooks():
    """插件 A 只要拿到 framework.hooks，就能把插件 B 的全部扩展点清掉。

    同进程插件本就互相信任，这里不追求安全隔离，但"误写插件名"或"同名前缀
    碰撞"造成的连带清空应当可察觉——目前一点痕迹都没有。
    """
    registry = HookRegistry(None)
    hits = []

    def b_handler(event, bot_name=None):
        hits.append('b')

    registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'plugin_a:event.before_dispatch',
                      lambda *a, **k: None)
    registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'plugin_b:event.before_dispatch', b_handler)

    # 模拟 A 误把 B 的名字传进清理接口
    registry.clear_plugin('plugin_b')

    _fire(registry, HookPoints.EVENT_BEFORE_DISPATCH, {'id': 2}, 'default')
    assert hits == ['b'], '插件 B 的处理器不应因为别人的 clear_plugin 调用而消失'


@pytest.mark.xfail(
    strict=True,
    reason="缺陷 A3：unregister 只按 name 匹配，跨插件同名即可互相注销；见 FINDINGS.md",
)
def test_unregister_is_scoped_to_owner():
    """name 里虽然带了 'plugin:' 前缀，但 unregister 不校验调用方归属。"""
    registry = HookRegistry(None)
    hits = []

    def victim(event, bot_name=None):
        hits.append('victim')

    registry.register(HookPoints.COMMAND_BEFORE, 'plugin_b:command.before', victim)
    registry.unregister(HookPoints.COMMAND_BEFORE, 'plugin_b:command.before')

    _fire(registry, HookPoints.COMMAND_BEFORE, {'cmd': 'x'})
    assert hits == ['victim']


def test_hook_arity_adapter_trims_extra_positional_args():
    """正向守卫：少参 handler 不该再抛 TypeError（1.28.16 修复点）。"""
    registry = HookRegistry(None)
    hits = []
    registry.register(HookPoints.EVENT_BEFORE_DISPATCH, 'p:event.before_dispatch',
                      lambda event: hits.append(event))
    _fire(registry, HookPoints.EVENT_BEFORE_DISPATCH, {'id': 3}, 'default')
    assert hits == [{'id': 3}]
