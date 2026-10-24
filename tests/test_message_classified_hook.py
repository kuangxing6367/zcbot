# -*- coding: utf-8 -*-
"""回归测试：message.classified 扩展点必须真实发出且分类正确。

背景（真实缺陷）：该扩展点曾实现在 dispatch 侧，引用了作用域内并不存在的 `ev`
（该层变量叫 event 且是原始 dict），触发 NameError 后被 except 吞成一条
「message.classified 扩展点异常」——基准测试下每条消息都刷一次。
而既有分类测试只直接测 Event 类、从不驱动 MessageRouter.route()，故漏网。

本测试真实驱动 route()，要求：
1. 扩展点确实被触发（而不是静默不触发 / 内部报错被吞）；
2. category / categories 与命中情况一致（general / regex / all）；
3. 全程不得出现「message.classified 扩展点异常」ERROR 日志。

直接运行：PYTHONPATH=. python tests/test_message_classified_hook.py
或：PYTHONPATH=. python -m pytest tests/test_message_classified_hook.py -q
"""
import asyncio
import logging
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, '.')

from framework.messaging.router import MessageRouter, _PluginRoute, _RouteCommand
from framework.messaging.event_bus import EventBus
from framework.hooks import HookRegistry, HookPoints

ERR_NEEDLE = 'message.classified 扩展点异常'


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.setLevel(logging.DEBUG)

    def emit(self, record):
        self.records.append(record)

    def errors(self, needle=None):
        out = [r for r in self.records if r.levelno >= logging.ERROR]
        if needle:
            out = [r for r in out if needle in r.getMessage()]
        return out


def _attach():
    cap = _LogCapture()
    lg = logging.getLogger('zcbot')
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    return cap


def _detach(cap):
    logging.getLogger('zcbot').removeHandler(cap)


class _Module:
    """极简插件模块：handler 被调用即记录，返回 None（视为已处理）。"""

    def __init__(self):
        self.calls = []

    def handler(self, ev, match):
        self.calls.append((ev, match))
        return None


def _build_router(captured):
    class FakeFW:
        def __init__(self):
            self.hooks = HookRegistry(self)
            self.event_bus = EventBus()
            self.db = None
            self.plugin_loader = SimpleNamespace(
                is_plugin_enabled_for_group_cached=lambda *a, **k: True)

    fw = FakeFW()
    fw.hooks.register(HookPoints.MESSAGE_CLASSIFIED, 't:cap',
                      lambda *a, **k: captured.append(a[0]), 50)
    return fw, MessageRouter(fw)


def _entry(module, simple=None, rx=None):
    cmd = _RouteCommand(
        id=1, pattern=(rx.pattern if rx else simple), handler_name='handler',
        require_level='', rx=rx, simple=simple, aliases=[], require_perm='')
    return _PluginRoute(module=module, commands=[cmd], index=None)


def _msg(text):
    """标准 OneBot11 文本消息段（文本在 data.text 下，见 Event._extract_text）。"""
    return [{'type': 'text', 'data': {'text': text}}]


async def _route(router, module, text, simple=None, rx=None):
    router._routes = {'p': _entry(module, simple=simple, rx=rx)}
    router._plugin_order = ['p']
    await router.route({'post_type': 'message', 'message': _msg(text),
                        'user_id': 1, 'message_type': 'private'})


async def test_classified_fires_on_general_command():
    """命中普通命令 → category == 'general'，且不得有扩展点异常日志。"""
    cap = _attach()
    try:
        captured = []
        _, router = _build_router(captured)
        module = _Module()
        await _route(router, module, '签到', simple='签到')

        assert len(captured) == 1, \
            f"message.classified 必须触发一次，实际 {len(captured)} 次"
        p = captured[0]
        assert p['category'] == 'general', \
            f"命中简单命令应分类为 general，实际 {p['category']!r}"
        assert p['categories']['command'] == 'general'
        assert p['categories']['is_raw'] is True, "raw 是横切维度，恒为真"
        assert p['categories']['is_message'] is True
        # 载荷里的 Event 对象自身分类必须与 category 一致（单一真相源）
        assert p['event'].category == p['category']
        assert len(module.calls) == 1, "命令 handler 应被调用一次"
    finally:
        _detach(cap)
    assert not cap.errors(ERR_NEEDLE), \
        f"不应出现扩展点异常日志：{[r.getMessage() for r in cap.errors(ERR_NEEDLE)]}"


async def test_classified_fires_on_regex_command():
    """命中正则命令 → category == 'regex'。"""
    cap = _attach()
    try:
        captured = []
        _, router = _build_router(captured)
        module = _Module()
        rx = re.compile(r'使用优惠\s+(.+)')
        await _route(router, module, '使用优惠 ABC', rx=rx)

        assert len(captured) == 1, \
            f"message.classified 必须触发一次，实际 {len(captured)} 次"
        assert captured[0]['category'] == 'regex', \
            f"命中正则命令应分类为 regex，实际 {captured[0]['category']!r}"
        assert captured[0]['event'].category == 'regex'
    finally:
        _detach(cap)
    assert not cap.errors(ERR_NEEDLE), \
        f"不应出现扩展点异常日志：{[r.getMessage() for r in cap.errors(ERR_NEEDLE)]}"


async def test_classified_all_when_no_command_matched():
    """未命中任何命令（闲聊）→ category == 'all'，仍必须触发。"""
    cap = _attach()
    try:
        captured = []
        _, router = _build_router(captured)
        module = _Module()
        await _route(router, module, '今天天气不错', simple='签到')

        assert len(captured) == 1, \
            f"未命中也必须触发（归为全量监听），实际 {len(captured)} 次"
        assert captured[0]['category'] == 'all', \
            f"未命中应分类为 all，实际 {captured[0]['category']!r}"
        assert captured[0]['categories']['command'] is None
        assert len(module.calls) == 0, "未命中时命令 handler 不应被调用"
    finally:
        _detach(cap)
    assert not cap.errors(ERR_NEEDLE), \
        f"不应出现扩展点异常日志：{[r.getMessage() for r in cap.errors(ERR_NEEDLE)]}"


async def test_classified_fires_even_when_propagation_stopped():
    """命中后事件被 stop 传播（提前 return）→ finally 仍必须送达分类。"""
    cap = _attach()
    try:
        captured = []
        _, router = _build_router(captured)

        class _StopModule:
            def handler(self, ev, match):
                ev.stop_event()   # 正确 API：终止事件传播（Event.stop_event）
                return None

        await _route(router, _StopModule(), '签到', simple='签到')

        assert len(captured) == 1, \
            f"传播被终止时仍须触发分类（finally 汇流），实际 {len(captured)} 次"
        assert captured[0]['category'] == 'general'
    finally:
        _detach(cap)
    assert not cap.errors(ERR_NEEDLE), \
        f"不应出现扩展点异常日志：{[r.getMessage() for r in cap.errors(ERR_NEEDLE)]}"


def main():
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(test_classified_fires_on_general_command())
        loop.run_until_complete(test_classified_fires_on_regex_command())
        loop.run_until_complete(test_classified_all_when_no_command_matched())
        loop.run_until_complete(
            test_classified_fires_even_when_propagation_stopped())
    finally:
        loop.close()
    print("OK: 4/4 message.classified 扩展点真实触发且分类正确")


if __name__ == '__main__':
    main()
