"""回归：消息首尾「隐形空白」导致命令匹配失败（平台多吐一个空格就不回复）。

现象：客户端看起来完全正常的一条 `/help`，机器人无论如何都不响应。
原因：适配器/平台（或被输入法塞进的全角空格、零宽字符）在正文前后多带了字符，
`"/help"` 实际是 `" /help"`，前缀匹配从第一个字符就对不上，整条命令静默失配；
命中率还被首字符倒排索引放大——首字符是空格时连"可剪枝"都进不去。

覆盖：命令匹配 API 本身、Event 构造（含 reply 在前的 @ 剥离 / CQ 字符串）、
系统关键词规则、以及走真实 `_match_plugin_commands` 的端到端命中。

直接运行：PYTHONPATH=. python tests/test_message_whitespace.py
或：PYTHONPATH=. python -m pytest tests/test_message_whitespace.py -q
"""
import sys
from types import SimpleNamespace

sys.path.insert(0, '.')

from framework.messaging.event import (
    Event,
    _strip_bot_at_prefix,
    normalize_match_text,
)
from framework.messaging.router import MessageRouter, _RouteCommand
from framework.messaging.router_keywords import _KeywordRule
from framework.messaging.router_match import _PluginRoute

# 平台/输入法会带进来的各类空白
LEADING_WS = [
    ('半角空格', ' '),
    ('全角空格', '\u3000'),
    ('不间断空格', '\u00a0'),
    ('制表符', '\t'),
    ('若干半角空格', '   '),
]
# 零宽字符不是 `str.isspace()`，只能在 Event 规范化层被删掉，单列
INVISIBLE = [('零宽空格', '\u200b'), ('零宽连接符', '\u200d'), ('BOM', '\ufeff')]


# ── 1. 归一化函数 ─────────────────────────────────────────────

def test_normalize_strips_and_maps_space_like():
    assert normalize_match_text(' /help') == '/help'
    assert normalize_match_text('/help\u3000') == '/help'
    assert normalize_match_text('\u00a0签到\u00a0') == '签到'
    assert normalize_match_text('  /help  ') == '/help'
    # 正文内部普通空格保留（命令参数不能被吞）
    assert normalize_match_text('/echo a  b') == '/echo a  b'


def test_normalize_removes_invisible_chars():
    for _, ch in INVISIBLE:
        assert normalize_match_text(ch + '/help' + ch) == '/help'
    assert normalize_match_text('签\u200b到') == '签到'


def test_strip_bot_at_prefix_variants():
    assert _strip_bot_at_prefix('[@10001] /help', 10001) == '/help'
    assert _strip_bot_at_prefix('[@10001]/help', 10001) == '/help'
    assert _strip_bot_at_prefix('[CQ:at,qq=10001] /help', 10001) == '/help'
    # 连续两个 @（重复艾特）也剥干净
    assert _strip_bot_at_prefix('[@10001][@10001] /help', 10001) == '/help'
    # 纯 @ 无内容：保留原文，不清成空串
    assert _strip_bot_at_prefix('[@10001]', 10001) == '[@10001]'
    # 别人的 @ 不动（@某人 /help 不应被当成本机命令）
    assert _strip_bot_at_prefix('[@999] /help', 10001) == '[@999] /help'


# ── 2. 匹配 API 本身 ──────────────────────────────────────────

def test_match_simple_accepts_leading_whitespace():
    for name, ws in LEADING_WS:
        m = MessageRouter._match_simple('/help', ws + '/help')
        assert m is not None, f'{name}：前导空白导致命令失配'
        assert m.group(1) == '', name


def test_match_simple_accepts_trailing_whitespace():
    for name, ws in LEADING_WS:
        assert MessageRouter._match_simple('/help', '/help' + ws) is not None, name


def test_match_simple_sep_any_whitespace():
    for name, ws in LEADING_WS:
        m = MessageRouter._match_simple('/echo', '/echo' + ws + 'hello')
        assert m is not None, name
        assert m.group(1) == 'hello', name


def test_match_simple_keeps_inner_args_spaces():
    m = MessageRouter._match_simple('/echo', '/echo a  b')
    assert m is not None
    assert m.group(1) == 'a  b'
    # group(0) 永远是原始消息
    assert m.group(0) == '/echo a  b'


def test_match_simple_still_rejects_other_prefix():
    assert MessageRouter._match_simple('/help', '/helper') is None
    assert MessageRouter._match_simple('/help', '/hel px') is None


# ── 3. Event 构造（生产入口）────────────────────────────────

def _ev(message, self_id=10001):
    return Event({
        'post_type': 'message', 'message_type': 'group', 'self_id': self_id,
        'user_id': 2, 'group_id': 3, 'message': message,
    })


def test_event_message_has_no_leading_whitespace():
    for name, ws in LEADING_WS:
        assert _ev(ws + '/help').message == '/help', name
    for name, ch in INVISIBLE:
        assert _ev(ch + '/help').message == '/help', name
    for name, ws in LEADING_WS:
        assert _ev('/help' + ws).message == '/help', name
    for name, ch in INVISIBLE:
        assert _ev('/help' + ch).message == '/help', name


def test_event_message_at_bot_with_reply_first():
    """群里「先回复消息、再 @机器人」：@ 段不在第 0 位，旧实现漏剥离。"""
    ev = _ev([
        {'type': 'reply', 'data': {'id': 9}},
        {'type': 'at', 'data': {'qq': 10001}},
        {'type': 'text', 'data': {'text': ' /help'}},
    ])
    assert ev.message == '/help'
    # 原始 segments / raw_message 不受影响
    assert ev.segments[0]['type'] == 'reply'
    assert ev.has_at_bot


def test_event_message_at_bot_fullwidth_space():
    ev = _ev([
        {'type': 'at', 'data': {'qq': 10001}},
        {'type': 'text', 'data': {'text': '\u3000/help'}},
    ])
    assert ev.message == '/help'


def test_event_message_cq_string_at_prefix():
    """adapter 直接给 CQ 字符串时（不做段解析），@ 前缀是文本形态。"""
    ev = _ev('[CQ:at,qq=10001] /help')
    assert ev.message == '/help'


def test_event_raw_message_kept_verbatim():
    raw = ' /help '
    ev = _ev(raw)
    assert ev.message == '/help'
    assert ev.raw_message == raw, 'raw_message 必须保留原文，插件可能要看原始内容'


# ── 4. 系统关键词规则 ────────────────────────────────────────

def _keyword_rule(keyword, match_type='exact'):
    return _KeywordRule(id=1, keyword=normalize_match_text(keyword), response='ok',
                        match_type=match_type, rx=None, handler='',
                        plugin_name='system')


def test_keyword_rule_tolerates_whitespace_on_both_sides():
    """库里的关键词带尾随空格、消息带前导全角空格，两边都能对上。"""
    class _R(MessageRouter):
        def __init__(self):        # 只测匹配，不跑 __init__ 的依赖装配
            self._keyword_rules = [_keyword_rule('签到 ')]

    r = _R()
    # _match_keyword 的契约是「已规范化的匹配文本」（route 传的 ev.message），
    # 这里沿生产路径取：消息先进 Event 再进关键词匹配。
    assert r._match_keyword(_ev('签到').message) is not None
    assert r._match_keyword(_ev('\u3000签到\u3000').message) is not None
    assert r._match_keyword(_ev('\u200b签到').message) is not None
    assert r._match_keyword(_ev('签到啦').message) is None, 'exact 不该前缀误命中'


# ── 5. 端到端：走真实 _match_plugin_commands ─────────────────

class _Hooks:
    async def trigger_async(self, *a, **k):
        return []


class _FW:
    db = None
    stats_writer = None
    hooks = _Hooks()

    async def reply_text(self, ev, text):
        raise AssertionError(f'不该走到权限回复：{text}')


class _Ev:
    """最小事件替身：只提供匹配环节读取的字段。"""
    role = 'member'
    is_admin = False
    is_superuser = False
    user_id = 1
    group_id = 2
    bot_name = 'default'
    adapter = 'onebot'

    def has_perm(self, node):
        return True


async def test_plugin_command_matches_with_leading_space():
    calls = []

    async def on_help(ev, match):
        calls.append(match.group(1))

    module = SimpleNamespace(on_help=on_help)
    r = MessageRouter(_FW())
    cmd = _RouteCommand(id=1, pattern='/help', handler_name='on_help',
                        require_level='', rx=None, simple='/help', aliases=[])
    entry = _PluginRoute(module, [cmd])
    ev = _Ev()

    for name, ws in LEADING_WS:
        calls.clear()
        hit = await r._match_plugin_commands(entry, ev, ws + '/help', 'demo')
        assert hit is True, f'{name}：命令没命中，机器人不会回复'
        assert calls == [''], name
    assert ev._category_command == 'general'


async def test_plugin_command_args_survive_whitespace():
    got = []

    async def on_echo(ev, match):
        got.append(match.group(1))

    module = SimpleNamespace(on_echo=on_echo)
    r = MessageRouter(_FW())
    cmd = _RouteCommand(id=1, pattern='/echo', handler_name='on_echo',
                        require_level='', rx=None, simple='/echo', aliases=[])
    entry = _PluginRoute(module, [cmd])

    assert await r._match_plugin_commands(entry, _Ev(), ' /echo  a  b ', 'demo') is True
    assert got == ['a  b'], got


async def test_plugin_command_unmatched_when_empty():
    """纯空白消息不该命中命令（规范化后为空）。"""
    async def on_help(ev, match):
        raise AssertionError('不该命中')

    module = SimpleNamespace(on_help=on_help)
    r = MessageRouter(_FW())
    cmd = _RouteCommand(id=1, pattern='/help', handler_name='on_help',
                        require_level='', rx=None, simple='/help', aliases=[])
    entry = _PluginRoute(module, [cmd])

    assert await r._match_plugin_commands(entry, _Ev(), '', 'demo') is False


# ── 6. IIPC 插件侧事件（独立 SDD 实现，需同口径）────────────

def test_sdk_event_normalizes_same_way():
    from framework.iipc.py_sdk.event import Event as SdkEvent

    sdk_ev = SdkEvent({
        'post_type': 'message', 'message_type': 'group', 'self_id': 10001,
        'user_id': 2, 'group_id': 3,
        'message': [{'type': 'at', 'data': {'qq': 10001}},
                    {'type': 'text', 'data': {'text': '\u3000 /help'}}],
    })
    assert sdk_ev.message == '/help', sdk_ev.message


if __name__ == '__main__':
    import asyncio
    import traceback

    sync_tests = [v for k, v in sorted(globals().items())
                  if k.startswith('test_') and not asyncio.iscoroutinefunction(v)]
    async_tests = [v for k, v in sorted(globals().items())
                   if k.startswith('test_') and asyncio.iscoroutinefunction(v)]
    failed = 0
    for fn in sync_tests:
        try:
            fn()
            print(f'PASS {fn.__name__}: {(fn.__doc__ or "").strip()}')
        except Exception:
            failed += 1
            print(f'FAIL {fn.__name__}')
            traceback.print_exc()
    for fn in async_tests:
        try:
            asyncio.run(fn())
            print(f'PASS {fn.__name__}: {(fn.__doc__ or "").strip()}')
        except Exception:
            failed += 1
            print(f'FAIL {fn.__name__}')
            traceback.print_exc()
    print(f'\n{len(sync_tests) + len(async_tests) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
