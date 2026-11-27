# 第 4 轮侦察材料：framework/ctx/base.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """
   2 插件上下文对象 (ctx)
   3 向插件暴露：命令注册、API调用、数据库、日志、配置读取、事件、定时任务
   4 
   5 异步模型：
   6 - async handler 中优先使用 aapi() / asend_msg() 等 async 方法，不阻塞事件循环
   7 - 旧插件继续使用 api() / send_msg() 等同步方法（内部桥接到主事件循环，自动兼容）
   8 
   9 能力按域拆分至 mixin：
  10 - ctx_messaging  协议中立快捷动作 / 身份判断 / 群级插件开关
  11 - ctx_events     命令 / 定时任务 / 事件订阅 / 扩展点 / 协议 API
  12 - ctx_webui      仪表盘卡片 / WebUI 内嵌 / 管理页扩展
  13 - ctx_db         同步/异步数据库操作
  14 """
  15 import json
  16 import logging
  17 import time
  18 from concurrent.futures import ThreadPoolExecutor
  19 from typing import Callable
  20 
  21 logger = logging.getLogger('zcbot')
  22 
  23 # 全局线程池，用于异步执行耗时操作（如图片渲染），不阻塞主消息处理流程
  24 _async_executor = ThreadPoolExecutor(
  25     max_workers=4,
  26     thread_name_prefix='async_plugin'
  27 )
  28 
  29 from framework.ctx.db import PluginDatabaseMixin
  30 from framework.ctx.events import PluginEventsMixin
  31 from framework.ctx.messaging import PluginMessagingMixin
  32 from framework.ctx.webui import PluginWebuiMixin
  33 
  34 
  35 class PluginContext(PluginMessagingMixin, PluginEventsMixin, PluginWebuiMixin, PluginDatabaseMixin):
  36     """插件上下文，传递给 register(ctx) 函数"""
  37 
  38     class _PluginLogger:
  39         """插件日志适配器：提供 .info/.warning/.error/.debug 接口，自动加插件名前缀"""
  40         def __init__(self, plugin_name: str):
  41             self._name = plugin_name
  42 
  43         def info(self, msg, *args, **kwargs):
  44             logger.info(f"[{self._name}] {msg}", *args, **kwargs)
  45 
  46         def warning(self, msg, *args, **kwargs):
  47             logger.warning(f"[{self._name}] {msg}", *args, **kwargs)
  48 
  49         def error(self, msg, *args, **kwargs):
  50             logger.error(f"[{self._name}] {msg}", *args, **kwargs)
  51 
  52         def debug(self, msg, *args, **kwargs):
  53             logger.debug(f"[{self._name}] {msg}", *args, **kwargs)
  54 
  55         def warn(self, msg, *args, **kwargs):
  56             logger.warning(f"[{self._name}] {msg}", *args, **kwargs)
  57 
  58         def exception(self, msg, *args, **kwargs):
  59             logger.exception(f"[{self._name}] {msg}", *args, **kwargs)
  60 
  61     def __init__(self, plugin_name: str, framework):
  62         self._plugin_name = plugin_name
  63         self._framework = framework  # 框架引擎引用
  64         self._db = framework.db
  65         self._commands = []  # 本次注册周期收集的命令
  66         self._tasks = []     # 本次注册周期收集的任务
  67         self._dashboard_cards = []  # 仪表盘卡片
  68         self._raw_message_handlers = []  # 原始消息处理器（收到原始消息事件，可选择性接管）
  69         self._group_extensions = []  # WebUI 群组管理页插件扩展
  70         self._user_extensions = []   # WebUI 用户管理页插件扩展
  71         self._logger = self._PluginLogger(plugin_name)
  72         self._config_cache = {}      # key -> (value, timestamp) 插件配置 TTL 缓存
  73         self._config_cache_ttl = 30  # 缓存有效期（秒），避免 async handler 同步查库阻塞事件循环
  74         self._register_journal = []  # 加载期注册动作流水（黑匣子 plugin_loaded 摘要用）
  75     @property
  76     def actions(self):
  77         """
  78         协议中立的动作调用面（推荐新代码使用）。
  79         优先按当前事件来源（current_source_var）选对应适配器——多接入端并存时不串线；
  80         无来源上下文时，OneBot 已注册则用 services['onebot_api']（旧插件兼容），
  81         否则协议无关 ActionProxy 转发到 api_caller；都没有才抛 RuntimeError。
  82         """
  83         services = self._framework.services
  84         source = None
  85         try:
  86             from framework.runtime import current_source_var
  87             source = current_source_var.get()
  88         except Exception as _exc:
  89             logger.debug('[actions] 捕获异常后按降级路径继续: %s', _exc)
  90             source = None
  91         if source:
  92             try:
  93                 adapter = services.adapter_for_source(source)
  94             except Exception as _exc:
  95                 logger.debug('[actions] 捕获异常后按降级路径继续: %s', _exc)
  96                 adapter = None
  97             if adapter is not None:
  98                 # OneBot 来源：优先类型化 onebot_api（旧 ctx.onebot 写法）
  99                 if getattr(adapter, 'adapter_id', '') == 'onebot' or \
 100                         type(adapter).__name__ == 'OneBotAdapter':
 101                     api = services.get('onebot_api')
 102                     if api is not None:
 103                         return api
 104                 from framework.messaging.protocol import ActionProxy
 105                 return ActionProxy(adapter)
 106         api = services.get('onebot_api')
 107         if api is not None:
 108             return api
 109         caller = services.get('api_caller')
 110         if caller is not None:
 111             from framework.messaging.protocol import ActionProxy
 112             return ActionProxy(caller)
 113         raise RuntimeError("无可用协议适配器（请启用一个接入端，如 core_plugins.onebot_adapter 或 core_plugins.http_inject）")
 114 
 115     @property
 116     def onebot(self):
 117         """
 118         兼容别名：等价于 ctx.actions（历史插件便捷面）。
 119         OneBot 11 API 封装优先返回 services['onebot_api']；
 120         否则回落到协议无关的 ActionProxy。
 121         """
 122         return self.actions
 123     @property
 124     def logger(self):
 125         """获取插件日志记录器（标准 logger 接口）"""
 126         return self._logger
 127     @property
 128     def plugin_name(self) -> str:
 129         """获取当前插件名"""
 130         return self._plugin_name
```
