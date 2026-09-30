# -*- coding: utf-8 -*-
"""
模型提供商（provider）总线

一个 provider = 「把一段对话历史换成模型回复」的一段代码。它只认识三件事：

    messages    OpenAI 格式的消息数组
    tools       本次可用的工具声明
    回复         文本，或一串工具调用指令

它**不认识**会话、不知道 llm_core 的存在、不关心消息从哪个群里来。这种刻意的
无知换来的是可插拔：DeepSeek、智谱、Moonshot、各种自建网关只要说的是
``POST /chat/completions`` 这一种方言，就都是同一个 OpenAICompatProvider，
换个 base_url 就能跑；要说别的方言（比如原生 Anthropic messages），写一个插件
实现 Provider 接口注册进来即可，不需要动 llm_core 一行代码。

注册一个自己的 provider（llm_core 释放后作为用户插件加载，模块名是
``plugin_llm_core``，不是 ``plugins.llm_core``）：

    from plugin_llm_core.providers import Provider, ProviderReply

    class MyProvider(Provider):
        id = "mine"
        supports_tools = False
        model = "my-model"

        def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
            ...
            return ProviderReply(content="你好")

    svc = fw.services.get('llm_core')
    svc.register_provider(MyProvider())

内置实现见 OpenAICompatProvider。
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger('zcbot.llm_core')

__all__ = [
    'Provider', 'ProviderReply', 'ToolCall', 'OpenAICompatProvider',
    'ProviderRegistry', 'ProviderError',
]

_DEFAULT_TIMEOUT = 120


class ProviderError(Exception):
    """提供商调用失败（网络、鉴权、限流、协议不符等）。

    抛给上层由 agent 转成一句人话回给用户，绝不把堆栈甩到群里。
    """


@dataclass
class ToolCall:
    """模型发出的一次函数调用指令"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ''

    def __repr__(self) -> str:
        return f"<ToolCall {self.name}({self.arguments})>"


@dataclass
class ProviderReply:
    """模型的一轮回复。文本和工具调用二选一，也可以都没有（模型在思考）。"""

    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    model: str = ''

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class Provider:
    """提供商基类。子类至少要实现 chat()。"""

    id: str = 'abstract'
    label: str = ''
    model: str = ''
    models: List[str] = []

    supports_tools: bool = True
    supports_vision: bool = False

    def __init__(self, **config):
        self.config = dict(config or {})
        # id 必须能从配置来：否则所有同类型实例都叫同一个名字，注册表只会剩最后一个
        self.id = self.config.get('id') or self.id
        self.model = self.config.get('model') or self.model
        models = self.config.get('models')
        self.models = list(models) if models else list(self.models)
        for key in ('supports_tools', 'supports_vision'):
            if key in self.config and self.config[key] is not None:
                setattr(self, key, bool(self.config[key]))
        self.label = self.config.get('label') or self.label or self.id

    # ---- 子类实现 ----

    def chat(self, messages: List[dict], tools: Optional[List[dict]] = None,
             temperature: float = 0.7, max_tokens: int = 1024) -> ProviderReply:
        raise NotImplementedError

    def list_models(self) -> List[str]:
        """本机可用的模型名；远端拉不到时返回配置里写死的那几个"""
        return list(self.models or ([self.model] if self.model else []))

    def serves(self, model: str) -> bool:
        return bool(model) and (model == self.model or model in self.models)

    def describe(self) -> dict:
        return {
            'id': self.id,
            'label': self.label,
            'model': self.model,
            'models': self.list_models(),
            'supports_tools': self.supports_tools,
            'supports_vision': self.supports_vision,
            'kind': type(self).__name__,
        }


class OpenAICompatProvider(Provider):
    """OpenAI 兼容的 /chat/completions 提供商。

    覆盖绝大多数情况：OpenAI 官方、DeepSeek、Moonshot、智谱部分版本、vLLM、
    Ollama、One-API / New-API 这类聚合网关。判断标准只有一条——能用同一套
    ``Authorization: Bearer`` + ``{"messages": [...]}`` 请求体说话。

    配置字段：

        base_url   必填，形如 https://api.deepseek.com/v1（结尾不带斜杠）
        api_key    必填（本地无鉴权的服务随便填个非空值即可）
        model      默认模型名
        timeout    单次请求超时（秒）
        proxy      可选，形如 http://127.0.0.1:7892
    """

    id = 'openai'

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024) -> ProviderReply:
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise ProviderError("缺少 requests 依赖，无法调用远端模型") from e

        base_url = (self.config.get('base_url') or '').rstrip('/')
        api_key = self.config.get('api_key') or ''
        if not base_url:
            raise ProviderError("未配置 base_url")
        if not api_key:
            raise ProviderError("未配置 api_key")

        payload: Dict[str, Any] = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
        }
        # 工具：只在提供商声明支持、且本次确实有工具时才下发。
        # 老模型（如部分 R1）看到 tools 字段会直接报不支持，白搭一轮请求。
        if tools and self.supports_tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        proxies = None
        proxy = self.config.get('proxy')
        if proxy:
            proxies = {'http': proxy, 'https': proxy}

        timeout = float(self.config.get('timeout') or _DEFAULT_TIMEOUT)
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=headers, json=payload, timeout=timeout, proxies=proxies)
        except Exception as e:
            raise ProviderError(f"请求模型服务失败: {e}") from e

        if resp.status_code != 200:
            raise ProviderError(_http_error_hint(resp.status_code, resp.text))

        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("模型服务返回的不是合法 JSON") from e

        choice = (body.get('choices') or [{}])[0]
        message = choice.get('message') or {}
        reply = ProviderReply(
            content=message.get('content'),
            usage=body.get('usage') or {},
            model=body.get('model') or self.model,
        )
        for raw in message.get('tool_calls') or []:
            fn = raw.get('function') or {}
            raw_args = fn.get('arguments') or '{}'
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                # 模型吐了不合法的 JSON：保留原文回灌，让它自己重写
                logger.warning(f"[{self.label}] 工具参数不是合法 JSON: {raw_args[:200]}")
                args = {}
            reply.tool_calls.append(ToolCall(
                id=raw.get('id') or f"call_{len(reply.tool_calls)}",
                name=fn.get('name') or '',
                arguments=args if isinstance(args, dict) else {},
                raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(args),
            ))
        return reply


def _http_error_hint(status: int, text: str) -> str:
    """把常见 HTTP 状态码翻译成人能看懂的排查方向"""
    brief = (text or '')[:180].replace('\n', ' ')
    common = {
        401: "API Key 无效（检查密钥是否填错或已过期）",
        403: "没有访问该模型的权限（检查账户余额 / 模型白名单）",
        404: "接口地址不对（base_url 通常需要以 /v1 结尾）",
        429: "触发限流，稍后重试",
    }
    hint = common.get(status, "服务端返回错误")
    return f"模型服务返回 {status}：{hint}。原始响应：{brief}"


class ProviderRegistry:
    """provider 注册表：按 id 管理，按模型名解析。"""

    _BUILTIN = {'openai': OpenAICompatProvider, 'openai_compat': OpenAICompatProvider}

    def __init__(self):
        self._providers: Dict[str, Provider] = {}
        self._default_id: Optional[str] = None

    @classmethod
    def build(cls, configs: Sequence[dict]) -> 'ProviderRegistry':
        """从配置列表构造注册表。

        configs 形如::

            [{"id": "deepseek", "type": "openai", "base_url": "...",
              "api_key": "...", "model": "deepseek-chat", "default": true}]
        """
        reg = cls()
        for cfg in configs or []:
            if not isinstance(cfg, dict):
                continue
            try:
                reg.add_from_config(cfg)
            except Exception as e:
                logger.error(f"构造模型提供商 [{cfg.get('id')}] 失败: {e}")
        return reg

    def add_from_config(self, cfg: dict) -> Optional[Provider]:
        kind = (cfg.get('type') or 'openai').lower()
        cls = self._BUILTIN.get(kind)
        if cls is None:
            logger.error(f"未知的提供商类型: {kind}（可用: {', '.join(sorted(self._BUILTIN))}）")
            return None
        # type 属于构造参数，不进 provider.config 避免被当成业务字段
        cfg = {k: v for k, v in cfg.items() if k != 'type'}
        provider = cls(**cfg)
        self.register(provider, default=bool(cfg.get('default')))
        return provider

    def register(self, provider: Provider, default: bool = False) -> None:
        if not provider.id:
            raise ValueError("provider 必须有 id")
        self._providers[provider.id] = provider
        if default or self._default_id is None:
            self._default_id = provider.id
        logger.info(f"已注册模型提供商 [{provider.id}] {provider.label} / {provider.model}")

    def unregister(self, provider_id: str) -> bool:
        existed = self._providers.pop(provider_id, None) is not None
        if existed and self._default_id == provider_id:
            self._default_id = next(iter(self._providers), None)
        return existed

    def get(self, provider_id: str = None) -> Optional[Provider]:
        pid = provider_id or self._default_id
        return self._providers.get(pid) if pid else None

    def require(self, provider_id: str = None) -> Provider:
        provider = self.get(provider_id)
        if provider is None:
            raise ProviderError(
                "没有可用的模型提供商，请先在配置里填好 base_url / api_key / model")
        return provider

    def resolve(self, model: str) -> Optional[Provider]:
        """按模型名找到能 serve 它的 provider"""
        for provider in self._providers.values():
            if provider.serves(model):
                return provider
        return None

    def list_providers(self) -> List[Provider]:
        return list(self._providers.values())

    @property
    def default_id(self) -> Optional[str]:
        return self._default_id


# 供「只想要一个默认实例」的场景使用（插件进程内通常靠 Service 持有）
_global_registry = ProviderRegistry()


def registry_of() -> ProviderRegistry:
    return _global_registry
