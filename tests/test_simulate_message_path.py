"""回归：模拟消息与 Web 聊天必须调用 Framework 上真实存在的 dispatch_event。

1.28.11 曾写成 `framework.dispatch.dispatch_event(...)`，Framework 并无 `dispatch`
子对象，工具一调用就 AttributeError。原测试只跑没有 loop 的分支，覆盖了不到这里。

用假事件循环 + 假 coroutine 捕获调用点，验证注入路径真的可达、且用的是正确的方法名。
"""
import importlib.util
import os
import sys
import types
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_aiwriter_main():
    path = os.path.join(ROOT, 'core_plugins', 'aiwriter', 'main.py')
    spec = importlib.util.spec_from_file_location('_aiwriter_main_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeLoop:
    def is_closed(self):
        return False


def test_host_bridge_uses_framework_dispatch_event():
    mod = _load_aiwriter_main()
    called = {}

    class FakeFW:
        loop = _FakeLoop()

        async def dispatch_event(self, raw, wait=False):
            called['raw'] = raw
            called['wait'] = wait
            return 'handled'

    bridge = mod._HostBridge(FakeFW())

    class Done:
        def result(self, timeout=None):
            return 'handled'

    def fake_run(coro, loop):
        # 协程体只有被 await 才会执行；这里驱动它，确保调用真的走到 dispatch_event。
        import asyncio

        asyncio.run(_await_and_close(coro))
        return Done()

    async def _await_and_close(coro):
        try:
            return await coro
        finally:
            coro.close()

    import asyncio

    with patch.object(asyncio, 'run_coroutine_threadsafe', side_effect=fake_run):
        out = bridge.simulate_message('hello', 123, 456)

    assert called['raw']['post_type'] == 'message'
    assert called['raw']['user_id'] == 123
    assert called['raw']['group_id'] == 456
    assert called['wait'] is True
    assert '已注入模拟消息' in out


def test_webchat_uses_framework_dispatch_event():
    from framework.api import webchat

    captured = {}

    class FakeFW:
        loop = _FakeLoop()

        async def dispatch_event(self, raw, wait=False):
            captured['raw'] = raw
            captured['wait'] = wait
            return 'ok'

    class FakeDB:
        def execute(self, sql, params=None):
            return 1

    class FakeHooks:
        def register(self, *a, **k):
            return None

    ctx = types.SimpleNamespace(
        app=types.SimpleNamespace(route=lambda *a, **k: (lambda fn: fn)),
        db=FakeDB(), framework=FakeFW(), require_auth=lambda fn: fn,
        hooks=FakeHooks())

    # 只验证模块内调用点：直接取注册函数源码里的表达式做静态断言，
    # 避免为一个 Flask 闭包伪造完整 app test_client。
    webchat.register(ctx)
    src = open(os.path.join(ROOT, 'framework', 'api', 'webchat.py'),
               encoding='utf-8').read()
    assert 'framework.dispatch.dispatch_event' not in src
    assert 'framework.dispatch_event(raw, wait=True)' in src
