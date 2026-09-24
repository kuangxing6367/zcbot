"""
协议适配器接口 + 服务注册表
框架核心通过此模块定义服务契约，官方/第三方插件实现并注册。

设计目标：framework 核心不认识任何具体协议（OneBot/HTTP/自定义…），
它只面向 ProtocolAdapter 抽象与 ServiceRegistry 编程。
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from framework.hooks import HookPoints

logger = logging.getLogger('zcbot')


class ProtocolAdapter(ABC):
    """协议适配器抽象基类（OneBot/HTTP/自定义协议等）"""

    # 接入端唯一 id（连接页/多接入端场景按此引用；子类可覆写）
    adapter_id: str = ''

    @abstractmethod
    async def handle_event(self, raw_event: dict, bot_name: str) -> Optional[dict]:
        """
        将原始协议事件转换为框架内部事件格式
        :return: 内部事件 dict，或 None 表示丢弃
        """
        ...

    @abstractmethod
    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        """调用协议 API（异步，协议侧必须实现）"""
        ...

    @abstractmethod
    def get_connected_bots(self) -> list:
        """返回已连接的来源/实例列表"""
        ...

    @abstractmethod
    def start(self):
        """启动适配器"""
        ...

    @abstractmethod
    async def stop(self):
        """停止适配器"""
        ...

    # ─────────────────────────────────────────────────────────────
    # 连接自描述（可选）：WebUI /api/connection 据此动态渲染配置表单，
    # 内核不再写死任何具体协议的字段/文案。
    # ─────────────────────────────────────────────────────────────

    def get_connection_info(self) -> Optional[dict]:
        """
        返回本接入端的连接描述；None 表示无可配置端点。

        约定形状：
        {
            "id": "onebot",                 # 唯一 id（缺省用 adapter_id/类名）
            "name": "OneBot 11 反向 WS",     # 显示名
            "config_section": "onebot",      # config.yaml 段名（可编辑配置所在）
            "fields": [                      # 可编辑字段（顺序即表单顺序）
                {"key": "listen_host", "label": "监听地址", "type": "string"},
                {"key": "listen_port", "label": "监听端口", "type": "number"},
                {"key": "access_token", "label": "Access Token", "type": "password"},
            ],
            "restart_keys": ["listen_host", "listen_port"],  # 改动需重启才生效
            "endpoint_hint": "ws://{host}:{port}/ws",        # 可选：接入地址提示
            "guide": "……接入说明……",                         # 可选：多行指引
            "status_extra": {"ws_port": 6830},                # 可选：并入 status
        }
        """
        return None

    def _connection_id(self) -> str:
        info = None
        try:
            info = self.get_connection_info()
        except Exception:
            info = None
        if isinstance(info, dict) and info.get('id'):
            return str(info['id'])
        return self.adapter_id or type(self).__name__

    # ─────────────────────────────────────────────────────────────
    # api_caller 服务统一契约
    # 接入端把自己注册为 services['api_caller'] 后，ctx.api()/aapi() 会调用
    # 下面的 call/acall。基类提供"转发到 call_api"的默认实现，使任何适配器
    # 无需重复样板即可满足契约（同步 call 自动桥接到框架主事件循环）。
    # ─────────────────────────────────────────────────────────────

    async def acall(self, action: str, bot: str = None, **params) -> dict:
        """异步动作调用（默认转发到 call_api）"""
        fw = getattr(self, 'framework', None)
        if fw is not None and getattr(fw, 'hooks', None) is not None:
            try:
                await fw.hooks.trigger_async(
                    HookPoints.ACTION_BEFORE, action, params, bot)
            except Exception as e:
                logger.error(f"action.before 扩展点异常: {e}")
        result = await self.call_api(action, bot, **params)
        if fw is not None and getattr(fw, 'hooks', None) is not None:
            try:
                await fw.hooks.trigger_async(
                    HookPoints.ACTION_AFTER, action, params, bot, result)
            except Exception as e:
                logger.error(f"action.after 扩展点异常: {e}")
        return result

    def call(self, action: str, bot: str = None, **params) -> dict:
        """同步动作调用（供 Web/executor 线程使用，内部桥接到主事件循环）"""
        fw = getattr(self, 'framework', None)
        if fw is not None and getattr(fw, 'hooks', None) is not None:
            try:
                fw.hooks.trigger_sync(HookPoints.ACTION_BEFORE, action, params, bot)
            except Exception as e:
                logger.error(f"action.before 扩展点异常: {e}")
        coro = self.call_api(action, bot, **params)
        loop = getattr(fw, 'loop', None) if fw else None
        if loop is not None and loop.is_running():
            try:
                result = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)
            except Exception as e:
                result = {"status": "failed", "retcode": -3, "msg": str(e)}
        # 没有可复用的运行循环时，在本线程临时运行（极少路径）
        else:
            try:
                result = asyncio.run(coro)
            except Exception as e:
                result = {"status": "failed", "retcode": -3, "msg": str(e)}
        if fw is not None and getattr(fw, 'hooks', None) is not None:
            try:
                fw.hooks.trigger_sync(HookPoints.ACTION_AFTER, action, params, bot, result)
            except Exception as e:
                logger.error(f"action.after 扩展点异常: {e}")
        return result

    # ─────────────────────────────────────────────────────────────
    # 协议中立的"主动发送一条文本"能力
    # 框架自身需要发文本时（如权限不足提示、关键词自动回复）统一走这里，
    # 由具体协议把它翻译成本协议动作；无主动发送能力的接入端（如纯事件
    # 注入端 http_inject）沿用默认实现，返回 unsupported 而不是抛异常。
    # ─────────────────────────────────────────────────────────────

    async def send_text(self, text: str, *, user_id=None, group_id=None,
                        source: str = None) -> dict:
        """
        发送一条纯文本消息（协议中立）。
        :param text: 文本内容
        :param user_id: 私聊目标
        :param group_id: 群目标
        :param source: 来源实例名（多账号场景）
        :return: 协议返回；默认表示当前接入端不支持主动发送
        """
        return {"status": "unsupported", "retcode": -10,
                "msg": "当前接入端不支持主动发送文本"}


class ActionProxy:
    """
    协议中立的动作调用代理（兜底用）。

    当接入端没有注册专用的 API 封装对象（如 services['onebot_api']）时，
    ctx.actions / ctx.onebot / 其它便捷面通过本代理把"任意属性访问"翻译成
    一次 api_caller 动作调用。它本身不含任何协议知识，只是机械转发。
    """

    def __init__(self, caller, default_bot=None):
        object.__setattr__(self, '_caller', caller)
        object.__setattr__(self, '_default_bot', default_bot)

    def _resolve_bot(self, bot):
        return bot or object.__getattribute__(self, '_default_bot')

    def call(self, action: str, bot=None, **params):
        return self._caller.call(action, bot=self._resolve_bot(bot), **params)

    async def acall(self, action: str, bot=None, **params):
        return await self._caller.acall(action, bot=self._resolve_bot(bot), **params)

    def __getattr__(self, action: str):
        # 以"动作名"动态生成同步调用方法
        def _method(bot=None, **params):
            return self._caller.call(action, bot=self._resolve_bot(bot), **params)
        _method.__name__ = action
        return _method


class ServiceRegistry:
    """
    服务注册表：官方插件注册自身为核心能力
    核心框架通过 get() 获取服务，不直接 import 官方插件代码
    """

    def __init__(self):
        self._services: Dict[str, Any] = {}
        self._adapters: Dict[str, ProtocolAdapter] = {}

    def register(self, name: str, service: Any):
        """注册服务（官方插件调用）"""
        if name in self._services:
            logger.warning(f"服务 [{name}] 已注册，将被覆盖")
        self._services[name] = service
        # 协议适配器按 adapter_id 汇总（多接入端可并存；protocol_adapter 键仍取最后注册者）
        if isinstance(service, ProtocolAdapter):
            aid = service._connection_id()
            self._adapters[aid] = service
        logger.debug(f"服务已注册: [{name}]")

    def get(self, name: str, default=None):
        """获取服务（核心框架/插件调用）"""
        return self._services.get(name, default)

    def has(self, name: str) -> bool:
        """检查服务是否已注册"""
        return name in self._services

    def remove(self, name: str):
        """移除服务"""
        svc = self._services.pop(name, None)
        if isinstance(svc, ProtocolAdapter):
            aid = svc._connection_id()
            if self._adapters.get(aid) is svc:
                self._adapters.pop(aid, None)

    def all(self) -> dict:
        """返回所有已注册服务"""
        return dict(self._services)

    def protocol_adapters(self) -> Dict[str, ProtocolAdapter]:
        """全部已注册协议适配器 {adapter_id: adapter}（连接页/状态聚合用）"""
        out = dict(self._adapters)
        # protocol_adapter 键上的当前主适配器兜底（未走过 register 的场景）
        primary = self._services.get('protocol_adapter')
        if isinstance(primary, ProtocolAdapter):
            out.setdefault(primary._connection_id(), primary)
        return out

    def primary_adapter(self) -> Optional[ProtocolAdapter]:
        """当前主接入端（services['protocol_adapter']）"""
        primary = self._services.get('protocol_adapter')
        return primary if isinstance(primary, ProtocolAdapter) else None
