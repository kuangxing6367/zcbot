# -*- coding: utf-8 -*-
"""智能体循环（工具调用闭环）。

把用户的话交给模型 → 模型流式输出文本 / 点名要调哪只「手」（工具）→ 本地执行
→ 结果回灌 → 模型再说话，直到不再调用工具。多轮收敛，到达 max_rounds 强制收口。

整轮以「事件」对外汇报（text / tool_start / tool_end /
error / done），终端据此实时渲染过程；循环本身不打印，便于测试与复用。
"""
import json

try:  # 包导入（测试 / 包内引用）
    from .llm import LLMError, stream_chat
except ImportError:  # 框架 loader 以「顶层模块」加载本插件：无父包，退回同目录绝对导入
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from llm import LLMError, stream_chat


class Agent:
    """一次会话内的智能体执行器（无状态之外仅持 cfg / tools / 事件回调）。"""

    def __init__(self, cfg, tools, on_event=None, should_stop=None):
        self.cfg = cfg
        self.tools = tools
        self._emit = on_event or (lambda kind, payload=None: None)
        self._should_stop = should_stop or (lambda: False)

    # ── 主循环 ─────────────────────────────────────────────────────────
    def run(self, messages):
        """就地把一轮交互追加进 messages，返回本轮最终文本。

        messages 需自带 system 提示（见 prompt.build）。返回值为模型最后一段
        自然语言（可能为空字符串）；历史与工具轮均已写入 messages。
        """
        tool_specs = self.tools.specs()
        max_rounds = max(1, int(self.cfg.get('max_rounds', 12) or 12))
        last_text = ''

        for _ in range(max_rounds):
            if self._should_stop():
                return last_text or '(已取消)'

            text_acc, calls = self._one_turn(messages, tool_specs)
            last_text = text_acc

            if not calls:
                messages.append({'role': 'assistant', 'content': text_acc})
                return text_acc or '(模型未返回内容)'

            # 记录 assistant 的工具调用（OpenAI 兼容格式）
            asst_calls = []
            for i, c in enumerate(calls):
                asst_calls.append({
                    'id': 'c%d' % i,
                    'type': 'function',
                    'function': {'name': c['name'], 'arguments': c.get('arguments', '')},
                })
            messages.append({'role': 'assistant', 'content': text_acc or None,
                             'tool_calls': asst_calls})

            # 逐个执行工具并回灌
            for i, c in enumerate(calls):
                if self._should_stop():
                    return last_text or '(已取消)'
                try:
                    args = json.loads(c.get('arguments') or '{}')
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                self._emit('tool_start', {'name': c['name'], 'args': args})
                try:
                    out = self.tools.call(c['name'], args)
                    ok = True
                except Exception as e:  # noqa: BLE001 —— 工具异常回灌给模型，不外抛
                    out = '错误: %s' % e
                    ok = False
                self._emit('tool_end', {'name': c['name'], 'args': args,
                                        'ok': ok, 'result': str(out)})
                messages.append({
                    'role': 'tool',
                    'tool_call_id': 'c%d' % i,
                    'content': str(out)[:4000],
                })

        self._emit('error', {'message': '达到最大轮数（%d），已强制收口' % max_rounds})
        return last_text or '(达到最大轮数，已强制收口)'

    # ── 单轮：流式取回文本与工具调用 ────────────────────────────────────
    def _one_turn(self, messages, tool_specs):
        text_acc = ''
        calls = []
        try:
            for kind, val in stream_chat(self.cfg, messages, tool_specs):
                if kind == 'text':
                    text_acc += val
                    self._emit('text', val)
                elif kind == 'reason':
                    self._emit('reason', val)
                elif kind == 'tool':
                    calls.append(val)
        except LLMError as e:
            self._emit('error', {'message': str(e)})
            raise
        return text_acc, calls
