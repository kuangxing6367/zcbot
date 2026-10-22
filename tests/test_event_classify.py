# -*- coding: utf-8 -*-
"""四类事件统一分类契约层（架构 C 项）回归测试。

直接运行：PYTHONPATH=. python tests/test_event_classify.py
或：PYTHONPATH=. python -m pytest tests/test_event_classify.py -q
"""
import asyncio
import re
import sys

sys.path.insert(0, '.')

from framework.messaging import event_classify as ec
from framework.messaging.event import Event
from framework.messaging.router_match import RouterMatchMixin
from framework.messaging.router import _PluginRoute, _RouteCommand
from framework.hooks import HookPoints


def test_event_category_enum():
    assert ec.EventCategory.GENERAL.value == 'general'
    assert ec.EventCategory.REGEX.value == 'regex'
    assert ec.EventCategory.ALL.value == 'all'
    assert ec.EventCategory.RAW.value == 'raw'
    assert ec.EventCategory.NONE.value == 'none'


def test_initial_classification():
    msg = ec.initial_classification({'post_type': 'message'})
    assert msg['is_message'] is True
    assert msg['is_raw'] is True
    assert msg['is_all'] is True
    assert msg['command'] is None

    non = ec.initial_classification({'post_type': 'notice'})
    assert non['is_message'] is False
    assert non['is_raw'] is False
    assert non['is_all'] is False


def test_event_category_default_and_non_message():
    ev = Event({'post_type': 'message', 'message': '随便说点', 'user_id': 1})
    # 命令未命中 → 归为全量监听
    assert ev.category == 'all'
    assert ev.categories == {'is_message': True, 'is_raw': True, 'is_all': True,
                             'command': None}

    ev2 = Event({'post_type': 'notice', 'notice_type': 'group_increase'})
    assert ev2.category == 'none'
    assert ev2.categories['is_message'] is False


def test_event_category_backfilled_on_general_and_regex():
    ev = Event({'post_type': 'message', 'message': '签到', 'user_id': 1,
                'message_type': 'group', 'group_id': 10})
    ev._category_command = ec.COMMAND_GENERAL
    assert ev.category == 'general'

    ev2 = Event({'post_type': 'message', 'message': '使用优惠 ABC', 'user_id': 2})
    ev2._category_command = ec.COMMAND_REGEX
    assert ev2.category == 'regex'


# ── 路由命中回填类别：用轻量 harness 真实跑 _match_plugin_commands ──

class _FakeModule:
    def handler(self, ev, match):
        return None  # 视为已处理


class _FakeHooks:
    async def trigger_async(self, point, *a, **k):
        return []


class _FakeFramework:
    def __init__(self):
        self.hooks = _FakeHooks()

    async def reply_text(self, *a, **k):
        return None


class _Harness(RouterMatchMixin):
    def __init__(self):
        self.framework = _FakeFramework()

    def _stats_hit(self, cmd_id):
        pass


def _build_entry(handler_name, simple=None, rx=None):
    cmd = _RouteCommand(
        id=1, pattern=(rx.pattern if rx else simple), handler_name=handler_name,
        require_level='', rx=rx, simple=simple, aliases=[],
        require_perm='')
    return _PluginRoute(module=_FakeModule(), commands=[cmd], index=None)


def test_router_backfills_general():
    h = _Harness()
    ev = Event({'post_type': 'message', 'message': '签到', 'user_id': 1,
                'message_type': 'group', 'group_id': 10})
    entry = _build_entry('handler', simple='签到')
    matched = asyncio.run(h._match_plugin_commands(entry, ev, '签到', 'p'))
    assert matched is True
    assert ev._category_command == 'general'
    assert ev.category == 'general'


def test_router_backfills_regex():
    h = _Harness()
    rx = re.compile(r'使用优惠\s+(.+)')
    ev = Event({'post_type': 'message', 'message': '使用优惠 ABC', 'user_id': 2})
    entry = _build_entry('handler', rx=rx)
    matched = asyncio.run(h._match_plugin_commands(entry, ev, '使用优惠 ABC', 'p'))
    assert matched is True
    assert ev._category_command == 'regex'
    assert ev.category == 'regex'


def test_router_no_match_keeps_all():
    h = _Harness()
    ev = Event({'post_type': 'message', 'message': '今天天气不错', 'user_id': 3})
    entry = _build_entry('handler', simple='签到')
    matched = asyncio.run(h._match_plugin_commands(entry, ev, '今天天气不错', 'p'))
    assert matched is False
    assert ev._category_command is None  # 未命中命令，仍归全量
    assert ev.category == 'all'


def test_hook_point_constant():
    assert HookPoints.MESSAGE_CLASSIFIED == 'message.classified'


if __name__ == '__main__':
    test_event_category_enum()
    test_initial_classification()
    test_event_category_default_and_non_message()
    test_event_category_backfilled_on_general_and_regex()
    test_router_backfills_general()
    test_router_backfills_regex()
    test_router_no_match_keeps_all()
    test_hook_point_constant()
    print('ALL EVENT_CLASSIFY TESTS PASSED')
