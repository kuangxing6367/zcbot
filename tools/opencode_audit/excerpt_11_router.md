# 第 11 轮侦察材料：framework/messaging/router.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 
   2 #                    _ooOoo_
   3 #                   o8888888o
   4 #                   88" . "88
   5 #                   (| -_- |)
   6 #                    O\ = /O
   7 #                ____/`---'\____
   8 #              .   ' \\| |// `.
   9 #               / \\||| : |||// \
  10 #             / _||||| -:- |||||- \
  11 #               | | \\\ - /// | |
  12 #             | \_| ''\---/'' | |
  13 #              \ .-\__ `-` ___/-. /
  14 #           ___`. .' /--.--\ `. . __
  15 #        ."" '< `.___\_<|>_/___.' >'"".
  16 #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
  17 #         \ \ `-. \_ __\ /__ _/ .-` / /
  18 # ======`-.____`-.___\_____/___.-`____.-'======
  19 #                    `=---='
  20 #
  21 # .............................................
  22 #          佛祖保佑             永无BUG
  23 
  24 """
  25 消息路由器（高性能版）
  26 按插件优先级顺序分发消息，匹配静态命令
  27 - 插件注册的动态命令（is_dynamic=1）仅用于展示，不参与命令匹配
  28 - 系统级动态命令（dynamic_commands 表，关键词自动回复）在插件未命中时兜底匹配
  29 - 文本消息统一监听通道：插件命令未命中时广播 `message` 事件（内容监听型插件可用 ctx.on('message')）
  30 
  31 异步模型：
  32 - 后台刷新任务周期性（默认 5s）从 DB 构建纯内存路由表（插件序 + 预编译命令 + 关键词规则），
  33   路由热路径零 DB 查询、零线程切换（内存路由思路）
  34 - 命令/关键词命中计数交给 framework.core.stats_writer 批量落库，不阻塞事件循环
  35 - handler 支持 async def（直接 await）和普通 def（转线程执行）
  36 """
  37 import asyncio
  38 import logging
  39 import re
  40 import threading
  41 import time
  42 from collections import OrderedDict
  43 from typing import Optional
  44 
  45 from framework.hooks import HookPoints
  46 from framework.log_broker import log_broker
  47 from framework.messaging.event import _has_text_segment, normalize_match_text
  48 from framework.messaging.router_keywords import KeywordReplyMixin, _KeywordRule
  49 
  50 logger = logging.getLogger('zcbot')
  51 
  52 
  53 
  54 class _RouteCommand:
  55     """预编译后的路由命令（构建一次，热路径直接复用）"""
  56 
  57     __slots__ = ('id', 'pattern', 'handler_name', 'require_level', 'require_perm',
  58                  'rx', 'simple', 'aliases')
  59 
  60     def __init__(self, id, pattern, handler_name, require_level,
  61                  rx, simple, aliases, require_perm=''):
  62         self.id = id
  63         self.pattern = pattern
  64         self.handler_name = handler_name
  65         self.require_level = require_level
  66         self.require_perm = require_perm  # 权限节点要求（节点式），空=不限制
  67         self.rx = rx          # 编译后的正则，或 None
  68         self.simple = simple  # 简单前缀匹配 pattern，或 None
  69         self.aliases = aliases
  70 
  71 
  72 from framework.blackbox import record as _bb_record
  73 from framework.messaging.router_match import (
  74     RouterMatchMixin,
  75     SimpleMatch,
  76     _PluginRoute,
  77     build_command_index,
  78 )
  79 
  80 
  81 class _PermissionDecisionCache:
  82     """命令权限判定的进程内缓存（TTL + 容量上限 + 线程安全 + 显式失效）。
  83 
  84     缓存值是一次权限解析的不可变快照 ``(role, PermissionSet)``：route() 把它
  85     预注入 Event，使 ``ev.role`` / ``ev.has_perm`` 在热路径上零 DB、零线程切换。
  86 
  87     设计取舍：
  88       - TTL 用 ``time.monotonic()``，不受系统时钟回拨影响；默认与路由表刷新
  89         间隔同量级，即便外部未接失效入口，权限变更最多一个 TTL 生效。
  90       - 容量上限用 OrderedDict LRU，put 时严格淘汰，避免长期运行无界增长。
  91       - 所有访问持 RLock；锁内只做内存字典操作，绝不 await / 查库，避免与
  92         ``asyncio.to_thread`` 互等造成死锁。
  93       - 失效由 ``MessageRouter.invalidate_permissions()`` 统一触发（清本缓存
  94         + 清底层 Event / perm 缓存），权限变更、权限引擎 reload、插件重载都会
  95         走到（插件重载 / Web 改命令经 ``_invalidate_cache()`` 联动）。
  96     """
  97 
  98     def __init__(self, ttl: float = 5.0, maxsize: int = 4096):
  99         self.ttl = ttl
 100         self.maxsize = maxsize
 101         self._data = OrderedDict()   # key -> (role, pset, ts_monotonic)
 102         self._lock = threading.RLock()
 103 
 104     def get(self, key):
 105         now = time.monotonic()
 106         with self._lock:
 107             ent = self._data.get(key)
 108             if ent is None:
 109                 return None
 110             role, pset, ts = ent
 111             if now - ts > self.ttl:
 112                 self._data.pop(key, None)
 113                 return None
 114             self._data.move_to_end(key)
 115             return role, pset
 116 
 117     def put(self, key, role, pset):
 118         with self._lock:
 119             self._data[key] = (role, pset, time.monotonic())
 120             self._data.move_to_end(key)
 121             while len(self._data) > self.maxsize:
 122                 self._data.popitem(last=False)
 123 
 124     def invalidate(self, user_id=None):
 125         with self._lock:
 126             if user_id is None:
 127                 self._data.clear()
 128                 return
 129             uid = str(user_id)
 130             for k in [k for k in self._data if k[0] == uid]:
```
