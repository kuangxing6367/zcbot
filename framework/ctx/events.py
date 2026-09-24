# -*- coding: utf-8 -*-
"""PluginContext 命令 / 定时任务 / 事件订阅 / 扩展点 / 协议 API（自 ctx.py 剥离的 mixin）"""
import asyncio
from typing import Callable

class PluginEventsMixin:
    """命令 / 定时任务 / 事件订阅 / 扩展点 / 协议 API"""

    # 以下方法依赖 PluginContext.__init__ 建立的实例字段

    # ---- 命令注册 ----

    def command(self, pattern: str, handler: Callable, priority: int = 50,
                dynamic: bool = False, alias: str = None, description: str = None,
                require_admin: bool = False, require_superuser: bool = False,
                require_perm: str = None):
        """
        注册一个命令
        :param pattern: 正则表达式或命令名（主匹配模式）
        :param handler: 处理函数 (event, match) -> None
        :param priority: 优先级，越小越优先
        :param dynamic: 是否为动态命令（dynamic=True 表示该命令在动态命令 tab 展示，仅标记用）
        :param alias: 命令别名，逗号分隔的字符串或列表（如 "/help,/h" 或 ["/help", "/h"]）
        :param description: 命令描述文本
        :param require_admin: 需要管理员/群主/超管权限（旧的身份轴判定）
        :param require_superuser: 需要超级管理员权限（高于 require_admin）
        :param require_perm: 需要的权限节点（新的权限组判定），如 'myplugin.ban'
            与 require_admin/require_superuser 可并存：两者都填时要求同时满足。
            需要「A 或 B」这类组合条件时，请在 handler 内自行调用 ev.has_perm() 判断。
        """
        if require_superuser:
            require_admin = False  # super 优先级更高

        if not callable(handler):
            raise TypeError(f"handler '{handler.__name__ if hasattr(handler, '__name__') else handler}' 不可调用")

        # 规范化 alias 为字符串
        if alias is not None:
            if isinstance(alias, (list, tuple)):
                alias = ','.join(str(a).strip() for a in alias)
            else:
                alias = str(alias).strip()

        # 从 handler 的 docstring 自动提取描述（如果未显式传入）
        if description is None and handler.__doc__:
            description = handler.__doc__.strip().split('\n')[0].strip()

        # 所有命令统一收集到列表中，由框架批量写入 commands 表
        self._commands.append({
            'plugin_name': self._plugin_name,
            'pattern': pattern,
            'alias': alias,
            'description': description,
            'priority': priority,
            'handler': handler,
            'handler_name': handler.__name__,
            'is_dynamic': 1 if dynamic else 0,
            'require_level': 'super' if require_superuser else ('admin' if require_admin else ''),
            'require_perm': (require_perm or '').strip().lower(),
        })

    # ---- 插件自定义 API 路由（Web 后台 / REST，复用框架鉴权）----

    def register_api(self, path: str, handler: Callable, methods=None, auth: bool = True,
                     description: str = None):
        """
        在框架 Web 服务器上注册一条自定义 REST 路由，自动复用框架登录/API Key 鉴权。

        这是把 ZCBOT 当IM 平台的关键接入点：外部系统/页面可通过 HTTP 与插件交互，
        而不必自己开 HTTP 服务、自己写鉴权。

        :param path: 路由路径，如 '/api/my/stats' 或 '/my/stats'
        :param handler: Flask 视图函数（同 Flask，可返回 jsonify/Response/(resp, status)）
        :param methods: 允许的 HTTP 方法，默认 ['GET']
        :param auth: 是否复用框架鉴权（默认 True：需登录或有效 API Key）
        :param description: 可选说明（当前仅日志记录）
        :return: True 表示已挂载；False 表示 Web 未启用（路由已登记，启用后自动挂载）
        """
        from framework.api import registry as _api_registry
        if methods is None:
            methods = ['GET']
        methods = [m.upper() for m in methods]

        # 双进程宿主模式：Web 在核心进程，走远程路由（handler 契约 fn(params)->dict/(status,dict)）
        rr = getattr(self._framework, '_remote_routes', None)
        if rr is not None:
            try:
                rr.register_route(path, methods, handler, auth)
            except Exception as e:
                self.log(f"远程 API 路由注册失败 {path} {methods}: {e}")
                return False
            if description:
                self.log(f"已注册远程 API 路由 {path} {methods}" + (f" ({description})" if description else ""))
            else:
                self.log(f"已注册远程 API 路由 {path} {methods}")
            return True

        route = _api_registry.register_route(path, methods, handler, auth)
        ok = route is not None
        if ok and description:
            self.log(f"已注册 API 路由 {path} {methods}" + (f" ({description})" if description else ""))
        elif not ok:
            self.log(f"已登记 API 路由 {path}（等待 Web 启用后挂载）")
        return ok

    # ---- 异步调度 / 文本提取等便捷能力 ----

    def call_async(self, coro):
        """
        把协程安全地调度到框架主事件循环执行（可从任意线程调用）。

        常见用途：在同步 handler / executor 线程 / Web 线程里触发一个异步动作，
        不必自己写 asyncio.run_coroutine_threadsafe 样板。
        :param coro: 待执行的协程对象
        :return: concurrent.futures.Future（可在原线程阻塞 .result()，或忽略让其后台运行）
        """
        import asyncio
        loop = getattr(self._framework, 'loop', None)
        if loop is None or not loop.is_running():
            raise RuntimeError("框架主事件循环未运行")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # ---- 定时任务 ----

    def task(self, cron_expr: str, executor: Callable, description: str = None):
        """注册定时任务"""
        if not callable(executor):
            raise TypeError(f"executor '{executor.__name__ if hasattr(executor, '__name__') else executor}' 不可调用")

        self._tasks.append({
            'plugin_name': self._plugin_name,
            'cron_expression': cron_expr,
            'handler': executor,
            'handler_name': executor.__name__,
            'description': description or f"{self._plugin_name} 定时任务",
        })

    # ---- 事件订阅/发布 ----

    def on(self, event_name: str, handler: Callable):
        """订阅系统事件（handler 支持 async def 和普通 def）"""
        self._framework.event_bus.subscribe(event_name, self._plugin_name, handler)

    def on_raw_message(self, handler: Callable):
        """
        注册原始消息处理器（原始消息注入点）

        该处理器收到的是**原始消息事件**（完整 dict，含全部消息段，未提取纯文本），
        在框架命令匹配/关键词回复之前触发，供选择性使用：
        - handler 返回 True        → 消息被接管，框架跳过对该消息的后续全部处理
        - handler 返回 None/False  → 消息继续走正常流程（命令匹配/关键词兜底等）

        签名：handler(raw_event: dict, bot_name: str) -> bool | None
        - raw_event: OneBot 11 原始事件（message 可能是消息段数组，含图片/at/回复等）
        - bot_name:  消息来源的 OneBot 实例名（多账号场景）

        支持 async def（直接 await）和普通 def（转线程执行，不阻塞事件循环）；
        多个插件按插件 priority 升序依次触发，单个处理器异常不影响其他处理器。
        """
        if not callable(handler):
            raise TypeError(f"handler '{getattr(handler, '__name__', handler)}' 不可调用")
        self._raw_message_handlers.append(handler)

    def emit(self, event_name: str, payload: dict = None):
        """发布事件（同步桥接，供旧插件使用）"""
        self._framework.event_bus.emit(event_name, payload)

    # ---- 扩展点（内核契约：把行为挂到内核的任意运行环节）----

    def hook(self, point: str, handler: Callable, priority: int = 50):
        """
        在内核的**扩展点**注册一个处理器——这是"往框架里插入自己的逻辑"的统一入口，
        可在几乎任意运行环节挂接行为：

          - 'lifecycle.startup' / 'lifecycle.shutdown'   进程启动 / 关闭
          - 'http.before_request' / 'http.after_request' Web 请求前后（before 可返回 Response 短路）
          - 'event.before_dispatch' / 'event.after_dispatch'  事件进入内核前后（before 返回 False 丢弃）
          - 'command.before' / 'command.after'          命令执行前后（before 返回 False 跳过）
          - 'message.before_send' / 'message.after_send' 框架主动发文本前后
          - 'action.before' / 'action.after'            任意协议动作调用前后（通知，不短路）

        handler 可以是普通函数或 `async def`；同名（本插件内）重复注册自动去重。
        也可注册自定义扩展点（任意字符串点位），由你自己的代码触发。

        :param point: 扩展点名称（建议使用上面的标准常量）
        :param handler: 处理函数；签名随扩展点而异，详见文档「扩展点」一节
        :param priority: 优先级，越小越先执行（默认 50）
        :return: True 表示已注册
        """
        if not callable(handler):
            raise TypeError(f"hook 的 handler 必须可调用: {getattr(handler, '__name__', handler)}")
        name = f"{self._plugin_name}:{point}"
        self._framework.hooks.register(point, name, handler, priority)
        return True

    def unhook(self, point: str):
        """
        注销本插件在该扩展点的全部处理器（插件卸载/重载前清理用）。
        :param point: 扩展点名称
        :return: True 表示已注销
        """
        name = f"{self._plugin_name}:{point}"
        self._framework.hooks.unregister(point, name)
        return True

    async def aemit(self, event_name: str, payload: dict = None):
        """异步发布事件（推荐 async handler 使用，不阻塞事件循环）"""
        await self._framework.event_bus.aemit(event_name, payload)

    # ---- 统一 API 入口 ----

    def api(self, action: str, bot: str = None, **params):
        """
        调用协议 API（同步桥接）
        推荐 async handler 使用 aapi()，避免阻塞事件循环
        """
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        caller = self._framework.services.get('api_caller')
        if caller is None:
            raise RuntimeError("无可用协议适配器")
        return caller.call(action, bot=bot, **params)

    async def aapi(self, action: str, bot: str = None, **params):
        """异步调用协议 API"""
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        caller = self._framework.services.get('api_caller')
        if caller is None:
            raise RuntimeError("无可用协议适配器")
        return await caller.acall(action, bot=bot, **params)
