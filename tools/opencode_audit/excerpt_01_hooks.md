# 第 1 轮侦察材料：framework/hooks.py 第 1~150 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """
   2 扩展点系统（Hook Registry）—— 平台内核的核心契约
   3 
   4 内核只负责最小必要能力：加载插件、路由事件、提供公共服务
   5 （数据库 / 权限 / 服务注册 / 事件总线 / 运行时上下文）。其余一切"行为"
   6 都通过**扩展点（hook point）**向插件与扩展开放，使任意功能都能挂载到
   7 几乎每一个运行环节：
   8 
   9   lifecycle.startup / lifecycle.shutdown          进程启动与关闭
  10   http.before_request / http.after_request       Web 请求前后（before 可短路）
  11   event.before_dispatch / event.after_dispatch   事件进入内核前后（before 返回 False 丢弃）
  12   command.before / command.after                命令执行前后（before 返回 False 跳过）
  13   message.before_send / message.after_send      框架主动发文本前后
  14   action.before / action.after                  任意协议动作调用前后（通知，不短路）
  15   router.before_route / router.after_route      消息路由前后（before 返回 False 跳过路由；after 通知型）
  16   router.message_unmatched                      消息未命中任何命令/关键词时兜底（返回 True 视为接管）
  17 
  18 插件侧用 `ctx.hook(point, handler, priority=50, serial=True)` 注册；同名（插件内）重复注册自动去重。
  19 handler 既可以是普通函数，也可以是 `async def`：
  20 - 事件循环内（内核管线）用 `trigger_async`，可安全 `await`；
  21 - Web / 线程上下文（Flask 请求）用 `trigger_sync`，async handler 会经内核事件循环
  22   fire-and-forget 执行，绝不阻塞请求线程。
  23 
  24 **串行 / 并行声明**：`register(..., serial=...)` 控制同一扩展点内 handler 的执行模型：
  25 - `serial=True`（默认）：按优先级顺序逐个 `await`，前一个完成再执行下一个；
  26   短路扩展点（如 `event.before_dispatch` 返回 False）依赖此语义——首个 False 即终止后续。
  27 - `serial=False`：与本扩展点其他非串行 handler 一起经 `asyncio.gather` 并发执行，
  28   不再保证先后；适合通知型扩展点（如 `*_after_*` / `action.*`）以提升吞吐。
  29   ⚠️ 并行 handler 不参与"串行短路"（竞态下无意义），需要短路语义请显式声明 `serial=True`。
  30 """
  31 import asyncio
  32 import inspect
  33 import logging
  34 import threading
  35 from typing import Callable, List, Tuple
  36 
  37 logger = logging.getLogger('zcbot')
  38 
  39 
  40 def _arity_of(handler):
  41     """算出 handler 能接受几个位置参数、哪些关键字。
  42 
  43     返回 ``(max_pos, free, allowed_kw)``：
  44 
  45     - ``free=True``：handler 有 ``*args`` / ``**kwargs``，或签名读不出来
  46       （内建函数、C 扩展、被装饰器包过）——原样传参，不做任何裁剪；
  47     - ``max_pos``：可接受的位置参数个数上限（``None`` 表示不限）；
  48     - ``allowed_kw``：可接受的参数名集合（``None`` 表示不限）。
  49 
  50     存在的理由：扩展点由框架调用、handler 由插件写，双方的参数表很容易不同步。
  51     `event.before_dispatch` 会传 `(event, bot_name)`，而 handler 只写 `def f(event)`
  52     是极自然的写法；不做适配就是**每条消息一条 TypeError**，日志被刷屏、
  53     功能静默失效。
  54     """
  55     try:
  56         sig = inspect.signature(handler)
  57     except (TypeError, ValueError):
  58         return (None, True, None)
  59     max_pos = 0
  60     allowed = set()
  61     for p in sig.parameters.values():
  62         if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
  63             max_pos += 1
  64             if p.kind is p.POSITIONAL_OR_KEYWORD:
  65                 allowed.add(p.name)
  66         elif p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
  67             return (None, True, None)
  68         elif p.kind is p.KEYWORD_ONLY:
  69             allowed.add(p.name)
  70     return (max_pos, False, allowed)
  71 
  72 
  73 class HookPoints:
  74     """标准扩展点常量（插件也可注册自定义扩展点，字符串点位即可）。"""
  75     LIFECYCLE_STARTUP = 'lifecycle.startup'
  76     LIFECYCLE_SHUTDOWN = 'lifecycle.shutdown'
  77     HTTP_BEFORE_REQUEST = 'http.before_request'
  78     HTTP_AFTER_REQUEST = 'http.after_request'
  79     EVENT_BEFORE_DISPATCH = 'event.before_dispatch'
  80     EVENT_AFTER_DISPATCH = 'event.after_dispatch'
  81     COMMAND_BEFORE = 'command.before'
  82     COMMAND_AFTER = 'command.after'
  83     MESSAGE_BEFORE_SEND = 'message.before_send'
  84     MESSAGE_AFTER_SEND = 'message.after_send'
  85     ACTION_BEFORE = 'action.before'
  86     ACTION_AFTER = 'action.after'
  87     ROUTER_BEFORE_ROUTE = 'router.before_route'
  88     ROUTER_AFTER_ROUTE = 'router.after_route'
  89     ROUTER_MESSAGE_UNMATCHED = 'router.message_unmatched'
  90     MESSAGE_CLASSIFIED = 'message.classified'   # 消息事件完成四类分类后（携带 ev.category / ev.categories）
  91 
  92 
  93 class HookPriority:
  94     """扩展点优先级的**段位约定**（数值小的先执行）。
  95 
  96     HookRegistry 早就按优先级排序并支持短路，但插件普遍用默认 50，顺序实际
  97     退化成"注册顺序"——两个都自称守卫的插件谁先跑，取决于加载拓扑序，改一个
  98     插件的依赖就可能悄悄换掉拦截顺序。这里把段位固定下来：
  99 
 100     - EARLY   前置改写：归一化字段补全、敏感词替换（会改事件内容）
 101     - GUARD   拦截判定：黑白名单、频控、场景开关（返回 False 丢事件）
 102     - NORMAL  普通观察/统计（默认值，与历史行为一致）
 103     - LATE    兜底与收尾：需要看到被前面处理过的最终事件的逻辑
 104 
 105     同段位内仍按注册顺序执行（稳定排序），跨段位的先后是有保证的。
 106     """
 107     # 【踩坑保佑】v1.17.0 Hook 优先级段位
 108     # 坑：插件普遍用默认50，顺序退化成注册顺序  →  HookPriority
 109     #                    _ooOoo_
 110     #                   o8888888o
 111     #                   88" . "88
 112     #                   (| -_- |)
 113     #                    O\ = /O
 114     #                ____/`---'\____
 115     #              .   ' \\| |// `.
 116     #               / \\||| : |||// \
 117     #             / _||||| -:- |||||- \
 118     #               | | \\\ - /// | |
 119     #             | \_| ''\---/'' | |
 120     #              \ .-\__ `-` ___/-. /
 121     #           ___`. .' /--.--\ `. . __
 122     #        ."" '< `.___\_<|>_/___.' >'"".
 123     #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
 124     #         \ \ `-. \_ __\ /__ _/ .-` / /
 125     # ======`-.____`-.___\_____/___.-`____.-'======
 126     #                    `=---='
 127     #
 128     # .............................................
 129     #          佛祖保佑             永无BUG
 130 
 131     EARLY = 10
 132     GUARD = 20
 133     NORMAL = 50
 134     LATE = 80
 135 
 136 
 137 # 各扩展点约定返回 False 时是否触发"短路"语义
 138 _SHORT_CIRCUIT = {
 139     HookPoints.HTTP_BEFORE_REQUEST: True,     # 返回 Flask Response 即短路
 140     HookPoints.EVENT_BEFORE_DISPATCH: True,   # 返回 False 丢弃事件
 141     HookPoints.COMMAND_BEFORE: True,          # 返回 False 跳过该命令
 142     HookPoints.MESSAGE_BEFORE_SEND: True,     # 返回 False 取消发送
 143     HookPoints.ROUTER_BEFORE_ROUTE: True,     # 返回 False 跳过本次路由
 144 }
 145 
 146 # 这些扩展点在无可用事件循环时，async handler 允许被跳过（避免阻塞）
 147 _ALLOW_SKIP_ASYNC = {
 148     HookPoints.HTTP_BEFORE_REQUEST,
 149     HookPoints.HTTP_AFTER_REQUEST,
 150 }
```
