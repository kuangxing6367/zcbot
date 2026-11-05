# -*- coding: utf-8 -*-
"""回归测试：跨插件命令冲突检测（缺口 3）。

路由按 plugin_order 遍历、首个匹配插件胜出；若多个插件注册了相同
pattern 或 alias，靠后的插件会被静默遮蔽。本测试验证路由表构建期的
冲突检测能正确告警并记入 self._command_conflicts。

直接运行：PYTHONPATH=. python tests/test_command_conflict.py
或：PYTHONPATH=. python -m pytest tests/test_command_conflict.py -q
"""
import logging
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, '.')

from framework.messaging.router import MessageRouter, _RouteCommand

LOG = logging.getLogger('zcbot')


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.setLevel(logging.DEBUG)

    def emit(self, record):
        self.records.append(record)

    def warnings(self, needle=None):
        out = [r for r in self.records if r.levelno >= logging.WARNING]
        if needle:
            out = [r for r in out if needle in r.getMessage()]
        return out


def _attach():
    cap = _LogCapture()
    LOG.addHandler(cap)
    LOG.setLevel(logging.DEBUG)
    return cap


def _detach(cap):
    LOG.removeHandler(cap)


class _FakeFW:
    def __init__(self):
        self.db = None
        self.plugin_loader = SimpleNamespace(
            is_plugin_enabled_for_group_cached=lambda *a, **k: True)


def _router():
    return MessageRouter(_FakeFW())


def _cmd(pattern, aliases=None, rx=None, simple=None):
    if rx is None and simple is None:
        if pattern.startswith('^'):
            rx = re.compile(pattern)
        else:
            simple = pattern
    return _RouteCommand(
        id=1, pattern=pattern, handler_name='handler',
        require_level='', rx=rx, simple=simple,
        aliases=aliases or [], require_perm='')


async def test_cross_plugin_pattern_conflict_detected():
    """两个插件注册相同 pattern → 检测告警，A（order 靠前）胜出、B 被遮蔽。"""
    cap = _attach()
    try:
        r = _router()
        by_plugin = {
            'A': [_cmd('^help')],
            'B': [_cmd('^help')],
        }
        r._detect_command_conflicts(by_plugin, ['A', 'B'])
        assert len(r._command_conflicts) == 1, r._command_conflicts
        c = r._command_conflicts[0]
        assert c['type'] == 'pattern' and c['key'] == '^help'
        assert c['winner'] == 'A' and c['losers'] == ['B']
        assert cap.warnings('命令冲突[pattern]'), [m.getMessage() for m in cap.records]
    finally:
        _detach(cap)


async def test_cross_plugin_alias_conflict_detected():
    """两个插件注册相同 alias → 检测告警。"""
    cap = _attach()
    try:
        r = _router()
        by_plugin = {
            'A': [_cmd('/help', aliases=['/h'])],
            'B': [_cmd('/about', aliases=['/h'])],
        }
        r._detect_command_conflicts(by_plugin, ['A', 'B'])
        c = [x for x in r._command_conflicts if x['type'] == 'alias']
        assert c, r._command_conflicts
        assert c[0]['key'] == '/h' and c[0]['winner'] == 'A' and c[0]['losers'] == ['B']
        assert cap.warnings('命令冲突[alias]')
    finally:
        _detach(cap)


async def test_no_conflict_when_patterns_distinct():
    """pattern 不同 → 无冲突。"""
    cap = _attach()
    try:
        r = _router()
        by_plugin = {
            'A': [_cmd('^help')],
            'B': [_cmd('^about')],
        }
        r._detect_command_conflicts(by_plugin, ['A', 'B'])
        assert r._command_conflicts == [], r._command_conflicts
        assert not cap.warnings('命令冲突')
    finally:
        _detach(cap)


async def test_plugin_not_in_order_excluded():
    """by_plugin 中存在但未入 order 的插件不计入冲突，且不影响结果。"""
    cap = _attach()
    try:
        r = _router()
        by_plugin = {
            'A': [_cmd('^help')],
            'B': [_cmd('^help')],  # B 未入 order，实际不参与路由
        }
        r._detect_command_conflicts(by_plugin, ['A'])
        assert r._command_conflicts == [], r._command_conflicts
    finally:
        _detach(cap)


async def test_conflicts_reset_on_rebuild():
    """_command_conflicts 每次检测前刷新，旧冲突解决后不残留。"""
    r = _router()
    r._command_conflicts = [{'type': 'pattern', 'key': 'x',
                             'winner': 'A', 'losers': ['B']}]
    by_plugin = {'A': [_cmd('^help')], 'B': [_cmd('^about')]}
    r._detect_command_conflicts(by_plugin, ['A', 'B'])
    assert r._command_conflicts == []


if __name__ == '__main__':
    import asyncio
    for fn in (test_cross_plugin_pattern_conflict_detected,
               test_cross_plugin_alias_conflict_detected,
               test_no_conflict_when_patterns_distinct,
               test_plugin_not_in_order_excluded,
               test_conflicts_reset_on_rebuild):
        asyncio.run(fn())
        print(f"PASS {fn.__name__}")
