# -*- coding: utf-8 -*-
"""
LLM 对话核心（llm_core）

由官方装载器 llm_load 释放到 plugins/ 下，负责整条对话链路组装：

    原始消息 ──(media)──▶ 图片/文本
    文本+工具 ──(agent)──▶ 模型 ──▶ 工具调用 ──(tools)──▶ 结果 ──▶ 再问模型
                         ▲
                    (providers) 模型提供商总线

分层原则：**本插件不认识任何具体厂商**。默认内置 OpenAI 兼容提供商；要接别家，
写插件注册 provider，不需要改这里一行代码。工具同理，见 register_tool。

命令（默认前缀 /llm，可在配置里改）：

    /llm     <内容>    与模型对话（自动处理工具调用）
    /llmreset          清空当前会话
    /llmtools          列出可用工具及其开关
    /llmtool <名> on|off   开关某个工具（需管理员）
    /llmstatus         查看当前会话、工具、提供商概况

给别的插件用的服务面：

    svc = fw.services.get('llm_core')

    @svc.tool(description="查询城市天气")
    def get_weather(location: str):
        '''查某城市今天的天气，返回温度和天气状况。'''
        return "26 度，晴"

    svc.register_provider(MyProvider())      # 注册自己的模型提供商
    await svc.achat("你好", event=event)      # 在自己的插件里触发一次对话
"""
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from .agent import AgentResult, ToolLoopAgent
from .history import ConversationStore, SessionPersist, estimate_tokens, session_key
from .media import MediaCache, build_user_content, extract_images
from .mcp import bind_mcp_tools
from .providers import OpenAICompatProvider, Provider, ProviderError, ProviderRegistry
from .tools import FunctionTool, ToolRegistry, llm_function, schema_from_callable

logger = logging.getLogger('zcbot.llm_core')

# 模块级状态：register() 时填充，命令/view 函数通过它们拿到当前实例
_Svc: Optional['LLMCoreService'] = None
_Agent: Optional[ToolLoopAgent] = None
_Media: Optional[MediaCache] = None
_Store: Optional[ConversationStore] = None
_Service: Optional['LLMCoreService'] = None

# 对话闸门：外部插件（如黑名单管理）注册的准入检查。
# gate(user_id) -> 拒绝时返回提示文本，放行返回 None。任一闸门拒绝即拒绝。
_CHAT_GATES: List[Any] = []

__plugin_meta__ = {
    "name": "LLM 对话核心",
    "version": "1.0.6",
    "author": "ZGRIC",
    "desc": "模型提供商总线 + 函数（工具）总线 + 工具调用闭环 + 会话窗口治理",
    "priority": 100,
}

# 版本号仅用于 /llmstatus 展示与日志；llm_load 重新释放/重启是按 manifest 里
# 每个文件的 md5 比对触发的（改了源码重新打包即可，见 tools/build_llm_payload.py），
# 不依赖这个号是否递增。改动源码后照常 --write 重打包即可生效。
__version__ = '1.0.6'

_DEFAULT_SYSTEM_PROMPT = (
    "你是跑在聊天软件里的助手。回答要用口语，别写小作文，"
    "能用工具就直接用，别跟用户复述你要调用什么工具。"
)

_MAX_REPLY_CHARS = 1200

try:  # Flask 是可选依赖（启用 webui 插件时才安装）
    from flask import jsonify, request as flask_request
    _HAS_FLASK = True
except Exception:  # noqa: BLE001
    jsonify = None
    flask_request = None
    _HAS_FLASK = False


# ── 内置示例工具（也是写函数的最简样板） ───────────────────────

@llm_function(
    description="获取当前日期和时间，回答和日期、时间、星期有关的问题时用",
    param_descriptions={'dummy': '占位参数，不需要传'})
def get_current_time(dummy: str = ''):
    """获取当前日期和时间。"""
    return time.strftime('%Y-%m-%d %H:%M:%S %A')


@llm_function(
    description="计算两段文本表示的简单算术表达式（只含数字与加减乘除括号）",
    param_descriptions={'expression': '算术表达式，如 (12+8)*3'})
def calculator(expression: str):
    """计算简单算术表达式。"""
    import ast
    import operator
    expr = (expression or '').strip()
    if not expr:
        return "没有可计算的表达式"
    # AST 安全求值：只放行四则运算节点，不 eval 任何东西
    ops = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.USub: operator.neg, ast.UAdd: operator.pos,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
    max_pow = 10 ** 6  # 幂运算防爆炸：指数过大直接拒

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("指数过大")
            return ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.operand))
        raise ValueError("表达式含不支持的内容")
    try:
        tree = ast.parse(expr, mode='eval')
        value = _eval(tree)
        if isinstance(value, float) and value == int(value) and abs(value) < max_pow * 10:
            value = int(value)
        return f"{expr} = {value}"
    except Exception as e:  # noqa: BLE001
        return f"计算失败：{e}"


_BUILTIN_TOOLS = [get_current_time, calculator]


# ── 配置读取 ──────────────────────────────────────────────────

def _cfg(key: str, default=None):
    """读插件配置。配置项会自动出现在 Web 面板（源码静态发现）。"""
    if ctx is None:
        return default
    try:
        return ctx.get_config(key, default)
    except Exception:  # noqa: BLE001
        return default


def _cfg_list(key: str) -> List[Any]:
    """结构化配置：支持列表，也支持面板里写成 JSON 字符串"""
    value = _cfg(key, None)
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            logger.warning(f"配置项 {key} 不是合法 JSON，已忽略：{value[:120]}")
            return []
    if isinstance(value, dict):
        return [value]
    return list(value or [])


def _providers_config() -> List[dict]:
    """构造 provider 配置列表。

    密钥支持两种落法，都是「不进仓库」的：
      1. 面板里填 api_key
      2. 环境变量 LLM_CORE_API_KEY（面板留空时兜底）
    """
    cfgs = [c for c in _cfg_list('providers') if isinstance(c, dict)]
    if not cfgs:
        return cfgs
    env_key = os.environ.get('LLM_CORE_API_KEY', '')
    out = []
    for cfg in cfgs:
        cfg = dict(cfg)
        if not cfg.get('api_key') and env_key:
            cfg['api_key'] = env_key
        out.append(cfg)
    return out


def _personas_config() -> Dict[str, dict]:
    """人格列表：[{id, name, prompt}]，id 做命令参数，name 做展示。返回 {id: 项}"""
    out = {}
    for item in _cfg_list('personas'):
        if not isinstance(item, dict):
            continue
        pid = str(item.get('id') or '').strip()
        prompt = str(item.get('prompt') or '').strip()
        if pid and prompt:
            out[pid] = {'name': str(item.get('name') or pid),
                        'prompt': prompt}
    return out


def _persona_line(pid: str) -> str:
    """当前会话的人格状态描述"""
    conv = _current_conv()
    cur = conv.persona if conv is not None else ''
    cur_name = _personas_config().get(cur, {}).get('name', cur or '默认')
    return f"当前人格：{cur_name}" + (f"（{cur}）" if cur else '')


def _current_conv():
    """命令场景里取当前会话（取不到返回 None）"""
    try:
        source = getattr(ctx, '_current_bot', None) or ''
        ev = _persona_event
        if ev is None:
            return None
        return _Svc.store.get(session_key(
            source, ev.group_id if ev.is_group else None, ev.user_id), create=False)
    except Exception:  # noqa: BLE001
        return None


# /llm人格 处理过程中暂存的事件（纯命令流，无并发风险）
_persona_event = None


async def handle_persona(event, match):
    """查看/切换当前会话人格：/llm人格 列表 | /llm人格 <id> | /llm人格 reset"""
    global _persona_event
    if ctx is None or _Svc is None:
        return
    _persona_event = event
    try:
        arg = ((match.group(1) if match else '') or '').strip()
        personas = _personas_config()
        if not arg or arg in ('list', '列表'):
            lines = ['人格列表：', '默认（全局 system_prompt）']
            lines += [f"{pid} — {p['name']}" for pid, p in personas.items()]
            lines += ['', _persona_line(arg)]
            lines += ['', '切换：/llm人格 <id>；恢复默认：/llm人格 reset']
            await ctx.asend_msg(user_id=event.user_id,
                                group_id=event.group_id if event.is_group else None,
                                message='\n'.join(lines))
            return
        if arg in ('reset', '默认'):
            conv = _Svc.store.get(session_key(
                getattr(ctx, '_current_bot', None) or '',
                event.group_id if event.is_group else None, event.user_id))
            conv.persona = ''
            await ctx.asend_msg(user_id=event.user_id,
                                group_id=event.group_id if event.is_group else None,
                                message="已恢复默认人格")
            return
        if arg not in personas:
            await ctx.asend_msg(user_id=event.user_id,
                                group_id=event.group_id if event.is_group else None,
                                message=f"没有人格 [{arg}]，用 /llm人格 查看列表")
            return
        conv = _Svc.store.get(session_key(
            getattr(ctx, '_current_bot', None) or '',
            event.group_id if event.is_group else None, event.user_id))
        conv.persona = arg
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message=f"已切换人格：{personas[arg]['name']}（{arg}）")
    finally:
        _persona_event = None


# ── 服务门面 ──────────────────────────────────────────────────

class LLMCoreService:
    """对外暴露的服务门面（fw.services.get('llm_core')）。

    这里刻意只暴露「够用」的接口：注册工具、注册提供商、拉会话、触发一次对话。
    任何更深的东西请直接用模块 API，别指望这个门面面面俱到。
    """

    version = __version__

    llm_function = staticmethod(llm_function)
    FunctionTool = FunctionTool
    Provider = Provider
    OpenAICompatProvider = OpenAICompatProvider
    estimate_tokens = staticmethod(estimate_tokens)
    session_key = staticmethod(session_key)

    def __init__(self, registry: ToolRegistry, providers: ProviderRegistry,
                 store: ConversationStore, agent: ToolLoopAgent):
        self._registry = registry
        self._providers = providers
        self._store = store
        self._agent = agent

    # ---- 工具总线 ----

    def tool(self, name: str = None, description: str = None, **kwargs):
        """装饰器：把函数登记成可被模型调用的工具。

        用法同 tools.llm_function，区别是这里会立刻入库并标好归属插件名。
        """
        def deco(func):
            schema = kwargs.pop('parameters', None) or schema_from_callable(
                func, kwargs.pop('param_descriptions', None),
                kwargs.pop('required', None))
            tool = FunctionTool(
                name=name or func.__name__,
                description=description or (func.__doc__ or '').strip().split('\n')[0],
                parameters=schema,
                handler=func,
                owner=_caller_owner(),
                source='plugin',
                **{k: v for k, v in kwargs.items()
                   if k in ('timeout', 'require_perm', 'enabled')},
            )
            setattr(func, '__llm_tool__', tool)
            self.register_tool(tool)
            return func
        return deco

    def register_tool(self, tool: FunctionTool) -> FunctionTool:
        self._registry.register(tool)
        logger.info(f"注册工具 [{tool.name}]（来源 {tool.source}）")
        return tool

    def unregister_tool(self, name: str) -> bool:
        return self._registry.unregister(name)

    def unregister_by_owner(self, owner: str) -> int:
        return self._registry.unregister_by_owner(owner)

    def list_tools(self, enabled_only: bool = False) -> List[FunctionTool]:
        return self._registry.list_tools(enabled_only)

    def set_tool_enabled(self, name: str, enabled: bool) -> bool:
        return self._registry.set_enabled(name, enabled)

    # ---- 对话闸门 ----

    def register_chat_gate(self, gate) -> None:
        """注册对话准入闸门 gate(user_id) -> 拒绝提示 | None（放行）。

        典型用途：黑名单/风控插件在对话发起前做拦截，命中即拒绝本次对话。
        """
        if gate not in _CHAT_GATES:
            _CHAT_GATES.append(gate)
            logger.info("注册对话闸门")

    def unregister_chat_gate(self, gate) -> None:
        if gate in _CHAT_GATES:
            _CHAT_GATES.remove(gate)
            logger.info("移除对话闸门")

    # ---- 提供商总线 ----

    def register_provider(self, provider: Provider, default: bool = False) -> None:
        self._providers.register(provider, default=default)

    def unregister_provider(self, provider_id: str) -> bool:
        return self._providers.unregister(provider_id)

    def get_provider(self, provider_id: str = None) -> Optional[Provider]:
        return self._providers.get(provider_id)

    def list_providers(self) -> List[Provider]:
        return self._providers.list_providers()

    # ---- 会话与对话 ----

    def conversation(self, source: str = '', group_id=None, user_id=None) -> Any:
        return self._store.get(session_key(source, group_id, user_id))

    @property
    def store(self) -> ConversationStore:
        return self._store

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def achat(self, text: str, event=None, images: List[dict] = None,
                    source: str = '', provider_id: str = None) -> AgentResult:
        """在自己的插件里触发一次对话（自动带上工具调用闭环）。"""
        key = session_key(source, event.group_id if event and event.is_group else None,
                          event.user_id if event else None)
        conv = self._store.get(key)
        provider = self._providers.get(provider_id)
        conv.append({'role': 'user',
                     'content': build_user_content(
                         text, images or [],
                         bool(provider and provider.supports_vision))})
        return await self._agent.arun(conv, provider=provider, event=event)

    async def areply(self, text: str, event=None) -> str:
        """最简形式：只要一句回答"""
        result = await self.achat(text, event=event)
        return result.text or result.error


def _caller_owner() -> str:
    """猜出调用方所属插件模块名（用于工具归属，卸载时可整批清理）"""
    try:
        import inspect
        frame = inspect.currentframe()
        # 本帧 → tool/deco → 调用者
        caller = frame.f_back.f_back if frame and frame.f_back else None
        return caller.f_globals.get('__name__', '') if caller else ''
    except Exception:  # noqa: BLE001
        return ''


# ── 命令处理器 ────────────────────────────────────────────────

def _check_chat_gates(user_id) -> Optional[str]:
    """跑一遍对话闸门，返回第一个拒绝提示；全放行返回 None"""
    for gate in _CHAT_GATES:
        try:
            tip = gate(user_id)
        except Exception as e:  # noqa: BLE001 - 闸门故障不拦截对话
            logger.debug(f"[llm_core] 对话闸门异常: {e}")
            continue
        if tip:
            return str(tip)
    return None


async def handle_chat(event, match):
    """与模型对话（自动完成工具调用闭环）"""
    if ctx is None:
        return
    tip = _check_chat_gates(event.user_id)
    if tip:
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message=tip)
        return
    text = (match.group(1) if match else '') or ''
    text = text.strip()
    if not text:
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message="用法：/llm 你要说的话")
        return

    source = getattr(ctx, '_current_bot', None) or ''
    images = []
    if _cfg('vision_enabled', True):
        images = _Media.take(source, event.group_id if event.is_group else None,
                             event.user_id)

    conv = _Svc.store.get(session_key(source,
                                      event.group_id if event.is_group else None,
                                      event.user_id))
    try:
        provider = _resolve_provider_for(event, text)
    except ProviderError as e:
        # 未配置任何提供商：回一句人话引导去配 Key，而不是让命令处理器抛异常静默
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message=f"（LLM 未就绪）{e}")
        return
    conv.append({'role': 'user',
                 'content': build_user_content(
                     text, images, bool(provider and provider.supports_vision))})

    started = time.time()
    result = await _Agent.arun(conv, provider=provider, event=event,
                               temperature=float(_cfg('temperature', 0.7)),
                               max_tokens=int(_cfg('max_tokens', 1024)))
    elapsed = time.time() - started

    # 工具执行错了不写进历史，否则错误上下文会被下一轮复读一遍
    body = result.text or result.error or "（模型没有给出任何内容）"
    if result.ok and body:
        conv.append({'role': 'assistant', 'content': body})

    for chunk in _split_text(body):
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message=chunk)
    try:
        ctx.emit('llm.chat', {
            'ok': result.ok, 'turns': result.turns, 'elapsed_ms': int(elapsed * 1000),
            'tools': [c['name'] for c in result.tool_calls],
            'chars': len(body), 'model': result.model,
        })
    except Exception:  # noqa: BLE001
        pass


async def handle_reset(event, match):
    """清空当前会话"""
    source = getattr(ctx, '_current_bot', None) or ''
    key = session_key(source, event.group_id if event.is_group else None,
                      event.user_id)
    existed = _Svc.store.get(key)
    if existed:
        existed.clear()
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="已清空当前会话")


async def handle_tools(event, match):
    """列出当前可用工具（含必填参数，方便核对 schema 推导结果）"""
    tools = _Svc.list_tools()
    if not tools:
        msg = "当前没有任何工具"
    else:
        lines = []
        for t in tools:
            required = (t.parameters or {}).get('required') or []
            req = f"（必填：{', '.join(required)}）" if required else ""
            lines.append(
                f"[{t.source}] {'开' if t.enabled else '关'} {t.name} — "
                f"{t.description}{req}")
        msg = f"共 {len(tools)} 个工具：\n" + '\n'.join(lines)
        msg += "\n\n开关：/llmtool <工具名> on|off"
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message=_clip(msg))


async def handle_tool_toggle(event, match):
    """开关某个工具: /llmtool <名> on|off"""
    raw = (match.group(1) if match else '') or ''
    parts = raw.strip().split()
    if len(parts) < 2:
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message="用法：/llmtool <工具名> on|off")
        return
    name, flag = parts[0], parts[1].lower()
    if flag not in ('on', 'off', 'open', 'close', 'true', 'false'):
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message="第二段只能是 on 或 off")
        return
    enabled = flag in ('on', 'open', 'true')
    ok = _Svc.set_tool_enabled(name, enabled)
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message=(f"工具 [{name}] 已{'启用' if enabled else '停用'}"
                                 if ok else f"没有叫 [{name}] 的工具"))


async def handle_status(event, match):
    """查看 LLM 子系统概况"""
    stats = _Svc.store.stats()
    providers = _Svc.list_providers()
    tools = _Svc.list_tools(enabled_only=True)
    lines = [
        f"llm_core v{__version__}",
        f"提供商 {len(providers)} 个："
        + (', '.join(f"{p.label}({p.model})" for p in providers) or '无'),
        f"工具 {len(tools)}/{len(_Svc.list_tools())} 个启用",
        f"会话 {stats['sessions']}/{stats['capacity']}，"
        f"估算 {stats['estimated_tokens']} tokens",
    ]
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message='\n'.join(lines))


def on_raw(raw_event: dict, bot_name: str):
    """原始消息：只做一件事——把图片抠出来暂存，返回 None 表示不接管消息。"""
    if not _cfg('vision_enabled', True):
        return None
    try:
        images = extract_images(raw_event)
        if not images:
            return None
        _Media.put(bot_name or '', raw_event.get('group_id'),
                   raw_event.get('user_id'), images)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[llm_core] 提取图片失败: {e}")
    return None


async def on_free_chat(payload: dict):
    """自由对话模式：按触发策略决定哪些普通消息进模型（默认关闭，避免意外刷屏）"""
    if not _cfg('free_chat', False) or ctx is None:
        return
    if not isinstance(payload, dict):
        return
    text = (payload.get('message') or '').strip()
    if not text:
        return

    hit, text = _should_free_reply(
        text, payload.get('group_id'),
        _cfg('wake_words', '') or '',
        _cfg_list('free_chat_groups'),
        float(_cfg('reply_probability', 100)))
    if not hit or not text:
        return

    payload = dict(payload, message=text)

    class _FakeMatch:
        @staticmethod
        def group(_i):
            return text

    event = payload.get('event')
    if not hasattr(event, 'is_group'):  # dict 或缺失都归一成最小事件占位
        event = _FakeEvent(payload)
    try:
        await handle_chat(event, _FakeMatch())
    except Exception as e:  # noqa: BLE001
        logger.error(f"[llm_core] 自由对话处理失败: {e}")


def _should_free_reply(text: str, group_id,
                       wake_words: str, groups: list, probability: float):
    """自由对话触发判定，返回 (是否回复, 去掉唤醒词后的文本)。

    顺序即优先级：群白名单是硬门槛 → 唤醒词无条件命中（跳过概率）→
    概率抽签兜底。纯函数，方便单独测试。
    """
    text = (text or '').strip()
    if not text:
        return False, ''

    # 1. 群白名单：配置了就只在这些群生效（私聊 group_id 为空不受限）
    if groups:
        allowed = set()
        for g in groups:
            try:
                allowed.add(int(g))
            except (TypeError, ValueError):
                continue
        if group_id and int(group_id) not in allowed:
            return False, text

    # 2. 唤醒词：前缀命中就剥掉再进模型，消息中间出现也算命中但不去词
    words = [w.strip() for w in str(wake_words or '').replace('，', ',').split(',') if w.strip()]
    low = text.lower()
    for w in words:
        wl = w.lower()
        if low.startswith(wl):
            return True, text[len(w):].lstrip(' ，,。:：')
        if wl in low:
            return True, text

    # 3. 概率：100 = 开着 free_chat 就全接
    if probability >= 100:
        return True, text
    import random
    return random.random() * 100 < probability, text


class _FakeEvent:
    """free_chat 场景下的最小事件占位（payload 里有时会直接带 event）"""

    def __init__(self, payload: dict):
        self.user_id = payload.get('user_id')
        self.group_id = payload.get('group_id')
        self.is_group = bool(payload.get('group_id'))
        self.role = payload.get('role', 'member')
        self.message = payload.get('message', '')
        self.sender = payload.get('sender') or {'user_id': self.user_id}


# ── REST / 面板数据 ───────────────────────────────────────────

def _view_overview():
    """面板总览：提供商 / 工具 / 会话（密钥一律打码）"""
    providers = []
    for p in _Svc.list_providers():
        info = p.describe()
        info['default'] = p.id == _Svc._providers.default_id
        providers.append(info)
    tools = [{
        'name': t.name, 'source': t.source, 'owner': t.owner,
        'description': t.description, 'enabled': t.enabled,
        'required': (t.parameters or {}).get('required', []),
    } for t in _Svc.list_tools()]
    stats = _Svc.store.stats()
    return jsonify({'code': 0, 'data': {
        'version': __version__,
        'providers': providers,
        'tools': tools,
        'sessions': stats,
        'config': {
            'max_turns': _cfg('max_turns', 6),
            'history_turns': _cfg('history_turns', 20),
            'trigger': _cfg('trigger', '/llm'),
            'free_chat': bool(_cfg('free_chat', False)),
            'vision_enabled': bool(_cfg('vision_enabled', True)),
            'api_key_configured': bool(_any_api_key()),
        },
    }})


def _view_toggle_tool(name: str):
    body = flask_request.get_json(silent=True) or {} if flask_request else {}
    enabled = body.get('enabled')
    if enabled is None and flask_request is not None:
        enabled = flask_request.args.get('enabled')
    enabled = str(enabled).strip().lower() in ('1', 'true', 'on', 'yes')
    ok = _Svc.set_tool_enabled(name, enabled)
    return jsonify({'code': 0 if ok else 1,
                    'msg': 'ok' if ok else f'工具 {name} 不存在'})


def _view_clear_sessions():
    n = _Svc.store.clear_all()
    return jsonify({'code': 0, 'data': {'cleared': n}})


# ── 注册入口 ──────────────────────────────────────────────────

def register(plugin_ctx):
    global ctx, _Service, _Svc, _Agent, _Media, _Store
    ctx = plugin_ctx
    fw = ctx._framework

    # 1. 工具总线
    registry = ToolRegistry('llm_core')
    if _cfg('enable_builtin_tools', True):
        for fn in _BUILTIN_TOOLS:
            tool = getattr(fn, '__llm_tool__', None)
            if tool is not None:
                registry.register(tool.clone(owner='llm_core', source='builtin'))

    # 2. 提供商总线
    cfgs = _providers_config()
    if not cfgs:
        # 面板还没配时，给一个空壳占位：命令会提示去配 Key，而不是直接崩
        providers = ProviderRegistry()
    else:
        providers = ProviderRegistry.build(cfgs)

    # 3. 会话仓库（可落盘：重启后历史不丢）
    persist = None
    if _cfg('persist_sessions', True):
        try:
            persist = SessionPersist(os.path.join(ctx.get_data_dir(), 'llm_sessions'))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"会话持久化不可用，退回纯内存: {e}")
    _Store = ConversationStore(
        system_prompt=_cfg('system_prompt', _DEFAULT_SYSTEM_PROMPT),
        max_turns=int(_cfg('history_turns', 20)),
        capacity=int(_cfg('session_capacity', 500)),
        persist=persist,
    )

    # 4. Agent 循环
    def _perm_check(user_id, node, context=None, role=None):
        try:
            return ctx.has_perm(user_id, node, context, role)
        except Exception:  # noqa: BLE001
            return False

    def _pick_provider(_model=None):
        try:
            return providers.require()
        except ProviderError as e:
            raise ProviderError(str(e))

    _Agent = ToolLoopAgent(
        registry=registry,
        provider_getter=_pick_provider,
        max_turns=int(_cfg('max_turns', 6)),
        perm_check=lambda uid, node: _perm_check(uid, node),
        logger=logger,
    )
    _Media = MediaCache(ttl=int(_cfg('media_ttl', 300)))

    # 5. MCP：把远端 server 的工具拉进总线（失败不影响其它部分可用）
    mcp_cfg = [dict(c) for c in _cfg_list('mcp_servers') if isinstance(c, dict)]
    if mcp_cfg:
        try:
            summary = bind_mcp_tools(registry, mcp_cfg)
            logger.info(f"MCP 接入结果: {summary}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP 接入异常: {e}")

    # 6. 服务门面
    _Service = LLMCoreService(registry, providers, _Store, _Agent)
    _Svc = _Service
    fw.services.register('llm_core', _Service)
    # 兼容按模块名直接取用
    try:
        import sys
        sys.modules.setdefault('plugin_llm_core', sys.modules[__name__])
    except Exception:  # noqa: BLE001
        pass

    # 7. 命令
    trigger = _cfg('trigger', '/llm') or '/llm'
    ctx.command(trigger, handle_chat, alias="/chat", description="与模型对话")
    ctx.command("/llmreset", handle_reset, description="清空当前会话")
    ctx.command("/llmtools", handle_tools, description="列出可用工具")
    ctx.command("/llmtool", handle_tool_toggle, require_admin=True,
                description="开关某个工具: /llmtool <名> on|off")
    ctx.command("/llmstatus", handle_status, description="查看 LLM 子系统概况")
    ctx.command("/llm人格", handle_persona, alias="/llmpersona", description="查看/切换当前会话人格")

    # 8. 原始消息（截图片）与自由对话
    ctx.on_raw_message(on_raw)
    if _cfg('free_chat', False):
        ctx.on('message', on_free_chat)

    # 9. WebUI 面板页 + 数据接口
    try:
        ctx.webui(title="LLM 对话", entry="index.html", icon="◈", order=40, sidebar=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"注册 WebUI 页面失败: {e}")
    if _HAS_FLASK:
        ctx.register_api('/api/llm_core/overview', _view_overview,
                         methods=['GET'], description="LLM 子系统总览")
        ctx.register_api('/api/llm_core/tools/<name>', _view_toggle_tool,
                         methods=['POST', 'PUT'], description="开关工具")
        ctx.register_api('/api/llm_core/sessions/clear', _view_clear_sessions,
                         methods=['POST'], description="清空全部会话")
    else:
        logger.info("未检测到 Flask，LLM 面板接口暂不挂载（启用 webui 插件后重载即生效）")

    ctx.log(f"LLM 对话核心已就绪 v{__version__}："
            f"{len(providers.list_providers())} 个提供商 / "
            f"{len(registry.list_tools(enabled_only=True))} 个工具")


def unregister():
    global _Service, ctx
    _CHAT_GATES.clear()
    try:
        services = ctx._framework.services
        if hasattr(services, 'remove'):
            services.remove('llm_core')
        elif hasattr(services, 'unregister'):
            services.unregister('llm_core')
    except Exception:  # noqa: BLE001
        pass
    if _Service is not None:
        # 把本插件注册的工具一并清干净，避免重载后重复登记
        try:
            _Service.unregister_by_owner('llm_core')
        except Exception:  # noqa: BLE001
            pass
    _Service = None


# ── 小工具 ────────────────────────────────────────────────────

def _any_api_key() -> bool:
    if os.environ.get('LLM_CORE_API_KEY'):
        return True
    for cfg in _providers_config():
        if cfg.get('api_key'):
            return True
    return False


def _resolve_provider_for(event, text: str) -> Optional[Provider]:
    """按消息内容挑选 provider（支持在消息里临时 @ 模型名，如 /llm @gpt-4o 你好）"""
    try:
        import re
        m = re.match(r'^@([\w\-\.:/]+)\s+', text or '')
        if m:
            token = m.group(1)
            # 先按 provider id 精确指定，再按模型名模糊解析
            picked = _Svc._providers.get(token) or _Svc._providers.resolve(token)
            if picked is not None:
                return picked
        return _Svc._providers.require()
    except ProviderError:
        raise
    except Exception:  # noqa: BLE001
        return None


_SENT_SPLIT = re.compile(r'[^。！？!?\n…]*[。！？!?\n…]+|[^。！？!?\n…]+$')


def _split_text(text: str, size: int = None) -> List[str]:
    """按句子边界分段（标点后断句），段落超过上限才退回硬切。

    比固定字符数硬切自然得多：模型一段话通常带多个句号，硬切会把
    「……今天是周一」和「明天记得……」劈成两条消息。
    """
    if not text:
        return []
    if size is None:
        try:
            size = max(100, int(_cfg('max_reply_chars', _MAX_REPLY_CHARS)))
        except Exception:  # noqa: BLE001
            size = _MAX_REPLY_CHARS
    if len(text) <= size:
        return [text]

    chunks: List[str] = []
    buf = ''
    for sent in _SENT_SPLIT.findall(text):
        if not sent:
            continue
        # 单句就超限：先把手头攒的收掉，再把这句硬切
        while len(sent) > size:
            if buf:
                chunks.append(buf)
                buf = ''
            chunks.append(sent[:size])
            sent = sent[size:]
        if len(buf) + len(sent) > size:
            chunks.append(buf)
            buf = sent
        else:
            buf += sent
    if buf:
        chunks.append(buf)
    return chunks or [text]


def _clip(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n……已截断（共 {len(text)} 字）"
