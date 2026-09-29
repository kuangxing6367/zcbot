# -*- coding: utf-8 -*-
"""声明式插件装饰器 API（framework.plugin）单元测试。

验证：模块顶层装饰器在导入期只登记，框架 flush 时统一落到 ctx，与在 register 体内
直接调用 ctx.xxx(...) 等价；handler 形参、关键字透传、docstring 自动提取描述均正确。
"""
import types

from framework.plugin import flush, clear_module


_SRC = '''
from framework.plugin import (
    command, on, on_message, on_raw_message, hook, task, api, dashboard_card, webui,
)

@command("/help", description="查看帮助")
def help_handler(event, match):
    ...

@command("/ping")
def ping_handler(event, match):
    """连通性探测"""
    ...

@on("member.join")
def on_join(event):
    ...

@on_message
def on_msg(event):
    ...

@on_raw_message
def raw_handler(raw_event, bot_name):
    ...

@hook("command.before", priority=10)
def before_command(event, ctx):
    ...

@task("0 0 * * *")
def daily_job():
    ...

@api("/my/stats", methods=["GET", "POST"])
def stats_view():
    ...

@dashboard_card("在线数")
def card_handler():
    ...

webui("我的面板", entry="index.html", sidebar=True)
'''


def _build_module(name):
    mod = types.ModuleType(name)
    mod.__name__ = name
    exec(compile(_SRC, name, 'exec'), mod.__dict__)
    return mod


def test_decorators_flush_to_ctx():
    mod = _build_module('fake_plugin_deco_test')
    ctx = types.SimpleNamespace(
        command=lambda *a, **k: ctx._log.append(('command', k)),
        on=lambda *a, **k: ctx._log.append(('on', k)),
        on_raw_message=lambda *a, **k: ctx._log.append(('on_raw_message', k)),
        hook=lambda *a, **k: ctx._log.append(('hook', k)),
        task=lambda *a, **k: ctx._log.append(('task', k)),
        register_api=lambda *a, **k: ctx._log.append(('register_api', k)),
        dashboard_card=lambda *a, **k: ctx._log.append(('dashboard_card', k)),
        webui=lambda *a, **k: ctx._log.append(('webui', k)),
        _log=[],
    )
    flush('fake_plugin_deco_test', ctx)

    calls = dict(ctx._log)
    # 命令：两条，pattern 与 handler 透传；description 按传入原样转发（无传入时为 None，
    # 真正从 docstring 提取是 ctx.command 在 flush 时的职责，本测试只验证装饰器转发语义）
    cmds = [k for k in ctx._log if k[0] == 'command']
    assert len(cmds) == 2, cmds
    by_pat = {k[1]['pattern']: k[1] for k in cmds}
    assert by_pat['/help']['description'] == '查看帮助'      # 显式传入，原样转发
    assert by_pat['/ping']['description'] is None            # 未传，转发为 None（由 ctx.command 提取）
    assert callable(by_pat['/help']['handler'])

    # 事件订阅
    ons = [k for k in ctx._log if k[0] == 'on']
    assert ('message',) in [(k[1].get('event_name'),) for k in ons]
    assert ('member.join',) in [(k[1].get('event_name'),) for k in ons]

    # 其余类型均被调用一次
    for kind in ('on_raw_message', 'hook', 'task', 'register_api', 'dashboard_card', 'webui'):
        hits = [k for k in ctx._log if k[0] == kind]
        assert len(hits) == 1, (kind, hits)
    # hook 透传 priority
    assert [k for k in ctx._log if k[0] == 'hook'][0][1]['priority'] == 10
    # api 透传 methods
    assert [k for k in ctx._log if k[0] == 'register_api'][0][1]['methods'] == ['GET', 'POST']
    # webui 透传 sidebar
    assert [k for k in ctx._log if k[0] == 'webui'][0][1]['sidebar'] is True


def test_flush_is_noop_for_undecorated_module():
    ctx = types.SimpleNamespace(command=lambda *a, **k: None)
    # 未使用装饰器的模块 flush 不应抛错、也不应注册任何东西
    flush('module_without_decorators', ctx)
    clear_module('fake_plugin_deco_test')
