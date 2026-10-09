# -*- coding: utf-8 -*-
"""
任务2（拆分核心架构债务）聚焦回归测试
======================================
覆盖：
1. 调度器单一权威实现 / 旧导入路径兼容：
   framework.scheduler.TaskScheduler 与 core_plugins.scheduler.main.TaskScheduler
   指向同一个类；旧调用面（pause/resume/remove/get_jobs/_scheduler/_plugin_tasks）保留；
   官方插件 register/unregister 语义（服务注册 / 禁用注册 None / 重载停旧实例）不变。
2. ServiceRegistry.call_when_ready / cancel_ready：
   立即触发 / 迟到触发 / 显式禁用（register None）/ 每句柄只触发一次 /
   取消后不再触发 / 回调异常隔离。
3. session 官方插件：清理任务经「调度器服务就绪」回调注册且恰好一次；
   停用后无残留（挂起回调 / 已注册任务 / 原始消息处理器 / 服务注册）；
   不再使用固定延迟定时器。
4. LogCoalescer：stop() 立即唤醒并有限等待后台线程退出（幂等）；合并去重行为不变。
5. CoreRuntime / core_services 装配面：RPC 方法名与抽取前完全一致、
   db/api/tx/route/log 行为契约不变、远程 stub 视图 Flask 契约不变、
   CoreRuntime 同名薄委托方法保留。
6. 单进程路径：导入调度器/会话栈不加载 framework.ipc（子解释器验证）；
   core_services 模块级仅标准库（flask / framework.api / framework.log_broker 延迟导入）。

运行：
    python -m pytest tests/test_task2_arch_split.py -v
    python tests/test_task2_arch_split.py
"""
import asyncio
import logging
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── 公共桩 ──────────────────────────────────────────────────────────────

class _Ctx:
    """插件 register(ctx) 的最小桩"""

    def __init__(self, fw):
        self._framework = fw
        self.logs = []

    def log(self, msg, level='info'):
        self.logs.append((level, msg))


class _FakeFramework:
    """scheduler / session 官方插件 register/unregister 所需的最小框架面"""

    def __init__(self, config=None):
        from framework.messaging.protocol import ServiceRegistry
        self.config = config or {}
        self.services = ServiceRegistry()
        self.loop = None
        self.plugin_loader = None
        self.db = None
        self._raw_handlers = {}

    def register_raw_message_handler(self, name, handler, priority=50):
        self._raw_handlers[name] = handler

    def unregister_raw_message_handlers(self, name):
        self._raw_handlers.pop(name, None)


class _StubScheduler:
    """记录任务注册/移除的调度器桩（验证 session 就绪回调行为）"""

    def __init__(self):
        self.tasks = []

    def add_plugin_task(self, task):
        self.tasks.append(dict(task))

    def remove_plugin_tasks(self, plugin_name):
        self.tasks = [t for t in self.tasks
                      if t.get('plugin_name') != plugin_name]


class _StubServer:
    """记录 register()/on() 的 IPC 服务端桩（验证装配面）"""

    def __init__(self):
        self.methods = {}
        self.events = {}

    def register(self, method, handler):
        self.methods[method] = handler

    def on(self, channel, handler):
        self.events.setdefault(channel, []).append(handler)


class _StubDb:
    def __init__(self):
        self.calls = []

    def query(self, sql, params=None):
        self.calls.append(('query', sql, params))
        return [{'ok': True}]

    def scalar(self, sql, params=None):
        self.calls.append(('scalar', sql, params))
        return 42


class _StubTx:
    def __init__(self):
        self.calls = []

    def begin(self):
        self.calls.append(('begin',))
        return 'tx-1'

    def run(self, tx_id, op, sql, params=None):
        self.calls.append(('run', tx_id, op, sql, params))
        return [['row']]

    def commit(self, tx_id):
        self.calls.append(('commit', tx_id))

    def rollback(self, tx_id):
        self.calls.append(('rollback', tx_id))


class _StubCaller:
    def __init__(self):
        self.calls = []

    async def acall(self, action, bot=None, **params):
        self.calls.append((action, bot, params))
        return {'status': 'ok', 'action': action}


class _StubAdapter:
    def get_connected_bots(self):
        return ['bot-1', 'bot-2']

    async def send_text(self, text, user_id=None, group_id=None, source=None):
        return {'status': 'ok', 'to': user_id or group_id, 'text': text}


def _reset_scheduler_plugin():
    import core_plugins.scheduler.main as m
    if getattr(m, '_scheduler', None) is not None:
        try:
            m._scheduler.stop()
        except Exception:
            pass
    m._scheduler = None
    m._fw = None


def _reset_session_plugin():
    import core_plugins.session.main as m
    m._manager = None
    m._fw = None
    m._ready_handle = None


def _strip_comment_lines(src: str) -> str:
    return '\n'.join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith('#'))


# ── 1. 调度器单一权威实现 / 旧导入路径 ──────────────────────────────────

def test_scheduler_old_import_paths_same_class():
    """旧导入路径必须指向同一权威实现（framework.scheduler 与官方插件薄封装）"""
    from framework.scheduler import TaskScheduler as fw_impl
    from core_plugins.scheduler.main import TaskScheduler as plugin_impl
    assert fw_impl is plugin_impl, "旧导入路径必须指向同一个 TaskScheduler"


def test_scheduler_public_surface_compat():
    """权威实现必须保留旧调用面（framework.api.tasks / loader / runtime 依赖）"""
    from framework.scheduler import TaskScheduler
    for attr in ('start', 'stop', 'add_plugin_task', 'remove_plugin_tasks',
                 'remove_task', 'get_jobs', 'pause_task', 'resume_task'):
        assert hasattr(TaskScheduler, attr), f"权威实现缺少旧调用面: {attr}"
    # 实例属性（framework.core.runtime._register_builtin_jobs / loader 自检依赖）
    sched = TaskScheduler(_FakeFramework())
    assert hasattr(sched, '_scheduler') and hasattr(sched, '_plugin_tasks')
    assert not sched._scheduler.running
    assert sched._plugin_tasks == {}


def test_scheduler_plugin_meta_process_tag_preserved():
    """官方插件加载语义不变：process 标记必须仍是 host（宿主侧加载）"""
    import core_plugins.scheduler.main as m
    assert m.__plugin_meta__['process'] == 'host'
    assert m.__plugin_meta__['priority'] == 0


def test_scheduler_plugin_register_unregister():
    """register：注册 services['scheduler'] 并启动；unregister：停调度器 + 摘服务"""
    import core_plugins.scheduler.main as m
    from framework.scheduler import TaskScheduler
    _reset_scheduler_plugin()
    try:
        fw = _FakeFramework()
        m.register(_Ctx(fw))
        sched = fw.services.get('scheduler')
        assert isinstance(sched, TaskScheduler)
        assert fw.services.has('scheduler')
        m.unregister()
        assert not fw.services.has('scheduler'), "停用后服务注册必须同步摘除"
        assert sched._stopped, "停用后调度器必须处于停止态"
    finally:
        _reset_scheduler_plugin()


def test_scheduler_plugin_disabled_registers_none():
    """scheduler.enabled=false：显式注册 None（等待方据此告警跳过而非挂死）"""
    import core_plugins.scheduler.main as m
    _reset_scheduler_plugin()
    try:
        fw = _FakeFramework({'scheduler': {'enabled': False}})
        m.register(_Ctx(fw))
        assert fw.services.has('scheduler')
        assert fw.services.get('scheduler') is None
        assert m._scheduler is None
        m.unregister()
        assert not fw.services.has('scheduler')
    finally:
        _reset_scheduler_plugin()


def test_scheduler_plugin_rereg_stops_previous():
    """插件重载（重复 register）必须先停旧实例，不残留等待线程/调度器"""
    import core_plugins.scheduler.main as m
    _reset_scheduler_plugin()
    try:
        fw = _FakeFramework()
        m.register(_Ctx(fw))
        old = fw.services.get('scheduler')
        m.register(_Ctx(fw))
        new = fw.services.get('scheduler')
        assert new is not old
        assert old._stopped, "重复 register 必须先停旧实例"
        m.unregister()
        assert new._stopped
    finally:
        _reset_scheduler_plugin()


# ── 2. ServiceRegistry 服务就绪回调 ─────────────────────────────────────

def _registry():
    from framework.messaging.protocol import ServiceRegistry
    return ServiceRegistry()


def test_service_registry_import_paths():
    """旧导入路径不变：protocol 直接导入与 messaging 包再导出同一对象"""
    from framework.messaging.protocol import ServiceRegistry as a
    from framework.messaging import ServiceRegistry as b
    assert a is b


def test_call_when_ready_immediate_fire():
    reg = _registry()
    svc = object()
    reg.register('scheduler', svc)
    calls = []
    h = reg.call_when_ready('scheduler', calls.append)
    assert h['state'] == 'fired'
    assert calls == [svc]


def test_call_when_ready_later_register_fires_exactly_once():
    """迟到注册触发一次；同名服务覆盖注册不得重复触发（一次性句柄）"""
    reg = _registry()
    calls = []
    h = reg.call_when_ready('scheduler', calls.append)
    assert h['state'] == 'pending'
    svc = object()
    reg.register('scheduler', svc)
    assert h['state'] == 'fired'
    assert calls == [svc]
    reg.register('scheduler', object())
    assert calls == [svc], "已触发句柄不得因服务覆盖注册而二次触发"


def test_call_when_ready_none_service_fires():
    """显式禁用 register(None) 也触发——等待方收到 None 告警跳过，而非无限挂起"""
    reg = _registry()
    calls = []
    reg.call_when_ready('scheduler', calls.append)
    reg.register('scheduler', None)
    assert calls == [None]


def test_cancel_ready_prevents_late_fire():
    """取消后迟到注册不得触发；内部挂起表不泄漏；重复取消返回 False"""
    reg = _registry()
    calls = []
    h = reg.call_when_ready('scheduler', calls.append)
    assert reg.cancel_ready(h) is True
    assert h['state'] == 'cancelled'
    reg.register('scheduler', object())
    assert calls == [], "取消后的回调不得被迟到注册触发"
    assert reg.cancel_ready(h) is False
    assert 'scheduler' not in reg._ready_callbacks


def test_ready_callback_exception_isolated():
    """回调异常不得外抛、不得影响注册流程"""
    reg = _registry()

    def _boom(svc):
        raise RuntimeError('boom')

    # ERROR 记录会经 root 落到其它测试遗留的日志合并器桶里，会话结束后再补发
    # 汇总（closed stream 噪音）。本测试只验证异常隔离，期间静音日志即可。
    logging.disable(logging.CRITICAL)
    try:
        reg.call_when_ready('scheduler', _boom)
        reg.register('scheduler', object())
        assert reg.has('scheduler')
    finally:
        logging.disable(logging.NOTSET)


def test_call_when_ready_rejects_non_callable():
    reg = _registry()
    try:
        reg.call_when_ready('scheduler', 'not-callable')
    except TypeError:
        pass
    else:
        raise AssertionError('非可调用回调必须抛 TypeError')


# ── 3. session 官方插件：就绪注册恰好一次 + 停用无残留 ──────────────────

def test_session_cleanup_task_via_ready_callback_once():
    """调度器后注册：清理任务经就绪回调注册恰好一次；调度器覆盖注册不重复触发"""
    import core_plugins.session.main as m
    _reset_session_plugin()
    try:
        fw = _FakeFramework()
        m.register(_Ctx(fw))
        assert fw.services.get('session_manager') is not None
        assert m._ready_handle is not None and m._ready_handle['state'] == 'pending'
        stub = _StubScheduler()
        fw.services.register('scheduler', stub)
        tasks = [t for t in stub.tasks if t.get('plugin_name') == 'session']
        assert len(tasks) == 1, "就绪回调必须注册恰好一个清理任务"
        assert tasks[0]['handler_name'] == '_cleanup_task'
        assert tasks[0]['cron_expression'] == '*/5 * * * *'
        assert m._ready_handle['state'] == 'fired'
        # 调度器覆盖注册：一次性句柄已消费，不得再补挂任务
        fw.services.register('scheduler', _StubScheduler())
        assert len([t for t in stub.tasks
                    if t.get('plugin_name') == 'session']) == 1
    finally:
        _reset_session_plugin()


def test_session_cleanup_task_immediate_when_scheduler_ready():
    """调度器先注册：session register 时立即注册清理任务（同一回调机制）"""
    import core_plugins.session.main as m
    _reset_session_plugin()
    try:
        fw = _FakeFramework()
        stub = _StubScheduler()
        fw.services.register('scheduler', stub)
        m.register(_Ctx(fw))
        tasks = [t for t in stub.tasks if t.get('plugin_name') == 'session']
        assert len(tasks) == 1
        assert m._ready_handle is not None and m._ready_handle['state'] == 'fired'
    finally:
        _reset_session_plugin()


def test_session_unregister_removes_registered_task():
    """调度器已就绪场景：停用后清理任务/原始消息处理器/服务注册全部摘除"""
    import core_plugins.session.main as m
    _reset_session_plugin()
    try:
        fw = _FakeFramework()
        stub = _StubScheduler()
        fw.services.register('scheduler', stub)
        m.register(_Ctx(fw))
        assert len([t for t in stub.tasks
                    if t.get('plugin_name') == 'session']) == 1
        assert 'session_manager' in fw._raw_handlers
        m.unregister()
        assert stub.tasks == [], "停用后已注册清理任务必须移除"
        assert not fw.services.has('session_manager')
        assert 'session_manager' not in fw._raw_handlers
        assert m._ready_handle is None and m._manager is None and m._fw is None
    finally:
        _reset_session_plugin()


def test_session_unregister_before_scheduler_ready_no_residue():
    """调度器尚未就绪时停用：挂起回调被取消，事后调度器注册不得迟到补任务
    （旧固定延迟定时器实现的竞态：停用后仍会迟到注册清理任务）"""
    import core_plugins.session.main as m
    _reset_session_plugin()
    try:
        fw = _FakeFramework()
        m.register(_Ctx(fw))
        assert m._ready_handle is not None and m._ready_handle['state'] == 'pending'
        m.unregister()
        assert m._ready_handle is None
        stub = _StubScheduler()
        fw.services.register('scheduler', stub)
        assert stub.tasks == [], "停用后迟到的调度器注册不得补挂清理任务"
        assert not fw.services.has('session_manager')
        assert 'session_manager' not in fw._raw_handlers
    finally:
        _reset_session_plugin()


def test_session_disabled_registers_none():
    import core_plugins.session.main as m
    _reset_session_plugin()
    try:
        fw = _FakeFramework({'session': {'enabled': False}})
        m.register(_Ctx(fw))
        assert fw.services.has('session_manager')
        assert fw.services.get('session_manager') is None
        assert m._ready_handle is None, "禁用路径不得登记就绪回调"
        assert not fw._raw_handlers
        assert not fw.services._ready_callbacks
        m.unregister()
        assert not fw.services.has('session_manager')
    finally:
        _reset_session_plugin()


def test_session_no_timer_based_registration():
    """源级防回归：session 不得再使用固定延迟定时器（Timer）注册清理任务"""
    import inspect
    import core_plugins.session.main as m
    code = _strip_comment_lines(inspect.getsource(m))
    assert 'Timer' not in code


# ── 4. LogCoalescer 停止流程与去重行为 ──────────────────────────────────

def test_log_coalescer_stop_wakes_promptly():
    """stop() 置信号后必须立即唤醒 flush 线程并有限等待退出（旧实现滞留整窗口）"""
    from framework.coalesce_log import LogCoalescer
    c = LogCoalescer(window=30.0)
    t0 = time.monotonic()
    ok = c.stop(timeout=3.0)
    dt = time.monotonic() - t0
    assert ok is True
    assert not c._timer.is_alive()
    assert dt < 2.0, f"stop() 应在唤醒后立即返回，实际耗时 {dt:.2f}s"
    # 幂等：重复调用仍返回已退出
    assert c.stop(timeout=1.0) is True


def _rec(msg, level=logging.WARNING, name='t.coalesce'):
    return logging.LogRecord(name=name, level=level, pathname='', lineno=0,
                             msg=msg, args=(), exc_info=None)


def test_log_coalescer_dedup_unchanged():
    """合并去重行为不变：首条放行 / 窗口内同类合并（数字归一化）/ 低级别不合并 /
    同 record 多 handler 决策一致"""
    from framework.coalesce_log import LogCoalescer
    c = LogCoalescer(window=60.0)
    try:
        r1 = _rec('用户 123 连接超时')
        r2 = _rec('用户 456 连接超时')
        r3 = _rec('用户 789 连接超时')
        assert c.filter(r1) is True
        assert c.filter(r2) is False
        assert c.filter(r3) is False
        assert c.filter(r2) is False, "同 record 二次过滤必须返回缓存决策"
        assert c.filter(_rec('普通消息', level=logging.INFO)) is True
    finally:
        assert c.stop() is True


# ── 5. CoreRuntime / core_services 装配面 ───────────────────────────────

_DB_METHODS = ('query', 'query_one', 'execute', 'execute_many', 'insert',
               'scalar', 'exists', 'count', 'table_exists', 'table_info',
               'table_has_column', 'pool_status')

_EXPECTED_METHODS = ({f'db.{n}' for n in _DB_METHODS}
                     | {'api.call', 'api.send_text', 'bots.list',
                        'route.register',
                        'tx.begin', 'tx.run', 'tx.commit', 'tx.rollback'})


def _wire_all(srv, fw, tx):
    from framework.ipc.core_services import (
        register_core_handlers, register_host_log_relay,
        register_remote_route_handler, register_tx_handlers)
    register_core_handlers(srv, fw)
    register_remote_route_handler(srv, fw)
    register_tx_handlers(srv, tx)
    register_host_log_relay(srv)


def test_core_services_rpc_surface_unchanged():
    """装配面防回归：RPC 方法名集合与抽取前完全一致；日志桥接只占 'log' 通道"""
    srv = _StubServer()
    _wire_all(srv, _FakeFramework(), _StubTx())
    assert set(srv.methods) == _EXPECTED_METHODS, \
        f"RPC 面差异: {sorted(set(srv.methods) ^ _EXPECTED_METHODS)}"
    assert set(srv.events) == {'log'}


def test_core_services_db_roundtrip():
    """db.* 经 to_thread 转发到真实 Database，参数/返回值不变"""
    from framework.ipc.core_services import register_core_handlers
    fw = _FakeFramework()
    fw.db = _StubDb()
    srv = _StubServer()
    register_core_handlers(srv, fw)
    out = asyncio.run(srv.methods['db.query'](sql='SELECT 1', params=None))
    assert out == [{'ok': True}]
    assert fw.db.calls == [('query', 'SELECT 1', None)]
    out = asyncio.run(srv.methods['db.scalar'](sql='SELECT COUNT(*)'))
    assert out == 42


def test_core_services_api_send_text_bots_list():
    from framework.ipc.core_services import register_core_handlers
    fw = _FakeFramework()
    srv = _StubServer()
    register_core_handlers(srv, fw)
    # 无 api_caller：RuntimeError（核心进程无协议适配器）
    try:
        asyncio.run(srv.methods['api.call'](action='send_msg', params={}))
    except RuntimeError:
        pass
    else:
        raise AssertionError('无 api_caller 时 api.call 必须抛 RuntimeError')
    # 注入后转发到 acall
    caller = _StubCaller()
    fw.services.register('api_caller', caller)
    out = asyncio.run(srv.methods['api.call'](
        action='send_msg', bot='bot-1', params={'message': 'hi'}))
    assert out['status'] == 'ok' and out['action'] == 'send_msg'
    assert caller.calls == [('send_msg', 'bot-1', {'message': 'hi'})]
    # bots.list：无 protocol_adapter → []；有 → 透传
    assert srv.methods['bots.list']() == []
    fw.services.register('protocol_adapter', _StubAdapter())
    assert srv.methods['bots.list']() == ['bot-1', 'bot-2']
    # api.send_text → protocol_adapter.send_text
    out = asyncio.run(srv.methods['api.send_text'](text='hi', user_id=123))
    assert out == {'status': 'ok', 'to': 123, 'text': 'hi'}
    # 无 protocol_adapter 时 send_text 也必须明确报错
    fw.services.remove('protocol_adapter')
    try:
        asyncio.run(srv.methods['api.send_text'](text='hi', user_id=123))
    except RuntimeError:
        pass
    else:
        raise AssertionError('无 protocol_adapter 时 api.send_text 必须抛 RuntimeError')


def test_core_services_tx_roundtrip():
    from framework.ipc.core_services import register_tx_handlers
    tx = _StubTx()
    srv = _StubServer()
    register_tx_handlers(srv, tx)
    assert asyncio.run(srv.methods['tx.begin']()) == 'tx-1'
    rows = asyncio.run(srv.methods['tx.run'](
        tx_id='tx-1', op='query', sql='SELECT 1', params=None))
    assert rows == [['row']]
    asyncio.run(srv.methods['tx.commit'](tx_id='tx-1'))
    asyncio.run(srv.methods['tx.rollback'](tx_id='tx-1'))
    assert [c[0] for c in tx.calls] == ['begin', 'run', 'commit', 'rollback']


def test_core_services_route_register_without_web():
    """无 Web app 时 route.register 返回 {'ok': False, 'error': 'web 未启用'}（行为不变）"""
    from framework.ipc.core_services import register_remote_route_handler
    from framework.api import registry as api_registry
    srv = _StubServer()
    register_remote_route_handler(srv, _FakeFramework())
    prev = getattr(api_registry, '_web_app', None)
    api_registry._web_app = None
    try:
        out = srv.methods['route.register'](
            route_id='r1', path='/x', methods=['GET'], auth=False)
        assert out == {'ok': False, 'error': 'web 未启用'}
    finally:
        api_registry._web_app = prev


def test_host_log_relay_batch_compat():
    """宿主日志桥接兼容单条 dict（旧宿主）与批量 {'batch': [...]}（新宿主）；
    非法条目静默跳过、绝不外抛"""
    from framework.ipc.core_services import register_host_log_relay
    from framework.log_broker import log_broker
    recorded = []
    orig = log_broker.log
    log_broker.log = lambda *a, **k: recorded.append((a, k))
    try:
        srv = _StubServer()
        register_host_log_relay(srv)
        h = srv.events['log'][0]
        h({'level': 'WARN', 'msg': 'single', 'logger': 'host'})
        h({'batch': [{'level': 'INFO', 'msg': 'b1', 'logger': 'x'},
                     'skip-me']})
        h('garbage-not-dict')
    finally:
        log_broker.log = orig
    assert [r[0][2] for r in recorded] == ['single', 'b1']
    assert all(r[0][0] == 'host' for r in recorded)


def test_bind_ipc_dispatch():
    """事件分发替换：fw.dispatch_event → IPC 推送到宿主"""
    from framework.ipc.core_services import bind_ipc_dispatch

    class _Srv:
        def __init__(self):
            self.sent = []

        async def asend_event(self, event):
            self.sent.append(event)

    srv = _Srv()
    fw = _FakeFramework()
    fw.dispatch_event = None
    bind_ipc_dispatch(srv, fw)
    assert fw.dispatch_event is not None
    asyncio.run(fw.dispatch_event({'type': 'x'}))
    assert srv.sent == [{'type': 'x'}]


def test_core_runtime_keeps_delegating_methods():
    """CoreRuntime 保留同名装配方法（薄委托），方法名/行为兼容不变"""
    from framework.ipc.core_runtime import CoreRuntime
    rt = CoreRuntime()
    fw = _FakeFramework()
    fw.db = _StubDb()
    tx = _StubTx()
    rt.framework = fw
    rt.server = _StubServer()
    rt._tx = tx
    rt._register_core_handlers()
    rt._register_remote_route_handler()
    rt._register_tx_handlers()
    methods = set(rt.server.methods)
    assert {f'db.{n}' for n in _DB_METHODS} <= methods
    assert {'api.call', 'api.send_text', 'bots.list', 'route.register',
            'tx.begin', 'tx.run', 'tx.commit', 'tx.rollback'} <= methods
    assert callable(rt._make_remote_view('r-1'))


def test_make_remote_view_flask_contract():
    """远程 stub 视图契约：dict→200；(status,dict)→状态码；(status,dict,headers)→
    带响应头；IPC 异常→503 {'code': -1, 'error': ...}"""
    import flask
    from framework.ipc.core_services import make_remote_view

    class _Srv:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def request_host(self, method, params):
            self.calls.append((method, params))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def _client(result):
        srv = _Srv(result)
        app = flask.Flask('remote-view-test')
        app.add_url_rule('/remote', endpoint='_zcbot_remote_r1',
                         view_func=make_remote_view(srv, 'r1'),
                         methods=['GET', 'POST'])
        return srv, app.test_client()

    # dict → 200
    srv, cli = _client({'code': 0, 'data': 'ok'})
    resp = cli.get('/remote?x=1')
    assert resp.status_code == 200
    assert resp.get_json() == {'code': 0, 'data': 'ok'}
    method, outer = srv.calls[0]
    assert method == 'http.dispatch'
    assert outer['route_id'] == 'r1'
    params = outer['params']
    assert params['method'] == 'GET'
    assert params['path'] == '/remote'
    assert params['args'] == {'x': '1'}

    # (status, dict) → 指定状态码
    _, cli = _client((404, {'code': 404, 'error': 'not found'}))
    resp = cli.get('/remote')
    assert resp.status_code == 404
    assert resp.get_json() == {'code': 404, 'error': 'not found'}

    # (status, dict, headers) → 带自定义响应头
    _, cli = _client((201, {'code': 0}, {'X-Custom': 'zcbot'}))
    resp = cli.post('/remote', json={'a': 1})
    assert resp.status_code == 201
    assert resp.headers.get('X-Custom') == 'zcbot'

    # IPC 异常 → 503
    _, cli = _client(RuntimeError('宿主未连接'))
    resp = cli.get('/remote')
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['code'] == -1
    assert '远程路由执行失败' in body['error']


# ── 6. 单进程路径不加载 IPC 组件 ────────────────────────────────────────

def test_single_process_imports_do_not_load_ipc():
    """单进程路径导入调度器/会话栈不得加载 framework.ipc
    （子解释器验证，避免本测试进程的模块缓存干扰）"""
    code = (
        "import sys; sys.path.insert(0, {root!r}); "
        "import framework.scheduler; "
        "import core_plugins.scheduler.main; "
        "import core_plugins.session.main; "
        "import framework.messaging.protocol; "
        "import framework.coalesce_log; "
        "bad = [m for m in sys.modules "
        "if m == 'framework.ipc' or m.startswith('framework.ipc.')]; "
        "print(('IPC_LOADED:' + ','.join(bad)) if bad else 'IPC_CLEAN')"
    ).format(root=ROOT)
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
                          encoding='utf-8', errors='replace',
                          timeout=120, env=env, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert 'IPC_CLEAN' in proc.stdout, \
        f"单进程栈不应加载 IPC 组件: {proc.stdout!r} {proc.stderr!r}"


def test_core_services_module_level_imports_are_stdlib_only():
    """core_services 模块级只允许标准库（asyncio/logging）：
    flask / framework.api / framework.log_broker 必须函数内延迟导入"""
    import ast
    src_path = os.path.join(ROOT, 'framework', 'ipc', 'core_services.py')
    with open(src_path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    top_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top_imports.add(node.module or '')
    allowed = {'asyncio', 'logging'}
    assert top_imports <= allowed, \
        f"core_services 模块级导入超出白名单: {sorted(top_imports - allowed)}"


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
