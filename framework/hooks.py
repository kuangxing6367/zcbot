"""
扩展点系统（Hook Registry）—— 平台内核的核心契约

内核只负责最小必要能力：加载插件、路由事件、提供公共服务
（数据库 / 权限 / 服务注册 / 事件总线 / 运行时上下文）。其余一切"行为"
都通过**扩展点（hook point）**向插件与扩展开放，使任意功能都能挂载到
几乎每一个运行环节：

  lifecycle.startup / lifecycle.shutdown          进程启动与关闭
  http.before_request / http.after_request       Web 请求前后（before 可短路）
  event.before_dispatch / event.after_dispatch   事件进入内核前后（before 返回 False 丢弃）
  command.before / command.after                命令执行前后（before 返回 False 跳过）
  message.before_send / message.after_send      框架主动发文本前后
  action.before / action.after                  任意协议动作调用前后（通知，不短路）
  router.before_route / router.after_route      消息路由前后（before 返回 False 跳过路由；after 通知型）
  router.message_unmatched                      消息未命中任何命令/关键词时兜底（返回 True 视为接管）

插件侧用 `ctx.hook(point, handler, priority=50, serial=True)` 注册；同名（插件内）重复注册自动去重。
handler 既可以是普通函数，也可以是 `async def`：
- 事件循环内（内核管线）用 `trigger_async`，可安全 `await`；
- Web / 线程上下文（Flask 请求）用 `trigger_sync`，async handler 会经内核事件循环
  fire-and-forget 执行，绝不阻塞请求线程。

**串行 / 并行声明**：`register(..., serial=...)` 控制同一扩展点内 handler 的执行模型：
- `serial=True`（默认）：按优先级顺序逐个 `await`，前一个完成再执行下一个；
  短路扩展点（如 `event.before_dispatch` 返回 False）依赖此语义——首个 False 即终止后续。
- `serial=False`：与本扩展点其他非串行 handler 一起经 `asyncio.gather` 并发执行，
  不再保证先后；适合通知型扩展点（如 `*_after_*` / `action.*`）以提升吞吐。
  ⚠️ 并行 handler 不参与"串行短路"（竞态下无意义），需要短路语义请显式声明 `serial=True`。
"""
import asyncio
import inspect
import logging
import threading
from typing import Callable, List, Tuple

logger = logging.getLogger('zcbot')


def _arity_of(handler):
    """算出 handler 能接受几个位置参数、哪些关键字。

    返回 ``(max_pos, free, allowed_kw)``：

    - ``free=True``：handler 有 ``*args`` / ``**kwargs``，或签名读不出来
      （内建函数、C 扩展、被装饰器包过）——原样传参，不做任何裁剪；
    - ``max_pos``：可接受的位置参数个数上限（``None`` 表示不限）；
    - ``allowed_kw``：可接受的参数名集合（``None`` 表示不限）。

    存在的理由：扩展点由框架调用、handler 由插件写，双方的参数表很容易不同步。
    `event.before_dispatch` 会传 `(event, bot_name)`，而 handler 只写 `def f(event)`
    是极自然的写法；不做适配就是**每条消息一条 TypeError**，日志被刷屏、
    功能静默失效。
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return (None, True, None)
    max_pos = 0
    allowed = set()
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            max_pos += 1
            if p.kind is p.POSITIONAL_OR_KEYWORD:
                allowed.add(p.name)
        elif p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            return (None, True, None)
        elif p.kind is p.KEYWORD_ONLY:
            allowed.add(p.name)
    return (max_pos, False, allowed)


class HookPoints:
    """标准扩展点常量（插件也可注册自定义扩展点，字符串点位即可）。"""
    LIFECYCLE_STARTUP = 'lifecycle.startup'
    LIFECYCLE_SHUTDOWN = 'lifecycle.shutdown'
    HTTP_BEFORE_REQUEST = 'http.before_request'
    HTTP_AFTER_REQUEST = 'http.after_request'
    EVENT_BEFORE_DISPATCH = 'event.before_dispatch'
    EVENT_AFTER_DISPATCH = 'event.after_dispatch'
    COMMAND_BEFORE = 'command.before'
    COMMAND_AFTER = 'command.after'
    MESSAGE_BEFORE_SEND = 'message.before_send'
    MESSAGE_AFTER_SEND = 'message.after_send'
    ACTION_BEFORE = 'action.before'
    ACTION_AFTER = 'action.after'
    ROUTER_BEFORE_ROUTE = 'router.before_route'
    ROUTER_AFTER_ROUTE = 'router.after_route'
    ROUTER_MESSAGE_UNMATCHED = 'router.message_unmatched'
    MESSAGE_CLASSIFIED = 'message.classified'   # 消息事件完成四类分类后（携带 ev.category / ev.categories）


class HookPriority:
    """扩展点优先级的**段位约定**（数值小的先执行）。

    HookRegistry 早就按优先级排序并支持短路，但插件普遍用默认 50，顺序实际
    退化成"注册顺序"——两个都自称守卫的插件谁先跑，取决于加载拓扑序，改一个
    插件的依赖就可能悄悄换掉拦截顺序。这里把段位固定下来：

    - EARLY   前置改写：归一化字段补全、敏感词替换（会改事件内容）
    - GUARD   拦截判定：黑白名单、频控、场景开关（返回 False 丢事件）
    - NORMAL  普通观察/统计（默认值，与历史行为一致）
    - LATE    兜底与收尾：需要看到被前面处理过的最终事件的逻辑

    同段位内仍按注册顺序执行（稳定排序），跨段位的先后是有保证的。
    """
    # 【踩坑保佑】v1.17.0 Hook 优先级段位
    # 坑：插件普遍用默认50，顺序退化成注册顺序  →  HookPriority
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    EARLY = 10
    GUARD = 20
    NORMAL = 50
    LATE = 80


# 各扩展点约定返回 False 时是否触发"短路"语义
_SHORT_CIRCUIT = {
    HookPoints.HTTP_BEFORE_REQUEST: True,     # 返回 Flask Response 即短路
    HookPoints.EVENT_BEFORE_DISPATCH: True,   # 返回 False 丢弃事件
    HookPoints.COMMAND_BEFORE: True,          # 返回 False 跳过该命令
    HookPoints.MESSAGE_BEFORE_SEND: True,     # 返回 False 取消发送
    HookPoints.ROUTER_BEFORE_ROUTE: True,     # 返回 False 跳过本次路由
}

# 这些扩展点在无可用事件循环时，async handler 允许被跳过（避免阻塞）
_ALLOW_SKIP_ASYNC = {
    HookPoints.HTTP_BEFORE_REQUEST,
    HookPoints.HTTP_AFTER_REQUEST,
}


class HookRegistry:
    """扩展点注册表：按扩展点收集 handler，按优先级触发。

    性能：注册/注销低频、触发高频，采用"写时复制快照"——
    注册变更时失效快照，触发热路径无锁读不可变 tuple，零拷贝。
    """

    def __init__(self, framework=None):
        self._framework = framework
        self._hooks = {}                 # point -> list[[priority, name, handler]]
        self._snapshots = {}             # point -> tuple((priority, name, handler))，写时复制
        self._arity = {}                 # (point, name) -> _arity_of 的结果
        self._lock = threading.Lock()

    def _fit_args(self, point: str, name: str, args: tuple, kwargs: dict):
        """把参数裁剪到 handler 能接受的范围（注册时算好，热路径只查表）。"""
        info = self._arity.get((point, name))
        if info is None:
            return args, kwargs
        max_pos, free, allowed = info
        if free:
            return args, kwargs
        if max_pos is not None and len(args) > max_pos:
            args = args[:max_pos]
        if kwargs and allowed is not None:
            kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        return args, kwargs

    def _invalidate(self, point: str):
        """失效指定扩展点快照（注册/注销后调用）"""
        self._snapshots.pop(point, None)

    def _snapshot(self, point: str) -> tuple:
        """读取扩展点处理器快照（无锁热路径；变更方负责失效重建）"""
        snap = self._snapshots.get(point)
        if snap is not None:
            return snap
        with self._lock:
            entries = self._hooks.get(point)
            if not entries:
                self._snapshots[point] = ()
                return ()
            snap = tuple(tuple(e) for e in entries)
            self._snapshots[point] = snap
            return snap

    def register(self, point: str, name: str, handler: Callable,
                 priority: int = HookPriority.NORMAL, serial: bool = True):
        """注册一个扩展点处理器。同名（同一 name）重复注册自动覆盖（去重）。

        :param serial: 是否要求串行执行（默认 True）。
            - True：按顺序等待前一个 handler 完成再执行下一个（短路扩展点依赖此语义）。
            - False：与本扩展点其他非串行 handler 并发执行（asyncio.gather），不再保证先后，
              适合通知型扩展点（如 *_after_*）以提升吞吐；不参与串行短路。
        """
        if not callable(handler):
            raise TypeError(f"扩展点 [{point}] 的 handler 必须可调用: {name}")
        arity = _arity_of(handler)
        with self._lock:
            self._arity[(point, name)] = arity
            entries = self._hooks.setdefault(point, [])
            for entry in entries:
                if entry[1] == name:
                    entry[0], entry[2], entry[3] = priority, handler, serial
                    break
            else:
                entries.append([priority, name, handler, serial])
            entries.sort(key=lambda e: e[0])
        self._invalidate(point)

    def unregister(self, point: str, name: str):
        """注销指定 name 的扩展点处理器。"""
        with self._lock:
            self._arity.pop((point, name), None)
            entries = self._hooks.get(point)
            if entries:
                self._hooks[point] = [e for e in entries if e[1] != name]
        self._invalidate(point)

    def unregister_prefix(self, point: str, prefix: str):
        """注销指定扩展点上所有 name 以 prefix 开头的处理器。

        供 PluginContext.unhook() 用：ctx.hook() 现在允许同插件同点位注册多个
        handler（名字带 #2/#3 后缀），精确 unregister 只能删一个。
        """
        with self._lock:
            entries = self._hooks.get(point)
            if not entries:
                return
            kept = [e for e in entries if not e[1].startswith(prefix)]
            if len(kept) == len(entries):
                return
            self._hooks[point] = kept
            for e in entries:
                if e[1].startswith(prefix):
                    self._arity.pop((point, e[1]), None)
        self._invalidate(point)

    def clear_plugin(self, plugin_name: str):
        """插件卸载时清除其全部扩展点（按 name 前缀 `plugin_name:` 识别）。"""
        prefix = f"{plugin_name}:"
        with self._lock:
            for point, entries in list(self._hooks.items()):
                kept = [e for e in entries if not e[1].startswith(prefix)]
                if len(kept) != len(entries):
                    self._hooks[point] = kept
                    for e in entries:
                        if e[1].startswith(prefix):
                            self._arity.pop((point, e[1]), None)
        # 涉及多个扩展点，简单起见全部失效
        self._snapshots.clear()

    def has(self, point: str) -> bool:
        """该扩展点是否存在已注册处理器。"""
        return bool(self._snapshot(point))

    def count(self, point: str) -> int:
        """查询某扩展点已注册的处理器数量（调试/监控用）。"""
        return len(self._snapshot(point))

    def points(self) -> list:
        """列出所有已注册的扩展点名称（调试/监控用）。"""
        with self._lock:
            return sorted(self._hooks.keys())

    async def trigger_async(self, point: str, *args, **kwargs):
        """
        在事件循环内触发（可安全 await async handler）。
        返回各 handler 的结果列表；调用方可用 `False in results` 判断是否短路。

        执行模型（见 register 的 serial 参数）：
        - 串行组（serial=True，按优先级顺序）：逐个 await，保持短路语义——
          短路扩展点遇到首个 False 即终止后续（含并行组不再触发）。
        - 并行组（serial=False）：与本组其他 handler 经 asyncio.gather 并发执行，
          收集全部结果；不参与"串行短路"（竞态下无意义）。
        """
        # 【踩坑保佑】设计哲学 5 热路径零开销
        # 坑：空触发必须零成本  →  trigger_async
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

        short_circuit = point in _SHORT_CIRCUIT
        results = []
        snap = self._snapshot(point)
        if not snap:
            # 无订阅者的扩展点是热路径最常见形态（每事件 5 次 trigger，
            # 多数点位无人注册），直接返回空结果，省去循环与短路判断
            return results

        # 按声明拆分为串行组（按优先级顺序）与并行组
        serial_entries = [(p, n, h) for (p, n, h, s) in snap if s]
        parallel_entries = [(p, n, h) for (p, n, h, s) in snap if not s]

        # 1) 串行组：顺序 await，保持短路语义（首个 False 即终止，并行组也不再触发）
        for _, name, handler in serial_entries:
            try:
                # 参数裁剪也放进 try：handler 签名畸形时不能让整个 trigger 崩掉
                hargs, hkwargs = self._fit_args(point, name, args, kwargs)
                if asyncio.iscoroutinefunction(handler):
                    value = await handler(*hargs, **hkwargs)
                else:
                    value = handler(*hargs, **hkwargs)
                results.append(value)
                if short_circuit and value is False:
                    return results
            except Exception as e:
                logger.error(f"扩展点 [{point}] 处理器 [{name}] 异常: {e}", exc_info=True)
                # fail-closed：短路型点位上，Guard 自己崩了必须等同「否决」，
                # 否则 `False in results` 判不出来，事件静默放行（原来就是这个问题）。
                # 并行组不参与短路，仍按 None 处理。
                if short_circuit:
                    results.append(False)
                    return results

        # 2) 并行组：与本组其他 handler 并发执行（asyncio.gather），收集全部结果
        if parallel_entries:
            async def _run(name, handler):
                hargs, hkwargs = self._fit_args(point, name, args, kwargs)
                try:
                    if asyncio.iscoroutinefunction(handler):
                        return await handler(*hargs, **hkwargs)
                    return handler(*hargs, **hkwargs)
                except Exception as e:
                    logger.error(f"扩展点 [{point}] 处理器 [{name}] 异常: {e}", exc_info=True)
                    return None
            para_results = await asyncio.gather(*(_run(n, h) for _, n, h in parallel_entries))
            results.extend(para_results)
        return results

    def trigger_sync(self, point: str, *args, **kwargs):
        """
        在 Web / 线程上下文触发：
        - sync handler 直接执行；
        - async handler 经内核 loop fire-and-forget（不阻塞当前线程），
          若该扩展点不允许跳过且无可用 loop，则记告警并跳过。
        """
        # 【踩坑保佑】设计哲学 7 短路语义
        # 坑：遇到首个False提前终止  →  trigger_sync
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

        results = []
        short_circuit = point in _SHORT_CIRCUIT
        loop = getattr(self._framework, 'loop', None) if self._framework else None
        for _, name, handler, _serial in self._snapshot(point):
            try:
                hargs, hkwargs = self._fit_args(point, name, args, kwargs)
                if asyncio.iscoroutinefunction(handler):
                    if loop is not None and loop.is_running():
                        asyncio.run_coroutine_threadsafe(handler(*hargs, **hkwargs), loop)
                        results.append(None)
                    elif point not in _ALLOW_SKIP_ASYNC:
                        logger.warning(
                            f"扩展点 [{point}] 的 async 处理器 [{name}] 无可用事件循环，已跳过")
                        results.append(None)
                else:
                    value = handler(*hargs, **hkwargs)
                    results.append(value)
                    if short_circuit and value is False:
                        return results
            except Exception as e:
                logger.error(f"扩展点 [{point}] 处理器 [{name}] 异常: {e}")
                # 与 trigger_async 一致：短路型点位上 Guard 崩溃按「否决」处理
                if short_circuit:
                    results.append(False)
                    return results
        return results
