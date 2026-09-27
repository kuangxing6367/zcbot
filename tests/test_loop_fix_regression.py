# -*- coding: utf-8 -*-
"""loop 误用修复的回归测试（T6）

覆盖三类修复：
1. http_inject / ws_client / discord / telegram 的 unregister()：
   不再在任意线程 asyncio.get_event_loop()（3.14 下直接抛 RuntimeError），
   统一取 framework.loop + run_coroutine_threadsafe 派发停服。
2. 三处「loop 未就绪重试」（ws_client / discord / telegram）：
   原 asyncio.get_event_loop().call_soon() 在无事件循环线程里抛错且被
   except: pass 吞掉，重试静默死亡；现改为 threading.Timer(0.5, ...)
   守护线程延后重试，带 _closing 守卫，停机后不再起定时器。
3. session 的 SessionManager.on_raw_message 异步过滤 handler：
   原 run_until_complete 在工作线程（无循环）必炸后被吞成 consume=True，
   过滤条件从未真正执行；现改为 run_coroutine_threadsafe(...).result()。
"""
import asyncio
import threading
import time

import pytest


# ── 工具：后台事件循环线程 ──────────────────────────────

def _start_bg_loop():
    """在守护线程中启动一个真实事件循环，返回 (loop, thread, ready_event)。"""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    t = threading.Thread(
        target=lambda: _run_bg(loop, ready), daemon=True, name='bg-loop')
    t.start()
    assert ready.wait(3), '后台事件循环未就绪'
    return loop, t


def _run_bg(loop, ready):
    asyncio.set_event_loop(loop)
    ready.set()
    loop.run_forever()


def _stop_bg_loop(loop, thread):
    if loop is not None:
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
    if thread is not None:
        thread.join(timeout=3)


def _make_future_in_loop(loop):
    """在工作线程中安全的在目标 loop 内创建 future。"""
    box = {}
    ev = threading.Event()

    def _mk():
        box['fut'] = loop.create_future()
        ev.set()

    loop.call_soon_threadsafe(_mk)
    assert ev.wait(3), 'future 创建超时'
    return box['fut']


class _StubFw:
    """只提供 loop 的 framework 桩。"""

    def __init__(self, loop=None):
        self.loop = loop


class _StubAdapter:
    """记录 stop 是否真正被执行。"""

    def __init__(self, maybe_loop):
        self.framework = _StubFw(maybe_loop)
        self.stopped = threading.Event()

    async def stop(self):
        self.stopped.set()


# ── 1. 四个插件的 unregister() ────────────────────────

@pytest.mark.parametrize('mod_name', [
    'http_inject', 'ws_client', 'discord', 'telegram',
])
def test_unregister_dispatch_to_framework_loop(mod_name):
    """unregister 在无 running loop 的线程里不抛错，且 stop() 派发回主循环真实执行。"""
    import importlib
    mod = importlib.import_module(f'core_plugins.{mod_name}.main')

    loop, thread = _start_bg_loop()
    try:
        stub = _StubAdapter(loop)
        saved = getattr(mod, '_adapter_instance', None)
        try:
            mod._adapter_instance = stub
            # 测试主体线程此刻没有 running loop：旧实现（get_event_loop +
            # loop.create_task）在此直接抛 RuntimeError；新实现必须静默成功
            mod.unregister()
            assert mod._adapter_instance is None, 'unregister 后应清空实例'
            # stop() 是经 run_coroutine_threadsafe 派发的协程，应在后台 loop 执行
            assert stub.stopped.wait(3), ('stop() 未在 framework.loop 上执行'
                                          f'（{mod_name} 的 unregister 派发失效）')
        finally:
            mod._adapter_instance = saved
    finally:
        _stop_bg_loop(loop, thread)


# ── 2. 三处「loop 未就绪重试」的守护定时器 ──────────────

@pytest.mark.parametrize('mod_name, method_name', [
    ('ws_client', '_schedule_connect'),
    ('discord', '_schedule_connect'),
    ('telegram', '_reschedule'),
])
def test_retry_no_running_loop_no_crash_and_stops_after_closing(
        mod_name, method_name, monkeypatch):
    """loop 未就绪时重试不抛错；_closing 后不再起新定时器（原实现静默死掉/无限重试）。"""
    import importlib
    mod = importlib.import_module(f'core_plugins.{mod_name}.main')
    inst = _make_adapter_instance(mod)

    started = []
    real_timer = threading.Timer

    def fake_timer(*a, **k):
        t = real_timer(*a, **k)
        started.append(t)
        return t

    monkeypatch.setattr(threading, 'Timer', fake_timer)
    monkeypatch.setattr(mod.threading, 'Timer', fake_timer)

    # 第一次调用应创建守护定时器，且不抛 RuntimeError
    getattr(inst, method_name)()
    assert started, f'{mod_name}.{method_name} 未创建重试定时器'
    created = started[-1]
    assert created.daemon is True, (
        f'{mod_name} 重试定时器必须 daemon=True，否则 loop 永不就绪时会'
        '阻塞解释器退出（CI pytest 超时）')
    assert created.interval == 0.5

    # _closing=True 后再次调用不应再创建新定时器（停机守卫）
    inst._closing = True
    before = len(started)
    getattr(inst, method_name)()
    assert len(started) == before, (
        f'{mod_name} _closing 后仍在创建重试定时器')


_ADAPTER_CLASSES = {
    'ws_client': ('WsClientAdapter', '_supervisor'),
    'discord': ('DiscordAdapter', '_supervisor'),
    'telegram': ('TelegramAdapter', '_task'),
}


def _make_adapter_instance(mod):
    """按模块构造只带重试路径所需属性的裸适配器实例。"""
    key = mod.__name__.split('.')[-2]
    cls_name, task_attr = _ADAPTER_CLASSES[key]
    cls = getattr(mod, cls_name)
    inst = object.__new__(cls)
    inst.framework = _StubFw(None)
    inst._closing = False
    setattr(inst, task_attr, None)
    return inst


@pytest.mark.parametrize('mod_name, method_name', [
    ('ws_client', 'start'),
    ('discord', 'start'),
    ('telegram', 'start'),
])
def test_start_without_loop_uses_daemon_retry(mod_name, method_name, monkeypatch):
    """start() 在 loop 未就绪时同样走守护定时器重试，不抛、不卡进程。"""
    import importlib
    mod = importlib.import_module(f'core_plugins.{mod_name}.main')
    inst = _make_adapter_instance(mod)

    started = []
    real_timer = threading.Timer

    def fake_timer(*a, **k):
        t = real_timer(*a, **k)
        started.append(t)
        return t

    monkeypatch.setattr(threading, 'Timer', fake_timer)
    monkeypatch.setattr(mod.threading, 'Timer', fake_timer)

    getattr(inst, method_name)()
    assert started and started[-1].daemon is True
    assert started[-1].interval == 0.5


# ── 3. session 异步过滤 handler 真实执行 ────────────────

def test_session_async_filter_handler_really_executes():
    """异步过滤 handler 必须真正运行并决定是否消费，不得被吞成 consume=True。"""
    from core_plugins.session.main import SessionManager

    loop, thread = _start_bg_loop()
    try:
        mgr = SessionManager(_StubFw(loop))
        key = '1001:2002'
        raw = {'user_id': 1001, 'group_id': 2002, 'message': 'hi'}

        # 场景 A：异步 handler 返回 False → 过滤不通过，不消费
        calls = []

        async def reject_handler(ev):
            calls.append(ev)
            return False

        fut = _make_future_in_loop(loop)
        with mgr._lock:
            mgr._sessions[key] = {
                'future': fut, 'expires': time.time() + 60,
                'handler': reject_handler,
            }

        # on_raw_message 由框架在工作线程（本测试主线程）调用
        consumed = mgr.on_raw_message(raw, 'bot')
        assert calls, '异步过滤 handler 从未被执行（修复失效）'
        assert calls[0]['message'] == 'hi', 'handler 收到的不是原始事件'
        assert consumed is False, 'handler 返回 False 却消费了消息'
        assert not fut.done(), '过滤未通过时 future 不应被 set_result'

        # 场景 B：异步 handler 返回 True → 消费并投递到 future
        calls2 = []

        async def accept_handler(ev):
            calls2.append(ev)
            return True

        fut2 = _make_future_in_loop(loop)
        with mgr._lock:
            mgr._sessions[key] = {
                'future': fut2, 'expires': time.time() + 60,
                'handler': accept_handler,
            }

        consumed2 = mgr.on_raw_message(raw, 'bot')
        assert calls2, '异步 accept handler 从未被执行'
        assert consumed2 is True, 'handler 返回 True 却未消费'
        wait_t0 = time.time()
        while not fut2.done() and time.time() - wait_t0 < 3:
            time.sleep(0.02)
        assert fut2.done(), '通过过滤后 future 未完成'
        assert fut2.result() == raw, 'future 结果不是原始事件'

        # 场景 C：同步 handler 不受影响
        calls3 = []

        def sync_handler(ev):
            calls3.append(ev)
            return False

        fut3 = _make_future_in_loop(loop)
        with mgr._lock:
            mgr._sessions[key] = {
                'future': fut3, 'expires': time.time() + 60,
                'handler': sync_handler,
            }
        consumed3 = mgr.on_raw_message(raw, 'bot')
        assert calls3 and consumed3 is False and not fut3.done()
    finally:
        _stop_bg_loop(loop, thread)