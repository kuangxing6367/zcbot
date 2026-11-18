"""plugin_test 示例用例 —— 照抄一条，改中间那段，就是你的插件测试。

四条用例分别对应插件最常写、也最容易写错的四件事：
1. 装饰器登记的东西，是否原样落到 ctx（命令名 / 优先级 / 描述有没有丢）；
2. ``register(ctx)`` 直写与 ``@command`` 装饰器是否等价（两条路并存，不能走偏）；
3. 没有 ``register`` 的插件是否仍被认作「有内容可注册」，flush 后登记表是否清干净；
4. handler 拿到事件后返回什么、回了什么包（纯函数式断言，不碰网络）。

跑法::

    py -3 -m pytest tests/plugin_test -q
"""
from framework.plugin import flush, has_pending

ECHO_PLUGIN = """
from framework.plugin import command, on, on_message, flush


@command('/echo', priority=10, description='原样回显')
def echo(event, match):
    return event.text


@on('member.join')
def on_join(event):
    return 'welcome'


@on_message
def on_any_message(event):
    if event.text == 'hi':
        event.reply('hi')


def register(ctx):
    flush(__name__, ctx)
"""

# 同样内容，但只留装饰器、不写 register —— 加载器应当照样认它有东西可注册
DECO_ONLY_PLUGIN = """
from framework.plugin import command


@command('/ping')
def ping(event, match):
    return 'pong'
"""


def test_decorators_land_on_ctx(make_plugin, rec_ctx):
    """插件声明了什么，ctx 就得收到什么——命令名、优先级、描述一个都不能丢。"""
    mod = make_plugin(ECHO_PLUGIN)
    mod.register(rec_ctx)

    commands = rec_ctx.of('command')
    assert len(commands) == 1
    _, kwargs = commands[0]
    assert kwargs['pattern'] == '/echo'
    assert kwargs['priority'] == 10
    assert kwargs['description'] == '原样回显'
    assert kwargs['handler'] is mod.echo

    # @on('member.join') 与 @on_message 落到同一个 ctx.on，用 event_name 区分
    assert [kw['event_name'] for _, kw in rec_ctx.of('on')] == ['member.join', 'message']


def test_decorator_matches_direct_ctx_call(make_plugin, rec_ctx):
    """``@command`` 与 ``ctx.command`` 直写必须等价。

    两条路并存是刻意的（旧插件不用改），语义一旦跑偏，作者会按自己习惯的那条写，
    另一条就成了暗坑。
    """
    decorated = make_plugin(ECHO_PLUGIN, name='decorated')
    decorated.register(rec_ctx)

    direct = make_plugin("""
def register(ctx):
    ctx.command('/echo', priority=10, description='原样回显',
                handler=lambda event, match: event.text)
""", name='direct')
    direct.register(rec_ctx)

    (_, deco_kw), (direct_args, direct_kw) = rec_ctx.of('command')
    # 直写的第一个位置参数就是 pattern，装饰器则统一走关键字
    assert direct_args[0] == deco_kw['pattern']
    assert direct_kw['priority'] == deco_kw['priority']
    assert direct_kw['description'] == deco_kw['description']


def test_register_can_be_omitted(make_plugin, rec_ctx):
    """没有 register 的插件不是空插件：装饰器登记过就算有内容。

    加载器靠 ``has_pending`` 判断「值不值得加载」；flush 之后待登记表必须清干净，
    否则重载会把同一批登记再落一遍，同一命令注册两次。
    """
    make_plugin(DECO_ONLY_PLUGIN)
    assert has_pending('plugin_test_demo_plugin') is True

    flush('plugin_test_demo_plugin', rec_ctx)

    assert has_pending('plugin_test_demo_plugin') is False
    assert [kw['pattern'] for _, kw in rec_ctx.of('command')] == ['/ping']


def test_handler_reads_event_and_replies(make_plugin, rec_ctx, msg_event):
    """handler 是纯函数：给事件、拿返回值、回包记在 event.replies 上。"""
    mod = make_plugin(ECHO_PLUGIN)
    mod.register(rec_ctx)

    assert mod.echo(msg_event('你好'), None) == '你好'

    event = msg_event('hi')
    mod.on_any_message(event)
    assert event.replies == [(('hi',), {})]

    # 不满足条件时不该回包——否则插件会对每条消息都刷一句
    quiet = msg_event('别理我')
    mod.on_any_message(quiet)
    assert quiet.replies == []
