"""
事件总线模块
支持 ctx.on(event_name, handler) 和 ctx.emit(event_name, payload)

异步模型：
- aemit() 异步发布，订阅 handler 支持 async def（直接 await）和普通 def（转线程）
- emit()  同步桥接，供旧插件/非 loop 线程使用
- 订阅/退订线程安全（插件可能在 executor 线程中注册）

性能：订阅低频、发布高频，采用"写时复制快照"——订阅变更时失效快照，
aemit 热路径无锁读不可变 tuple；无订阅者时零锁零拷贝直接返回。
"""
import asyncio
import contextvars
import logging
import threading
import time
import uuid
from collections import deque
from typing import Callable, Dict, List

logger = logging.getLogger('zcbot')

# 第 6 轮：因果链 / 事件风暴 / 毒消息收口。
# 依据：CWE-674 不受控递归、CWE-835 无法退出的循环、CWE-400 资源耗尽、
# CloudEvents 1.0 的 correlation/causation 扩展属性、事件驱动架构里
# event storm / poison message / DLQ / backpressure 的通行做法。
# 实测修复前：handler 内再 emit 同名事件会递归 328 层直到 RecursionError；
# A→B→A 乒乓同样 328 次；异常 handler 只打日志、无任何隔离记录。
_MAX_CAUSAL_DEPTH = 8          # 单条因果链最大嵌套层数
_MAX_TRACE_EVENTS = 64         # 同一 trace 内累计发布上限
_MAX_SAME_EVENT = 3            # 同一事件名在一条链上最多出现次数（环检测）
_DLQ_MAX = 256                 # 死信队列有界，避免它自己变成内存漏洞

# contextvar 会随 asyncio.create_task / asyncio.to_thread 自动复制，所以
# emit() 同步桥接与线程内二次发布都能继承同一条因果链，无需改 payload。
_CAUSAL_VAR = contextvars.ContextVar('zcbot_event_causal', default=None)


def _child_scope(parent, event_name):
    """从父因果域派生子域；返回 (scope, prior_same_event)。"""
    if parent is None:
        return ({'trace_id': uuid.uuid4().hex, 'depth': 0,
                 'event_name': event_name, 'event_count': 1,
                 'chain': {event_name: 1}}, 0)
    chain = dict(parent['chain'])
    prior = int(chain.get(event_name, 0))
    chain[event_name] = prior + 1
    return ({'trace_id': parent['trace_id'], 'depth': parent['depth'] + 1,
             'event_name': event_name,
             'event_count': parent['event_count'] + 1,
             'chain': chain}, prior)


def current_causal():
    """供插件/日志读取当前因果域（不修改 payload，向后兼容）。"""
    scope = _CAUSAL_VAR.get()
    if scope is None:
        return None
    return {'trace_id': scope['trace_id'], 'depth': scope['depth'],
            'event_name': scope['event_name'],
            'event_count': scope['event_count'],
            'chain': dict(scope['chain'])}


class EventBus:
    """轻量级事件总线"""

    def __init__(self):
        self._subscribers: dict[str, list[dict]] = {}  # event_name -> [{plugin_name, handler}]
        self._snapshots: dict[str, tuple] = {}         # event_name -> ((plugin_name, handler), ...)，写时复制
        self._lock = threading.Lock()
        self._dead_letters = deque(maxlen=_DLQ_MAX)
        self._pending_tasks = set()   # 保存 emit() fire-and-forget 任务引用，防被 GC
        self._total_emits = 0
        self._total_rejected = 0
        self._total_failures = 0

    def _invalidate(self, event_name: str):
        """失效指定事件快照（订阅/退订后调用）"""
        self._snapshots.pop(event_name, None)

    def _snapshot(self, event_name: str) -> tuple:
        """读取订阅者快照（无锁热路径；变更方负责失效重建）"""
        snap = self._snapshots.get(event_name)
        if snap is not None:
            return snap
        with self._lock:
            subs = self._subscribers.get(event_name)
            if not subs:
                self._snapshots[event_name] = ()
                return ()
            snap = tuple((s['plugin_name'], s['handler']) for s in subs)
            self._snapshots[event_name] = snap
            return snap

    def subscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """订阅事件（同一插件同一 handler 重复订阅自动去重，防止心跳重注册后重复触发）"""
        with self._lock:
            subs = self._subscribers.setdefault(event_name, [])
            for s in subs:
                if s['plugin_name'] == plugin_name and s['handler'] == handler:
                    return  # 已订阅，去重
            subs.append({
                'plugin_name': plugin_name,
                'handler': handler
            })
            self._invalidate(event_name)
        logger.debug(f"事件订阅: [{plugin_name}] → {event_name}")

    def once(self, event_name: str, plugin_name: str, handler: Callable):
        """
        一次性订阅：事件首次触发后自动退订（无论 handler 返回什么）。
        适用于"只等一次"的场景（如等待某个异步结果/状态变更）。
        """
        async def _wrapper(payload):
            try:
                if asyncio.iscoroutinefunction(handler):
                    return await handler(payload)
                return handler(payload)
            finally:
                self.unsubscribe(event_name, plugin_name, _wrapper)
        self.subscribe(event_name, plugin_name, _wrapper)

    def unsubscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """按 handler 精确退订某事件的订阅。"""
        with self._lock:
            subs = self._subscribers.get(event_name)
            if not subs:
                return
            kept = [s for s in subs
                    if not (s['plugin_name'] == plugin_name and s['handler'] == handler)]
            if len(kept) == len(subs):
                return
            if kept:
                self._subscribers[event_name] = kept
            else:
                del self._subscribers[event_name]
            self._invalidate(event_name)

    def unsubscribe_plugin(self, plugin_name: str):
        """移除某插件的所有订阅"""
        with self._lock:
            for event_name in list(self._subscribers.keys()):
                self._subscribers[event_name] = [
                    s for s in self._subscribers[event_name]
                    if s['plugin_name'] != plugin_name
                ]
                if not self._subscribers[event_name]:
                    del self._subscribers[event_name]
                self._invalidate(event_name)

    def subscribers_of(self, event_name: str) -> int:
        """查询某事件的当前订阅者数量（调试/监控用）。"""
        return len(self._snapshot(event_name))

    def subscriber_names(self, event_name: str) -> list:
        """返回某事件当前的订阅插件名列表（黑匣子 broadcast 阶段用，去重保序）。"""
        out = []
        seen = set()
        for s in self._snapshot(event_name):
            name = s.get('plugin_name')
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def stats(self) -> dict:
        """返回事件总线订阅统计：{event_name: 订阅数}（调试/监控用）。"""
        with self._lock:
            return {name: len(subs) for name, subs in self._subscribers.items()}

    def _dead_letter(self, event_name, payload, reason, scope,
                     plugin_name='', detail=''):
        """有界 DLQ：记录被拒绝/反复失败的事件，供运维查询，不自动重放。

        只存摘要不存整包 payload（避免超大事件把死信队列本身撑爆）。
        """
        keys = list(payload.keys())[:20] if isinstance(payload, dict) else []
        cur = current_causal()
        entry = {
            'ts': time.time(), 'event_name': event_name, 'reason': reason,
            'plugin_name': plugin_name, 'detail': str(detail)[:200],
            'payload_keys': keys,
            'trace_id': (scope or {}).get('trace_id') or (cur or {}).get('trace_id'),
            'depth': (scope or {}).get('depth', (cur or {}).get('depth')),
        }
        with self._lock:
            self._dead_letters.append(entry)
        return entry

    def dead_letters(self):
        """返回死信队列快照（最多 _DLQ_MAX 条）。"""
        with self._lock:
            return list(self._dead_letters)

    def causal_stats(self):
        """因果链与流控统计（诊断 / 告警用）。"""
        with self._lock:
            return {
                'max_depth': _MAX_CAUSAL_DEPTH,
                'max_trace_events': _MAX_TRACE_EVENTS,
                'max_same_event': _MAX_SAME_EVENT,
                'total_emits': self._total_emits,
                'total_rejected': self._total_rejected,
                'total_handler_failures': self._total_failures,
                'dead_letter_count': len(self._dead_letters),
                'pending_bridge_tasks': len(self._pending_tasks),
            }

    async def _invoke_one(self, event_name: str, plugin_name: str, handler: Callable, payload):
        """执行单个订阅者（async 直接 await，sync 转线程），异常仅记录不抛出。

        第 6 轮：异常同时进有界 DLQ（poison message 隔离），但保持
        「不让一个订阅者中断其它订阅者」的原有语义不变。
        """
        try:
            if asyncio.iscoroutinefunction(handler):
                return await handler(payload)
            return await asyncio.to_thread(handler, payload)
        except Exception as e:
            logger.error(f"事件处理异常: [{plugin_name}] {event_name} - {e}")
            self._total_failures += 1
            self._dead_letter(event_name, payload, 'handler_exception',
                              _CAUSAL_VAR.get(), plugin_name,
                              f'{type(e).__name__}: {e}')
            return None

    async def aemit(self, event_name: str, payload: dict = None, parallel: bool = False,
                    allow: Callable = None) -> bool:
        """
        异步发布事件，通知所有订阅者
        返回 bool：任一订阅 handler 返回 True 视为"已处理"（调用方可用于路由终止判断）
        向后兼容：现有订阅者返回 None/False 不影响

        :param parallel: True 时并发执行各订阅者（asyncio.gather），
            互不依赖的监听型事件可显著降低总耗时；默认 False 保持串行保序语义。
        :param allow: 可选订阅者过滤器 ``plugin_name -> bool``；返回 False 的
            订阅者本次不执行。用于多平台接入的「适配器↔插件绑定」——插件声明
            只服务某些适配器时，不应收到其它适配器广播的消息。不传=全量（原行为）。
        """
        # ── 因果链准入：环 / 深度 / 单 trace 预算，全部在派发前判定 ──
        scope, prior = _child_scope(_CAUSAL_VAR.get(), event_name)
        self._total_emits += 1
        reject = None
        if prior + 1 > _MAX_SAME_EVENT:
            reject = ('causal_loop_detected',
                      f"事件 {event_name!r} 在同一因果链上第 {prior + 1} 次出现")
        elif scope['depth'] > _MAX_CAUSAL_DEPTH:
            reject = ('causal_depth_exceeded',
                      f"因果深度 {scope['depth']} > {_MAX_CAUSAL_DEPTH}")
        elif scope['event_count'] > _MAX_TRACE_EVENTS:
            reject = ('trace_event_budget_exceeded',
                      f"单 trace 事件数 {scope['event_count']} > {_MAX_TRACE_EVENTS}")
        if reject is not None:
            self._total_rejected += 1
            self._dead_letter(event_name, payload, reject[0], scope, detail=reject[1])
            logger.warning(
                "事件风暴保护：拒绝派发 %s（trace=%s depth=%s）: %s",
                event_name, scope['trace_id'], scope['depth'], reject[1])
            return False

        token = _CAUSAL_VAR.set(scope)
        try:
            subscribers = self._snapshot(event_name)
            if allow is not None:
                subscribers = tuple(s for s in subscribers if allow(s[0]))
            if not subscribers:
                return False

            payload = payload or {}
            logger.debug("事件触发: %s → %s 个订阅者 (trace=%s depth=%s)",
                         event_name, len(subscribers),
                         scope['trace_id'], scope['depth'])

            if parallel and len(subscribers) > 1:
                results = await asyncio.gather(*[
                    self._invoke_one(event_name, plugin_name, handler, payload)
                    for plugin_name, handler in subscribers
                ])
                return any(r is True for r in results)

            handled = False
            for plugin_name, handler in subscribers:
                result = await self._invoke_one(
                    event_name, plugin_name, handler, payload)
                if result is True:
                    handled = True
            return handled
        finally:
            _CAUSAL_VAR.reset(token)

    def emit(self, event_name: str, payload: dict = None):
        """
        同步桥接发布事件
        - 主事件循环内调用 → fire-and-forget 调度
        - 其他线程调用 → 用临时事件循环执行
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            try:
                # Python 官方文档明确要求保存 task 引用，否则任务可能被 GC
                # 中途回收；原来的裸 create_task 没有引用。
                task = loop.create_task(self.aemit(event_name, payload))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
                return
            except RuntimeError:
                pass
        asyncio.run(self.aemit(event_name, payload))
