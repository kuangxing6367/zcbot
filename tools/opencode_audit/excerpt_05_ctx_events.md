# 第 5 轮侦察材料：framework/ctx/events.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """PluginContext 命令 / 定时任务 / 事件订阅 / 扩展点 / 协议 API（自 ctx.py 剥离的 mixin）"""
   2 import asyncio
   3 import logging
   4 from typing import Callable
   5 
   6 from framework.hooks import HookPriority
   7 
   8 logger = logging.getLogger('zcbot')
   9 
  10 
  11 class PluginEventsMixin:
  12     """命令 / 定时任务 / 事件订阅 / 扩展点 / 协议 API"""
  13 
  14     def _journal(self, kind: str, detail: str):
  15         """记录一次 ctx 注册动作（加载期流水，供黑匣子 plugin_loaded 摘要）。"""
  16         try:
  17             self._register_journal.append({'kind': kind, 'detail': detail})
  18         except Exception as _exc:
  19             logger.debug('[_journal] 捕获异常后按降级路径继续: %s', _exc)
  20 
  21     # 以下方法依赖 PluginContext.__init__ 建立的实例字段
  22 
  23     # ---- 命令注册 ----
  24 
  25     def command(self, pattern: str, handler: Callable, priority: int = 50,
  26                 dynamic: bool = False, alias: str = None, description: str = None,
  27                 require_admin: bool = False, require_superuser: bool = False,
  28                 require_perm: str = None):
  29         """
  30         注册一个命令
  31         :param pattern: 正则表达式或命令名（主匹配模式）
  32         :param handler: 处理函数 (event, match) -> None
  33         :param priority: 优先级，越小越优先
  34         :param dynamic: 是否为动态命令（dynamic=True 表示该命令在动态命令 tab 展示，仅标记用）
  35         :param alias: 命令别名，逗号分隔的字符串或列表（如 "/help,/h" 或 ["/help", "/h"]）
  36         :param description: 命令描述文本
  37         :param require_admin: 需要管理员/群主/超管权限（旧的身份轴判定）
  38         :param require_superuser: 需要超级管理员权限（高于 require_admin）
  39         :param require_perm: 需要的权限节点（新的权限组判定），如 'myplugin.ban'
  40             与 require_admin/require_superuser 可并存：两者都填时要求同时满足。
  41             需要「A 或 B」这类组合条件时，请在 handler 内自行调用 ev.has_perm() 判断。
  42         """
  43         if require_superuser:
  44             require_admin = False  # super 优先级更高
  45 
  46         if not callable(handler):
  47             raise TypeError(f"handler '{handler.__name__ if hasattr(handler, '__name__') else handler}' 不可调用")
  48 
  49         # 规范化 alias 为字符串
  50         if alias is not None:
  51             if isinstance(alias, (list, tuple)):
  52                 alias = ','.join(str(a).strip() for a in alias)
  53             else:
  54                 alias = str(alias).strip()
  55 
  56         # 从 handler 的 docstring 自动提取描述（如果未显式传入）
  57         if description is None and handler.__doc__:
  58             description = handler.__doc__.strip().split('\n')[0].strip()
  59 
  60         # 所有命令统一收集到列表中，由框架批量写入 commands 表
  61         self._commands.append({
  62             'plugin_name': self._plugin_name,
  63             'pattern': pattern,
  64             'alias': alias,
  65             'description': description,
  66             'priority': priority,
  67             'handler': handler,
  68             'handler_name': handler.__name__,
  69             'is_dynamic': 1 if dynamic else 0,
  70             'require_level': 'super' if require_superuser else ('admin' if require_admin else ''),
  71             'require_perm': (require_perm or '').strip().lower(),
  72         })
  73         self._journal('command', f"{pattern} -> {handler.__name__}")
  74 
  75     # ---- 插件自定义 API 路由（Web 后台 / REST，复用框架鉴权）----
  76 
  77     def register_api(self, path: str, handler: Callable, methods=None, auth: bool = True,
  78                      description: str = None):
  79         """
  80         在框架 Web 服务器上注册一条自定义 REST 路由，自动复用框架登录/API Key 鉴权。
  81 
  82         这是把 ZCBOT 当IM 平台的关键接入点：外部系统/页面可通过 HTTP 与插件交互，
  83         而不必自己开 HTTP 服务、自己写鉴权。
  84 
  85         :param path: 路由路径，如 '/api/my/stats' 或 '/my/stats'
  86         :param handler: Flask 视图函数（同 Flask，可返回 jsonify/Response/(resp, status)）
  87         :param methods: 允许的 HTTP 方法，默认 ['GET']
  88         :param auth: 是否复用框架鉴权（默认 True：需登录或有效 API Key）
  89         :param description: 可选说明（当前仅日志记录）
  90         :return: True 表示已挂载；False 表示 Web 未启用（路由已登记，启用后自动挂载）
  91         """
  92         from framework.api import registry as _api_registry
  93         if methods is None:
  94             methods = ['GET']
  95         methods = [m.upper() for m in methods]
  96         self._journal('api', f"{','.join(methods)} {path}")
  97 
  98         # 双进程宿主模式：Web 在核心进程，走远程路由（handler 契约 fn(params)->dict/(status,dict)）
  99         rr = getattr(self._framework, '_remote_routes', None)
 100         if rr is not None:
 101             try:
 102                 rr.register_route(path, methods, handler, auth,
 103                                   plugin=self._plugin_name)
 104             except TypeError:
 105                 # 老版本注册器不收 plugin：退回三参调用，保证插件不因框架升级失配
 106                 rr.register_route(path, methods, handler, auth)
 107             except Exception as e:
 108                 self.log(f"远程 API 路由注册失败 {path} {methods}: {e}")
 109                 return False
 110             if description:
 111                 self.log(f"已注册远程 API 路由 {path} {methods}" + (f" ({description})" if description else ""))
 112             else:
 113                 self.log(f"已注册远程 API 路由 {path} {methods}")
 114             return True
 115 
 116         route = _api_registry.register_route(path, methods, handler, auth,
 117                                              plugin=self._plugin_name)
 118         ok = route is not None
 119         if ok and description:
 120             self.log(f"已注册 API 路由 {path} {methods}" + (f" ({description})" if description else ""))
 121         elif not ok:
 122             self.log(f"已登记 API 路由 {path}（等待 Web 启用后挂载）")
 123         return ok
 124 
 125     # ---- 异步调度 / 文本提取等便捷能力 ----
 126 
 127     def call_async(self, coro):
 128         """
 129         把协程安全地调度到框架主事件循环执行（可从任意线程调用）。
 130 
```
