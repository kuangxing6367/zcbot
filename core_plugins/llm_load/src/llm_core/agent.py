# -*- coding: utf-8 -*-
"""
Agent 循环（tool loop）

模型提供商只做「单轮补全」：喂进去历史和工具列表，吐出来一段文本或一串工具调用
指令。真正让模型「把事办完」的是这一层的循环——也就是常说的 ReAct：

    给模型问题和工具 → 模型说要调什么 → 我们执行 → 把结果塞回历史
    → 再问模型 → 它看到结果决定还要不要再调一次 → 直到它给出最终回答

几个必须做对的细节：

- **中间状态要进历史。** 模型必须看得见自己刚才调了什么、结果是什么，否则下一轮
  它会重复调用同一个工具。
- **最后一轮要收口。** 到达最大轮数还在调工具时，去掉 tools 再问一次，逼它基于
  已有结果给出结论，而不是把半截工具结果甩给用户。
- **工具失败要回灌。** 参数写错、超时、没权限，都做成一条工具结果让模型自己改，
  而不是终止整个会话。
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .providers import Provider, ProviderError, ProviderReply
from .tools import ToolRegistry

__all__ = ['AgentResult', 'ToolLoopAgent']

# 轮数用尽时的收口提示：明确告诉模型不许再调工具（不进历史，避免污染上下文）
_NUDGE = ("工具调用轮数已达上限。请基于上面已经拿到的工具结果直接给出最终回答，"
          "不要再请求调用任何工具。")


@dataclass
class AgentResult:
    """一次完整对话的结果"""

    text: str = ''
    error: str = ''
    turns: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    model: str = ''

    @property
    def ok(self) -> bool:
        return not self.error

    def brief(self) -> str:
        """给日志用的一行摘要"""
        names = ' → '.join(c['name'] for c in self.tool_calls) or '无'
        return f"turns={self.turns} tools=[{names}] chars={len(self.text or '')}"


class ToolLoopAgent:
    """工具调用循环执行器。"""

    def __init__(self,
                 registry: ToolRegistry,
                 provider: Provider = None,
                 provider_getter: Callable[[Optional[str]], Provider] = None,
                 max_turns: int = 6,
                 perm_check: Callable = None,
                 logger=None):
        """
        :param registry: 工具注册表
        :param provider: 固定使用的提供商（与 provider_getter 二选一）
        :param provider_getter: 每次调用前解析提供商，支持按消息动态切换模型
        :param max_turns: 工具调用的最大轮数，超出后强制收口
        :param perm_check: 权限判定回调 check(user_id, node, context, role) -> bool
        """
        self.registry = registry
        self.provider = provider
        self.provider_getter = provider_getter
        self.max_turns = max(1, int(max_turns))
        self.perm_check = perm_check
        self.log = logger

    # ---- 内部工具 ----

    def _resolve_provider(self, preferred: Provider = None) -> Provider:
        if preferred is not None:
            return preferred
        if self.provider_getter is not None:
            return self.provider_getter(None)
        if self.provider is not None:
            return self.provider
        raise ProviderError("没有可用的模型提供商")

    def _debug(self, msg: str) -> None:
        if self.log is not None:
            self.log.debug(f"[agent] {msg}")

    async def _call_provider(self, provider: Provider, messages: List[dict],
                             tools: Optional[List[dict]], temperature: float,
                             max_tokens: int) -> ProviderReply:
        """把同步 HTTP 请求丢到线程池，不阻塞事件循环"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: provider.chat(messages, tools, temperature, max_tokens))

    def _tool_decls(self, last_round: bool, allow_tools: bool = True) -> Optional[List[dict]]:
        """本轮携带的工具声明。最后一轮不带工具，逼模型收口。

        allow_tools=False 只影响本轮调用（如提供商不支持函数调用），
        绝不动注册表里的全局开关——那是用户自己的配置。
        """
        if last_round or not allow_tools:
            return None
        decls = self.registry.to_openai_tools()
        return decls or None

    # ---- 主循环 ----

    async def arun(self, conversation, provider: Provider = None,
                   temperature: float = 0.7, max_tokens: int = 1024,
                   event=None) -> AgentResult:
        """跑一轮完整对话（可能包含多轮工具调用）。

        :param conversation: Conversation 对象，执行过程中会被就地追加中间消息
        """
        try:
            provider = self._resolve_provider(provider)
        except ProviderError as e:
            return AgentResult(error=str(e))

        allow_tools = bool(provider.supports_tools)
        if not allow_tools:
            self._debug(f"提供商 [{provider.label}] 不支持函数调用，本轮不携带工具声明")

        result = AgentResult(model=getattr(provider, 'model', ''))

        try:
            for turn in range(1, self.max_turns + 1):
                result.turns = turn
                last_round = turn >= self.max_turns
                messages = conversation.as_request()
                decls = self._tool_decls(last_round, allow_tools)

                reply = await self._call_provider(
                    provider, messages, decls, temperature, max_tokens)

                if not reply.has_tool_calls:
                    result.text = reply.content or ''
                    result.usage = reply.usage or {}
                    if last_round and not result.text:
                        result.text = "这个问题我处理到一半就卡住了，换个说法再问我试试。"
                    conversation.append({'role': 'assistant', 'content': result.text})
                    self._debug(f"第 {turn} 轮收敛：{result.brief()}")
                    return result

                # assistant 消息要带 tool_calls 一起入历史，模型下次才看得见自己做过什么
                conversation.append({
                    'role': 'assistant',
                    'content': reply.content or '',
                    'tool_calls': [{
                        'id': call.id,
                        'type': 'function',
                        'function': {'name': call.name, 'arguments': call.raw_arguments or '{}'},
                    } for call in reply.tool_calls],
                })

                for call in reply.tool_calls:
                    tool = self.registry.get(call.name)
                    if tool is None or not tool.enabled:
                        outcome = f"[工具 {call.name}] 不存在或已被停用"
                    else:
                        outcome = await self.registry.aexecute(
                            tool, call.arguments, event=event,
                            perm_check=self.perm_check)
                    result.tool_calls.append({
                        'name': call.name,
                        'arguments': call.arguments,
                        'result': (outcome or '')[:2000],
                    })
                    conversation.append({
                        'role': 'tool',
                        'tool_call_id': call.id,
                        'name': call.name,
                        'content': outcome if outcome is not None else '',
                    })

                self._debug(f"第 {turn} 轮执行 {len(reply.tool_calls)} 个工具")
        except ProviderError as e:
            result.error = str(e)
        except Exception as e:  # noqa: BLE001 - 任何异常都不该炸掉会话
            result.error = f"对话异常：{e}"

        # 到这里说明始终收敛不了：去掉工具 + 显式收口提示再问一次，让它给结论
        if not result.text and not result.error:
            try:
                messages = conversation.as_request() + [{'role': 'user', 'content': _NUDGE}]
                reply = await self._call_provider(
                    provider, messages, None, temperature, max_tokens)
                text = reply.content
                if reply.has_tool_calls or not text:
                    text = _summarize_tool_outputs(result.tool_calls)
                result.text = text
                conversation.append({'role': 'assistant', 'content': text})
            except Exception as e:  # noqa: BLE001
                result.error = f"收口请求失败：{e}"
        return result


def _summarize_tool_outputs(calls: List[Dict[str, Any]]) -> str:
    """模型一直不出结论时，把它已经拿到的工具结果兜底整理给用户。

    比甩一句「处理失败」有用得多：多数情况下工具结果里已经有答案了，
    只是模型没能把它组织成人话。
    """
    if not calls:
        return "这个问题我反复尝试也没得到结论，换个说法再问一次试试。"
    lines = ["（工具调用轮数已达上限，以下是已经拿到的结果）"]
    for call in calls[-5:]:
        body = (call.get('result') or '').strip().replace('\n', ' ')
        if len(body) > 160:
            body = body[:160] + '…'
        lines.append(f"- {call.get('name')}: {body}")
    return '\n'.join(lines)
