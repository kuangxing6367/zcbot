"""扩展点参数适配：框架多传参数不能把 handler 打死。

`event.before_dispatch` 固定传 `(event, bot_name)`。插件/内置监听器写
`def f(event)` 极自然；不做裁剪就是每条消息一条 TypeError，功能静默失效、
日志被刷屏。
"""
import asyncio

from framework.hooks import HookPoints, HookRegistry


def test_single_arg_handler_tolerates_extra_args():
    hooks = HookRegistry()
    got = []

    def record(event):
        got.append(event)

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'media:record', record)
    asyncio.run(hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'id': 1}, 'bot-a'))

    assert got == [{'id': 1}], 'handler 应收到 event，多出来的 bot_name 被裁掉'


def test_two_arg_handler_still_gets_both():
    hooks = HookRegistry()
    got = []

    def record(event, bot_name):
        got.append((event, bot_name))

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'x:record', record)
    asyncio.run(hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'id': 2}, 'bot-b'))

    assert got == [({'id': 2}, 'bot-b')], '声明了两个参数就应完整收到'


def test_varargs_handler_gets_everything():
    hooks = HookRegistry()
    got = []

    def record(*args):
        got.append(args)

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'x:any', record)
    asyncio.run(hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'id': 3}, 'bot-c'))

    assert got == [({'id': 3}, 'bot-c')]


def test_zero_arg_handler_does_not_blow_up():
    hooks = HookRegistry()
    got = []

    def ping():
        got.append('ping')

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'x:ping', ping)
    asyncio.run(hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'id': 4}, 'bot-d'))

    assert got == ['ping']


def test_kwargs_are_trimmed_to_accepted_names():
    hooks = HookRegistry()
    got = []

    def record(event):
        got.append(event)

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'x:kw', record)
    asyncio.run(hooks.trigger_async(
        HookPoints.EVENT_BEFORE_DISPATCH, {'id': 5}, bot_name='bot-e'))

    assert got == [{'id': 5}]


def test_short_circuit_still_works_after_trimming():
    """裁剪参数不能破坏短路语义（False 仍要终止后续）"""
    hooks = HookRegistry()
    seen = []

    def stop(event):
        seen.append('stop')
        return False

    def later(event, bot_name):
        seen.append('later')

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'a:stop', stop, priority=10)
    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'b:later', later, priority=20)
    results = asyncio.run(
        hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'id': 6}, 'bot-f'))

    assert results == [False], results
    assert seen == ['stop'], '短路后不应再执行后面的 handler'


def test_reload_updates_arity():
    """同名重新注册（热重载）后，参数表要跟着换掉，不能沿用旧缓存"""
    hooks = HookRegistry()
    got = []

    def v1(event):
        got.append(('v1', event))

    def v2(event, bot_name):
        got.append(('v2', event, bot_name))

    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'x:rec', v1)
    asyncio.run(hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'n': 1}, 'b'))
    hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'x:rec', v2)
    asyncio.run(hooks.trigger_async(HookPoints.EVENT_BEFORE_DISPATCH, {'n': 2}, 'b'))

    assert got == [('v1', {'n': 1}), ('v2', {'n': 2}, 'b')]


if __name__ == '__main__':
    for fn in (test_single_arg_handler_tolerates_extra_args,
               test_two_arg_handler_still_gets_both,
               test_varargs_handler_gets_everything,
               test_zero_arg_handler_does_not_blow_up,
               test_kwargs_are_trimmed_to_accepted_names,
               test_short_circuit_still_works_after_trimming,
               test_reload_updates_arity):
        fn()
        print('ok', fn.__name__)


def test_keyword_only_param_is_filled_from_extra_positional():
    """审计 D-1-3：def f(event, *, bot_name) 必须拿到第二个位置实参。

    旧版 _fit_args 把多余位置参数直接截断，handler 抛 TypeError，
    再被短路点的 fail-closed 转成 False 丢掉整条事件。
    """
    r = HookRegistry()
    hits = []

    def handler(event, *, bot_name):
        hits.append((event, bot_name))

    r.register('event.before_dispatch', 'p:kw', handler)
    results = asyncio.run(r.trigger_async('event.before_dispatch', {'id': 1}, 'bot-a'))

    assert hits == [({'id': 1}, 'bot-a')], f'keyword-only 未回填: hits={hits} results={results}'
    assert False not in results
