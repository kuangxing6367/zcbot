# -*- coding: utf-8 -*-
"""
函数（工具）总线

这一层是全插件的核心概念，把「函数」这件事讲清楚只需要四个词：

    name          函数叫什么。模型靠它点名字调用，必须是稳定的英文标识
    description   一句话描述。写给**模型**看的，不是写给人看的注释
    parameters    参数的 JSON Schema。这是一份契约，不是文档
    handler       真正被执行的代码

三条必须记住的规则：

1. **描述的质量决定调用率。** 模型不知道你的函数内部怎么实现，它只看到
   名字和描述。写成「处理东西」这种模糊描述，结果就是永远不会被调用；
   写成「查询某个城市今天的天气，返回温度和天气状况」，模型就知道什么时候用。

2. **参数 schema 是双向契约。** 模型照它产出参数，我们在执行前按它校验。
   校验不通过时**不要硬着头皮执行**，而是把错误信息作为工具结果回灌给模型，
   让它自己改正再来一次——这才是工具调用比硬编码流程强的地方。

3. **返回值语义。** 返回字符串 → 进入下一轮对话，模型据此生成最终回复；
   返回 None → 不进入上下文（适合「已经自己把结果发出去了」的工具，
   比如自己发了图片、自己发了长消息）。

注册方式有两种，等价：

    # 1. 装饰器（适合给自己插件内部的函数登记）
    @llm_function(description="查询天气")
    def get_weather(location: str):
        '''查询某个城市今天的天气。'''
        return "26 度，晴"

    # 2. 显式注册（适合外部/动态来源，如 MCP server 拉下来的工具）
    tool = FunctionTool(name="get_weather", description="...",
                        parameters={...}, handler=my_func, source="mcp")
    registry.register(tool)

对外接口由 llm_core 的服务门面暴露：

    svc = fw.services.get('llm_core')
    svc.register_tool(tool)
    svc.unregister_tool("get_weather")
    svc.list_tools()
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('zcbot')

__all__ = [
    'FunctionTool', 'ToolCallError', 'ToolRegistry',
    'llm_function', 'schema_from_callable',
]


# JSON Schema 类型名。OpenAI 兼容层只认这几种，多一个都不行
_JSON_TYPES = ('string', 'number', 'integer', 'boolean', 'object', 'array')

# Python 注解 → JSON Schema 类型
_PY_TO_JSON = {
    'str': 'string',
    'int': 'integer',
    'float': 'number',
    'bool': 'boolean',
    'dict': 'object',
    'list': 'array',
    'tuple': 'array',
}


class ToolCallError(Exception):
    """工具调用失败。

    执行器会把它转成一条普通的工具结果回灌给模型（而不是抛炸会话），
    这样模型能看到错误原因并自行修正参数重试。
    """


def _json_type_of(annotation: Any) -> str:
    """把 Python 类型注解翻译成 JSON Schema 类型名。

    注解缺失、不可识别时一律按 string 处理——宁可让模型传字符串进来我们自己转，
    也不要因为类型不明就直接拒绝开始。
    """
    if annotation is inspect.Parameter.empty:
        return 'string'
    if isinstance(annotation, str):
        return _PY_TO_JSON.get(annotation, 'string')
    name = getattr(annotation, '__name__', None)
    if name:
        return _PY_TO_JSON.get(name, 'string')
    text = str(annotation)
    if text.startswith('typing.'):
        return 'string'
    for py_name, json_name in _PY_TO_JSON.items():
        if py_name in text:
            return json_name
    return 'string'


def _first_line(doc: Optional[str]) -> str:
    """取 docstring 第一行作为默认描述"""
    if not doc:
        return ''
    for line in doc.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ''


def schema_from_callable(func: Callable,
                         descriptions: Optional[Dict[str, str]] = None,
                         required: Optional[List[str]] = None,
                         properties: Optional[Dict[str, dict]] = None) -> dict:
    """从 Python 函数推导 OpenAI 兼容的 parameters schema。

    推导顺序：显式 properties > 类型注解 > 按 string 兜底。
    带默认值的参数不算必填；名称为 event 的参数会被跳过（那是框架注入的，
    不是模型该传的东西）。

    :param descriptions: {参数名: 该参数的一句话说明}，强烈建议提供——
                         模型只能看到这里的话
    """
    descriptions = descriptions or {}
    properties = dict(properties or {})
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        sig = None

    inferred_required: List[str] = []
    if sig is not None:
        for pname, param in sig.parameters.items():
            if pname in ('self', 'cls', 'event'):
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                              inspect.Parameter.VAR_KEYWORD):
                continue
            if pname in properties:
                # 显式声明优先，只补必填判定
                if param.default is inspect.Parameter.empty:
                    inferred_required.append(pname)
                continue
            properties[pname] = {
                'type': _json_type_of(param.annotation),
                'description': descriptions.get(pname, '') or pname,
            }
            if param.default is inspect.Parameter.empty:
                inferred_required.append(pname)

    if required is not None:
        inferred_required = list(required)

    # 必填项必须是 properties 里真实存在的键，否则某些服务端会直接 400
    inferred_required = [k for k in inferred_required if k in properties]

    return {
        'type': 'object',
        'properties': properties,
        'required': inferred_required,
    }


@dataclass
class FunctionTool:
    """一个可被模型调用的函数。

    :param name: 函数名（英文标识，模型用它点名）
    :param description: 给模型看的一句话描述，决定调用率
    :param parameters: JSON Schema 对象（{"type":"object","properties":{...},"required":[...]}）
    :param handler: 执行体，支持普通函数与 async def，参数是模型传来的键值对
    :param source: 来源标记：plugin / builtin / mcp。来源不同，后续治理策略不同
    :param owner: 归属插件名，用于卸载时批量清理
    :param require_perm: 需要的权限节点（如 'sign.admin'），不满足时拒绝执行
    :param enabled: 是否参与本次对话。可在面板/命令里热开关
    :param timeout: 单次执行超时（秒）
    """

    name: str
    description: str
    parameters: dict
    handler: Callable
    source: str = 'plugin'
    owner: str = ''
    require_perm: str = ''
    enabled: bool = True
    timeout: float = 20.0

    def to_openai_tool(self) -> dict:
        """导出 OpenAI 兼容的工具声明（放进请求的 tools 字段）"""
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self.description,
                'parameters': self.parameters or {'type': 'object', 'properties': {}},
            },
        }

    def clone(self, **overrides) -> 'FunctionTool':
        """复制一份并覆盖字段（主要用于标记 source / owner 后再入库）"""
        return replace(self, **overrides)

    def __repr__(self) -> str:
        return f"<FunctionTool {self.name} ({self.source}) {'on' if self.enabled else 'off'}>"


def llm_function(name: str = None, description: str = None,
                 parameters: dict = None, required: List[str] = None,
                 param_descriptions: Dict[str, str] = None,
                 timeout: float = 20.0, require_perm: str = '',
                 enabled: bool = True):
    """把普通函数登记成可被模型调用的函数。

    装饰器只做「登记」——把 FunctionTool 挂到函数的 __llm_tool__ 上，
    真正入库发生在 ToolRegistry.collect(module) 被调用时。
    """
    def deco(func: Callable) -> Callable:
        schema = (dict(parameters) if parameters else
                  schema_from_callable(func, param_descriptions, required))
        tool = FunctionTool(
            name=name or func.__name__,
            description=description or _first_line(func.__doc__),
            parameters=schema,
            handler=func,
            source='plugin',
            owner='',
            require_perm=require_perm,
            enabled=enabled,
            timeout=timeout,
        )
        setattr(func, '__llm_tool__', tool)
        return func
    return deco


class ToolRegistry:
    """工具注册表：登记、启停、导出声明、执行。

    同一个名字重复注册时**后来者覆盖先在者**，但会打日志——热重载插件时这属于
    正常现象，可如果是两个不同插件抢同一个名字，日志会告诉你谁赢了。
    """

    def __init__(self, logger_name: str = 'llm_core'):
        self._tools: Dict[str, FunctionTool] = {}
        self._log = logging.getLogger(f'zcbot.{logger_name}')

    # ---- 登记 ----

    def register(self, tool: FunctionTool) -> None:
        if not tool.name or not callable(tool.handler):
            raise ValueError("工具必须有 name 且 handler 可调用")
        existing = self._tools.get(tool.name)
        if existing is not None and existing.owner not in ('', tool.owner):
            self._log.warning(
                f"工具 [{tool.name}] 被 [{tool.owner or '未知'}] 覆盖"
                f"（原归属 {existing.owner or '未知'}）")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_by_owner(self, owner: str) -> int:
        """按归属插件批量清理（插件卸载时用）"""
        victims = [k for k, v in self._tools.items() if v.owner == owner]
        for k in victims:
            self._tools.pop(k, None)
        return len(victims)

    def collect(self, module) -> List[FunctionTool]:
        """扫描模块里所有带 __llm_tool__ 标记的函数并入库。返回本次入库的工具列表。"""
        got = []
        for _attr in dir(module):
            obj = getattr(module, _attr, None)
            tool = getattr(obj, '__llm_tool__', None)
            if isinstance(tool, FunctionTool):
                tool = tool.clone(owner=tool.owner or getattr(module, '__name__', ''))
                self.register(tool)
                got.append(tool)
        return got

    # ---- 查询 ----

    def get(self, name: str) -> Optional[FunctionTool]:
        return self._tools.get(name)

    def list_tools(self, enabled_only: bool = False) -> List[FunctionTool]:
        items = list(self._tools.values())
        if enabled_only:
            items = [t for t in items if t.enabled]
        return sorted(items, key=lambda t: (t.source != 'builtin', t.name))

    def names(self, enabled_only: bool = True) -> List[str]:
        return [t.name for t in self.list_tools(enabled_only)]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            return False
        tool.enabled = bool(enabled)
        return True

    def set_enabled_all(self, enabled: bool) -> int:
        for t in self._tools.values():
            t.enabled = bool(enabled)
        return len(self._tools)

    def to_openai_tools(self, only: Optional[List[str]] = None) -> List[dict]:
        """导出请求体里的 tools 字段。不传只会导出启用的工具。"""
        pool = [t for t in self._tools.values() if t.enabled]
        if only is not None:
            wanted = set(only)
            pool = [t for t in pool if t.name in wanted]
        return [t.to_openai_tool() for t in sorted(pool, key=lambda x: x.name)]

    # ---- 校验与执行 ----

    @staticmethod
    def validate(tool: FunctionTool, arguments: dict) -> None:
        """按 JSON Schema 的最小子集校验参数。

        只做三件确定会影响正确性的事：缺必填、类型不符、传了未声明的参数。
        复杂约束（枚举、范围、正则）交给书面问题自己去业务代码里判断。
        """
        props = (tool.parameters or {}).get('properties', {}) or {}
        required = (tool.parameters or {}).get('required', []) or []

        missing = [k for k in required if k not in arguments]
        if missing:
            raise ToolCallError(f"缺少必填参数: {', '.join(missing)}")

        unknown = [k for k in arguments if k not in props]
        if unknown:
            raise ToolCallError(
                f"参数 {', '.join(unknown)} 未在 {tool.name} 的参数表中声明；"
                f"可用参数: {', '.join(props) or '（无）'}")

        for key, spec in props.items():
            if key not in arguments:
                continue
            want = spec.get('type')
            if want not in _JSON_TYPES:
                continue
            if not _type_ok(arguments[key], want):
                raise ToolCallError(
                    f"参数 {key} 类型不符：期望 {want}，实际 {type(arguments[key]).__name__}")

    async def aexecute(self, tool: FunctionTool, arguments: dict,
                       event=None, perm_check: Callable = None) -> str:
        """执行一次工具调用，返回要回灌给模型的字符串（None 表示不回灌）。

        :param perm_check: 权限判定回调 check(user_id, node, context, role) -> bool
        """
        try:
            arguments = dict(arguments or {})
            self.validate(tool, arguments)

            if tool.require_perm:
                uid = getattr(event, 'user_id', None) if event is not None else None
                if perm_check is None:
                    raise ToolCallError("当前未配置权限判定，已拒绝本次调用")
                ok = perm_check(uid, tool.require_perm)
                if asyncio.iscoroutine(ok):
                    ok = await ok
                if not ok:
                    self._log.warning(f"工具 [{tool.name}] 被权限节点 {tool.require_perm} 拒绝")
                    raise ToolCallError("你没有执行该操作的权限")

            kwargs = dict(arguments)
            if event is not None and _accepts_event(tool.handler):
                kwargs['event'] = event

            if asyncio.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(tool.handler(**kwargs),
                                                timeout=tool.timeout)
            else:
                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: tool.handler(**kwargs)),
                    timeout=tool.timeout)
            return _stringify(result)
        except asyncio.TimeoutError:
            return f"[工具 {tool.name}] 执行超时（>{tool.timeout}s）"
        except ToolCallError as e:
            # 回灌给模型，让它自己改参数重试
            return f"[工具 {tool.name}] 调用失败：{e}"
        except Exception as e:  # noqa: BLE001 - 工具异常不能炸掉整个会话
            self._log.exception(f"工具 [{tool.name}] 执行异常")
            return f"[工具 {tool.name}] 执行异常：{e}"


def _accepts_event(handler: Callable) -> bool:
    """handler 是否接收 event 参数（显式声明或 **kwargs 都算）"""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return False
    for name, param in sig.parameters.items():
        if name == 'event':
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _type_ok(value: Any, want: str) -> bool:
    if value is None:
        return False
    if want == 'string':
        return isinstance(value, str)
    if want == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if want == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == 'boolean':
        return isinstance(value, bool)
    if want == 'object':
        return isinstance(value, dict)
    if want == 'array':
        return isinstance(value, (list, tuple))
    return True


def _stringify(result: Any) -> Optional[str]:
    """把工具返回值变成回灌给模型的文本。

    None 保持为 None（约定：不进上下文）；其余走 str()，
    字典/列表用 JSON 序列化，避免 Python 的单引号恶心模型。
    """
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list, tuple)):
        import json
        try:
            return json.dumps(result, ensure_ascii=False)
        except TypeError:
            return str(result)
    return str(result)


# 工具 20 秒默认超时：宁可让模型看到「超时」再换个办法，也不要让一次请求挂死会话
if __name__ == '__main__':  # pragma: no cover - 手工自检入口
    def demo_weather(location: str):
        """查询某个城市今天的天气，返回温度和天气状况。"""
        return f"{location}: 26 度，晴"

    print(schema_from_callable(demo_weather, {'location': '城市名，如 北京'}))
