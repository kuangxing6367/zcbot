"""
插件上下文对象 (ctx)
向插件暴露：命令注册、API调用、数据库、日志、配置读取、事件、定时任务

异步模型：
- async handler 中优先使用 aapi() / asend_msg() 等 async 方法，不阻塞事件循环
- 旧插件继续使用 api() / send_msg() 等同步方法（内部桥接到主事件循环，自动兼容）

能力按域拆分至 mixin：
- ctx_messaging  协议中立快捷动作 / 身份判断 / 群级插件开关
- ctx_events     命令 / 定时任务 / 事件订阅 / 扩展点 / 协议 API
- ctx_webui      仪表盘卡片 / WebUI 内嵌 / 管理页扩展
- ctx_db         同步/异步数据库操作
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger('zcbot')

# 全局线程池，用于异步执行耗时操作（如图片渲染），不阻塞主消息处理流程
_async_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix='async_plugin'
)

from framework.ctx.messaging import PluginMessagingMixin
from framework.ctx.events import PluginEventsMixin
from framework.ctx.webui import PluginWebuiMixin
from framework.ctx.db import PluginDatabaseMixin


class PluginContext(PluginMessagingMixin, PluginEventsMixin, PluginWebuiMixin, PluginDatabaseMixin):
    """插件上下文，传递给 register(ctx) 函数"""

    class _PluginLogger:
        """插件日志适配器：提供 .info/.warning/.error/.debug 接口，自动加插件名前缀"""
        def __init__(self, plugin_name: str):
            self._name = plugin_name

        def info(self, msg, *args, **kwargs):
            logger.info(f"[{self._name}] {msg}", *args, **kwargs)

        def warning(self, msg, *args, **kwargs):
            logger.warning(f"[{self._name}] {msg}", *args, **kwargs)

        def error(self, msg, *args, **kwargs):
            logger.error(f"[{self._name}] {msg}", *args, **kwargs)

        def debug(self, msg, *args, **kwargs):
            logger.debug(f"[{self._name}] {msg}", *args, **kwargs)

        def warn(self, msg, *args, **kwargs):
            logger.warning(f"[{self._name}] {msg}", *args, **kwargs)

        def exception(self, msg, *args, **kwargs):
            logger.exception(f"[{self._name}] {msg}", *args, **kwargs)

    def __init__(self, plugin_name: str, framework):
        self._plugin_name = plugin_name
        self._framework = framework  # 框架引擎引用
        self._db = framework.db
        self._commands = []  # 本次注册周期收集的命令
        self._tasks = []     # 本次注册周期收集的任务
        self._dashboard_cards = []  # 仪表盘卡片
        self._raw_message_handlers = []  # 原始消息处理器（收到原始消息事件，可选择性接管）
        self._group_extensions = []  # WebUI 群组管理页插件扩展
        self._user_extensions = []   # WebUI 用户管理页插件扩展
        self._logger = self._PluginLogger(plugin_name)
        self._config_cache = {}      # key -> (value, timestamp) 插件配置 TTL 缓存
        self._config_cache_ttl = 30  # 缓存有效期（秒），避免 async handler 同步查库阻塞事件循环
    @property
    def actions(self):
        """
        协议中立的动作调用面（推荐新代码使用）。
        优先返回当前接入端注册的专用动作封装 services['onebot_api']；
        若接入端只注册了通用 api_caller，则用协议无关的 ActionProxy 兜底，
        框架核心本身不包含任何具体协议实现。
        """
        api = self._framework.services.get('onebot_api')
        if api is not None:
            return api
        caller = self._framework.services.get('api_caller')
        if caller is not None:
            from framework.messaging.protocol import ActionProxy
            return ActionProxy(caller)
        raise RuntimeError("无可用协议适配器（请启用一个接入端，如 core_plugins.onebot_adapter 或 core_plugins.http_inject）")

    @property
    def onebot(self):
        """
        兼容别名：等价于 ctx.actions（历史插件便捷面）。
        OneBot 11 API 封装优先返回 services['onebot_api']；
        否则回落到协议无关的 ActionProxy。
        """
        return self.actions
    @property
    def logger(self):
        """获取插件日志记录器（标准 logger 接口）"""
        return self._logger
    @property
    def plugin_name(self) -> str:
        """获取当前插件名"""
        return self._plugin_name
    @property
    def _current_bot(self):
        """
        当前消息来源的 OneBot 实例名（由框架在 handler 执行期间通过 contextvars 注入）
        多 bot 并发场景下每个事件独立，不再使用插件级共享变量（原 router 直接写
        module.ctx._current_bot 会在并发消息交错时发错 bot）
        """
        try:
            from framework.runtime import current_source_var
            return current_source_var.get()
        except Exception:
            return None

    def get_data_dir(self) -> str:
        """
        获取当前插件的数据/配置目录绝对路径（plugins_dat/<plugin_name>/）
        插件应在此目录下读写自己的配置文件、缓存数据等，而非 plugins/ 代码目录
        """
        import os
        dat_dir = os.path.join(
            self._framework.plugin_loader.plugins_dat_dir,
            self._plugin_name
        )
        if not os.path.isdir(dat_dir):
            os.makedirs(dat_dir, exist_ok=True)
        return dat_dir

    # ---- 权限组（LuckPerms 风格）----

    def has_perm(self, user_id: int, node: str, context: dict = None,
                 role: str = None) -> bool:
        """
        判断某用户是否拥有指定权限节点（未定义按拒绝处理）

        :param user_id: 用户 ID
        :param node: 权限节点，如 'myplugin.ban'
        :param context: 上下文 {'group': '123456', 'bot': 'main', 'msgtype': 'group'}
        :param role: 框架身份（super/owner/admin/member），用于注入内置角色组
        """
        from framework import perm
        return perm.has_perm(self._db, user_id, node, context, role)

    def check_perm(self, user_id: int, node: str, context: dict = None,
                   role: str = None):
        """三态权限查询：True=授予 / False=显式否决 / None=未定义"""
        from framework import perm
        return perm.check_perm(self._db, user_id, node, context, role)

    def user_groups(self, user_id: int, context: dict = None, role: str = None) -> list:
        """用户的生效权限组（含继承展开，按 weight 降序）"""
        from framework import perm
        return perm.user_groups(self._db, user_id, context, role)

    # ---- 插件配置读取 ----

    def get_config(self, key: str, default=None):
        """
        读取插件配置项（带 TTL 缓存，避免 async handler 同步查库阻塞事件循环）
        配置值由 Web UI 通过 _conf_schema.json 定义并存储在 plugin_configs 表中
        :param key: 配置键名
        :param default: 默认值（配置不存在时返回）
        :return: 配置值
        """
        cache_key = (self._plugin_name, key)
        now = time.time()
        # 命中缓存直接返回
        cached = self._config_cache.get(cache_key)
        if cached is not None:
            value, ts = cached
            if now - ts < self._config_cache_ttl:
                return value
            self._config_cache.pop(cache_key, None)
        # 缓存未命中，查库
        try:
            row = self._db.query_one(
                "SELECT config_value FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                (self._plugin_name, key)
            )
            if row:
                value = row['config_value']
                # 数据库值为 NULL 时返回 default 并缓存 None 标记
                if value is None:
                    self._config_cache[cache_key] = (default, now)
                    return default
                # 尝试 JSON 解码（非字符串类型）
                try:
                    decoded = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    decoded = value
                self._config_cache[cache_key] = (decoded, now)
                return decoded
            self._config_cache[cache_key] = (default, now)
            return default
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 读取配置 {key} 失败: {e}")
            return default

    def get_all_config(self) -> dict:
        """读取插件所有配置项，返回 {key: value} 字典"""
        try:
            rows = self._db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s",
                (self._plugin_name,)
            )
            result = {}
            for r in rows:
                try:
                    result[r['config_key']] = json.loads(r['config_value'])
                except (json.JSONDecodeError, TypeError):
                    result[r['config_key']] = r['config_value']
            return result
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 读取全部配置失败: {e}")
            return {}

    def get_text(self, message_or_event) -> str:
        """
        从消息中提取纯文本。入参可以是：
          - OneBot 消息（str，或富媒体段列表 [{'type':'text','data':{'text':'...'}}, ...]）
          - 事件对象 / dict（自动取 message / raw_message / text 字段）
        富媒体（图片等）被剥离，只保留文本内容。
        """
        from framework.messaging.event import _extract_text
        if isinstance(message_or_event, dict):
            text = (message_or_event.get('message')
                    or message_or_event.get('raw_message')
                    or message_or_event.get('text'))
        elif hasattr(message_or_event, 'message'):
            text = getattr(message_or_event, 'message', None) \
                or getattr(message_or_event, 'raw_message', None)
        else:
            text = message_or_event
        return _extract_text(text or '')

    # ---- 日志 ----

    def log(self, msg: str, level: str = 'info'):
        """输出日志"""
        level = level.upper()
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(f"[{self._plugin_name}] {msg}")

    # ---- 异步执行 ----

    def run_async(self, func: Callable, *args, **kwargs):
        """
        异步执行耗时操作（如图片渲染、网络请求），不阻塞主消息处理流程。

        提交的任务在线程池中执行，返回 concurrent.futures.Future 对象。
        适合用于图片渲染、文件处理等不需要立即返回的耗时操作。

        示例：
            def render_and_send():
                path = renderer.render_card(...)
                ctx.send_msg(..., message=f"[CQ:image,file=file:///{path}]")

            ctx.run_async(render_and_send)
        """
        return _async_executor.submit(func, *args, **kwargs)

    def audit_log(self, action: str, target_type: str = None,
                  target_name: str = None, detail: dict = None,
                  result: str = 'success', error_message: str = None):
        """
        记录插件操作审计日志
        无需管理员上下文，插件可以记录自己的操作（如数据修改、配置变更等）
        :param action: 操作名，如 'sign_in', 'update_data', 'create_record'
        :param target_type: 操作对象类型，如 'user', 'data', 'record'
        :param target_name: 操作对象名称
        :param detail: 详情字典（将被 JSON 序列化）
        :param result: 结果 'success' 或 'failure'
        :param error_message: 错误信息（result='failure' 时填写）
        """
        try:
            self._db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, target_name, "
                "detail, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (0, f"plugin:{self._plugin_name}", f"plugin.{action}",
                 target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 result, error_message)
            )
        except Exception as e:
            logger.warning(f"[{self._plugin_name}] 审计日志写入失败: {e}")

    # ---- 多轮会话（官方插件 session 提供）----

    async def wait_for(self, event, prompt=None, timeout=60, handler=None):
        """
        等待用户下一条消息（多轮会话）
        :param event: 当前事件对象
        :param prompt: 可选提示消息（自动发送）
        :param timeout: 超时秒数
        :param handler: 可选过滤 handler(raw_event) -> bool
        :return: 消息 dict 或 None（超时）
        """
        mgr = self._framework.services.get('session_manager')
        if mgr is None:
            raise RuntimeError("会话管理器未加载（请启用 core_plugins.session）")
        return await mgr.wait_for(self, event, prompt, timeout, handler)

    def create_session(self, event, timeout=60):
        """
        创建会话对象（支持 async with）
        用法：
            async with ctx.create_session(event, timeout=120) as sess:
                name = await sess.ask("你叫什么名字？")
                age = await sess.ask("年龄？")
                sess.data['name'] = name
        """
        mgr = self._framework.services.get('session_manager')
        if mgr is None:
            raise RuntimeError("会话管理器未加载（请启用 core_plugins.session）")
        return mgr.session_context(self, event, timeout)
