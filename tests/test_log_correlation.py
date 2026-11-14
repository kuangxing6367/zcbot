"""回归测试：日志事件关联 ID（缺口 6）。

背景（真实缺陷）：框架早有 current_event_id_var，但从未被 .set()，所有日志
都无法按事件串联。本测试验证：
1. _EventIdFilter 把 contextvar 的 eid 注入每条记录（无上下文时为 ''）；
2. 文本格式串能渲染 eid，且不污染无上下文的普通日志；
3. dispatch._process_event 在事件处理跨度内 set/reset current_event_id_var。

直接运行：PYTHONPATH=. python tests/test_log_correlation.py
或：PYTHONPATH=. python -m pytest tests/test_log_correlation.py -q
"""
import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, '.')

from framework.runtime import current_event_id_var


def _record():
    return logging.LogRecord('zcbot', logging.INFO, 'path', 1, 'hello', None, None)


async def test_event_id_filter_injects_var():
    from framework.core.base import _EventIdFilter
    f = _EventIdFilter()
    token = current_event_id_var.set('evt-xyz')
    try:
        rec = _record()
        assert f.filter(rec) is True
        assert rec.zcbot_eid == 'evt-xyz'
    finally:
        current_event_id_var.reset(token)
    # 无上下文 → ''
    rec2 = _record()
    f.filter(rec2)
    assert rec2.zcbot_eid == ''


async def test_text_formatter_renders_eid():
    from framework.core.base import _EventIdFilter
    fmt = logging.Formatter(
        '[%(asctime)s] %(levelname)s%(zcbot_eid)s %(message)s')
    token = current_event_id_var.set('evt-9')
    try:
        rec = _record()
        _EventIdFilter().filter(rec)
        out = fmt.format(rec)
        assert 'evt-9' in out and 'hello' in out
    finally:
        current_event_id_var.reset(token)
    # 无 eid 时格式串不产生多余标记
    rec2 = _record()
    _EventIdFilter().filter(rec2)
    out2 = fmt.format(rec2)
    assert 'evt-' not in out2 and out2.endswith('hello')


async def test_json_formatter_includes_eid():
    import json

    from framework.jsonlog import JsonFormatter
    token = current_event_id_var.set('evt-j')
    try:
        rec = _record()
        rec.zcbot_eid = 'evt-j'
        payload = json.loads(JsonFormatter().format(rec))
        assert payload.get('eid') == 'evt-j'
    finally:
        current_event_id_var.reset(token)


async def test_dispatch_process_event_injects_eid():
    from framework.core.dispatch import FrameworkDispatchMixin
    captured = {}
    m = SimpleNamespace()
    m.config = {'log': {'log_raw_message': False}}
    m._raw_message_handlers = []
    m.event_bus = SimpleNamespace(aemit=AsyncMock())
    m.stats_writer = SimpleNamespace(register_user=lambda *a, **k: None)
    m.hooks = SimpleNamespace(trigger_async=AsyncMock())
    m.log_broker = SimpleNamespace(log_message=lambda *a, **k: None,
                                   log=lambda *a, **k: None)

    async def _route(ev, bot):
        captured['eid'] = current_event_id_var.get()

    m.router = SimpleNamespace(route=_route)

    async def _no_raw(ev, bot):
        return False

    m._dispatch_raw_message_handlers = _no_raw

    event = {
        'type': 'message', 'message_type': 'private', 'user_id': 1,
        'message': [{'type': 'text', 'data': {'text': '/help'}}],
        '_bb_eid': 'evt-abc', 'bot_name': 'default',
    }
    await FrameworkDispatchMixin._process_event(m, event)
    assert captured['eid'] == 'evt-abc', captured
    # 处理后应 reset，不泄漏到后续事件
    assert current_event_id_var.get() is None


if __name__ == '__main__':
    for fn in (test_event_id_filter_injects_var,
               test_text_formatter_renders_eid,
               test_json_formatter_includes_eid,
               test_dispatch_process_event_injects_eid):
        asyncio.run(fn())
        print(f"PASS {fn.__name__}")
