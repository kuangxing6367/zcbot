"""验证 LLM 调用链追踪：agent.arun 在模型调用与工具执行处埋点，产出 result.trace。"""
import asyncio
import logging
import os
import sys

# 从 __file__ 推导源码目录，别写死本机绝对路径（CI 是 Linux，硬编码必挂）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_ROOT, 'core_plugins', 'llm_load', 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from llm_core.agent import ToolLoopAgent  # noqa: E402
from llm_core.providers import Provider, ProviderReply, ToolCall  # noqa: E402
from llm_core.tools import FunctionTool, ToolRegistry  # noqa: E402

_FAILED = False


def check(cond, msg):
    global _FAILED
    print('PASS' if cond else 'FAIL', msg)
    if not cond:
        _FAILED = True


class FakeProvider(Provider):
    """第一轮返回工具调用，第二轮收敛为文本。"""
    def __init__(self):
        super().__init__(id='fake', model='fake-m')
        self.n = 0

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
        self.n += 1
        if self.n == 1:
            return ProviderReply(
                tool_calls=[ToolCall(id='c1', name='echo', arguments={'x': 'hi'})],
                model='fake-m')
        return ProviderReply(content='done', model='fake-m')


def echo(x: str):
    return f'echo:{x}'


def boom(x: str):
    raise ValueError('boom')


def make_conv():
    class C:
        def __init__(self):
            self.msgs = []
        def as_request(self):
            return self.msgs
        def append(self, m):
            self.msgs.append(m)
    return C()


async def run():
    reg = ToolRegistry('t')
    reg.register(FunctionTool(
        name='echo', description='', parameters={'type': 'object',
        'properties': {'x': {'type': 'string'}}, 'required': ['x']},
        handler=echo, source='builtin', enabled=True))
    agent = ToolLoopAgent(registry=reg, provider=FakeProvider(), max_turns=6,
                          logger=logging.getLogger('t'))
    res = await agent.arun(make_conv())

    check(res.trace['turns'] == 2, f'turns==2，实际 {res.trace["turns"]}')
    spans = res.trace['spans']
    check(any(s['type'] == 'model' for s in spans), '存在 model span')
    check(any(s['type'] == 'tool' and s['name'] == 'echo' for s in spans),
          '存在 tool(echo) span')
    check(res.trace['total_ms'] >= 0, 'total_ms >= 0')
    ms = [s for s in spans if s['type'] == 'model'][0]
    check(all(k in ms for k in ('duration_ms', 'error', 'usage')), 'model span 字段完整')
    ts = [s for s in spans if s['type'] == 'tool'][0]
    check(ts['error'] is None, 'echo 成功时 error=None')
    check(len([s for s in spans if s['type'] == 'model']) == 2, '两轮各一次模型调用')

    # 失败工具场景：error 标记为 fail
    class FakeProviderBoom(FakeProvider):
        def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
            self.n += 1
            if self.n == 1:
                return ProviderReply(
                    tool_calls=[ToolCall(id='c1', name='boom', arguments={'x': 'hi'})],
                    model='fake-m')
            return ProviderReply(content='done', model='fake-m')

    reg2 = ToolRegistry('t2')
    reg2.register(FunctionTool(
        name='boom', description='', parameters={'type': 'object',
        'properties': {'x': {'type': 'string'}}, 'required': ['x']},
        handler=boom, source='builtin', enabled=True))
    agent2 = ToolLoopAgent(registry=reg2, provider=FakeProviderBoom(), max_turns=6,
                           logger=logging.getLogger('t2'))
    res2 = await agent2.arun(make_conv())
    ts2 = [s for s in res2.trace['spans'] if s['type'] == 'tool' and s['name'] == 'boom']
    check(ts2 and ts2[0]['error'] == 'fail', '失败工具标记 error=fail')


asyncio.run(run())


# ── 追踪历史保留与 API 语义（main._record_trace / _view_trace）──
import llm_core.main as main_mod  # noqa: E402


class _FakeArgs:
    def __init__(self, d):
        self._d = d

    def get(self, k):
        return self._d.get(k)


class _FakeReq:
    def __init__(self, d):
        self.args = _FakeArgs(d)
        self.method = 'GET'


def test_history():
    real_jsonify = main_mod.jsonify
    main_mod.jsonify = None  # 拿纯 dict，方便断言
    try:
        main_mod._TRACE_HISTORY.clear()
        for i in range(3):
            main_mod._record_trace({'model': f'm{i}', 'turns': i + 1,
                                    'total_ms': i * 10, 'spans': []})
        check(len(main_mod._TRACE_HISTORY) == 3, '记录 3 条历史')
        check(main_mod._TRACE_HISTORY[0]['model'] == 'm2', '索引 0 是最新一条')

        main_mod.flask_request = _FakeReq({'index': '2'})
        out = main_mod._view_trace()
        check(out['data']['model'] == 'm0', 'index=2 取到最早那条')
        check(len(out['history']) == 3 and out['history'][0]['index'] == 0,
              'history 摘要按最新在前')

        main_mod.flask_request = _FakeReq({})
        check(main_mod._view_trace()['data']['model'] == 'm2', '不带 index 默认最新')

        main_mod.flask_request = _FakeReq({'index': '99'})
        check(main_mod._view_trace()['data'] == {}, 'index 超界返回空')
        main_mod.flask_request = _FakeReq({'index': 'abc'})
        check(main_mod._view_trace()['data']['model'] == 'm2', '非法 index 回落最新')

        main_mod._TRACE_HISTORY.clear()
        for i in range(main_mod._TRACE_MAX + 5):
            main_mod._record_trace({'model': f'x{i}'})
        check(len(main_mod._TRACE_HISTORY) == main_mod._TRACE_MAX, '超过上限自动淘汰')
        check(main_mod._TRACE_HISTORY[0]['model'] == f'x{main_mod._TRACE_MAX + 4}',
              '淘汰旧的、保留最新的')
        main_mod._record_trace({})
        check(len(main_mod._TRACE_HISTORY) == main_mod._TRACE_MAX, '空 trace 不入库')
    finally:
        main_mod.jsonify = real_jsonify
        main_mod.flask_request = None
        main_mod._TRACE_HISTORY.clear()


test_history()

if _FAILED:
    print('RESULT: FAIL')
    sys.exit(1)
print('RESULT: ALL PASS')
