"""适配器多实例（onebot.instances[]）回归。

核心约束：**默认零行为变化**。不配 instances 时，_build_instance_configs 返回
单份主配置，register 走和旧版完全一样的路径。配了 instances 时，每项继承主配置
并覆盖差异键（典型是 listen_port/access_token/bot_name_prefix），各实例独立端口、
独立 token，共享 ApiCaller（按 bot_name 路由，互不串）。

另外锁住 bot_name_prefix：两个实例连同一个 self_id 时，前缀让连接名不撞车。

运行：py -3 -m pytest tests/test_adapter_multi_instance.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import core_plugins.onebot_adapter.main as ob


@pytest.fixture(autouse=True)
def _restore_globals():
    """register/unregister 会改模块级实例全局，测后复原避免污染同进程其它测试。"""
    old_inst, old_all = ob._adapter_instance, ob._adapter_instances
    yield
    ob._adapter_instance, ob._adapter_instances = old_inst, old_all


# ── 配置展开 ──────────────────────────────────────────────────
def test_no_instances_returns_single_base():
    base = {'listen_port': 6830, 'access_token': 'tok', 'enabled': True}
    out = ob._build_instance_configs(base)
    assert out == [base]
    assert out[0] is not base  # 副本，避免实例改动污染主配置


def test_instances_inherit_base_and_override():
    base = {
        'listen_port': 6830, 'access_token': 'base-tok', 'enabled': True,
        'max_frame_size': 12345,
        'instances': [
            {'listen_port': 80, 'access_token': '1234', 'bot_name_prefix': 'a'},
            {'listen_port': 800, 'access_token': '5678', 'bot_name_prefix': 'b'},
        ],
    }
    out = ob._build_instance_configs(base)
    assert len(out) == 2
    # 每项继承主配置的全部键
    assert out[0]['max_frame_size'] == 12345
    assert out[1]['enabled'] is True
    # 差异键各自覆盖
    assert out[0]['listen_port'] == 80 and out[0]['access_token'] == '1234'
    assert out[1]['listen_port'] == 800 and out[1]['access_token'] == '5678'
    # instances 自身不泄漏进子配置
    assert all('instances' not in c for c in out)
    # 主配置未被改动
    assert base['listen_port'] == 6830 and len(base['instances']) == 2


def test_instance_can_override_any_base_key():
    base = {'listen_port': 6830, 'listen_host': '0.0.0.0', 'max_frame_size': 100,
            'instances': [{'listen_host': '127.0.0.1', 'max_frame_size': 999}]}
    out = ob._build_instance_configs(base)
    assert out[0]['listen_host'] == '127.0.0.1'
    assert out[0]['max_frame_size'] == 999
    assert out[0]['listen_port'] == 6830  # 未覆盖的保留


def test_empty_or_invalid_instances_falls_back():
    for bad in ([], None, 'oops', {}, [None, 42, 'x']):
        base = {'listen_port': 6830, 'instances': bad}
        out = ob._build_instance_configs(base)
        assert len(out) == 1 and out[0]['listen_port'] == 6830, bad


# ── 每实例独立 WS server 配置 ──────────────────────────────────
def test_ws_server_per_instance_port_and_token():
    from core_plugins.onebot_adapter.main import ApiCaller, OneBotWebSocketServer

    caller = ApiCaller()
    cfgs = ob._build_instance_configs({
        'listen_port': 6830, 'access_token': 'base',
        'instances': [
            {'listen_port': 80, 'access_token': '1234', 'bot_name_prefix': 'a'},
            {'listen_port': 800, 'access_token': '5678', 'bot_name_prefix': 'b'},
        ],
    })
    servers = [OneBotWebSocketServer(c, lambda *a: None, caller) for c in cfgs]
    assert [(s.port, s.access_token) for s in servers] == [(80, '1234'), (800, '5678')]
    assert [s.bot_name_prefix for s in servers] == ['a', 'b']
    # 共享 caller
    assert all(s.api_caller is caller for s in servers)


def test_ws_server_no_prefix_by_default():
    from core_plugins.onebot_adapter.main import ApiCaller, OneBotWebSocketServer

    s = OneBotWebSocketServer({'listen_port': 6830}, lambda *a: None, ApiCaller())
    assert s.bot_name_prefix == ''


# ── bot_name 前缀隔离 ─────────────────────────────────────────
def test_bot_name_prefix_applied_to_connection(monkeypatch):
    """两实例连同一 self_id 时，前缀让 bot_name 不撞车。"""
    import asyncio

    from core_plugins.onebot_adapter.main import ApiCaller, OneBotWebSocketServer
    import core_plugins.onebot_adapter.main as mod

    caller = ApiCaller()
    server_a = OneBotWebSocketServer(
        {'listen_port': 80, 'bot_name_prefix': 'a'}, lambda *a: None, caller)
    server_b = OneBotWebSocketServer(
        {'listen_port': 800, 'bot_name_prefix': 'b'}, lambda *a: None, caller)

    captured = {}

    class FakeConn:
        def set_ws(self, ws): pass
        def set_ws_server(self, s): pass

    def fake_register(self, name):
        captured.setdefault('names', []).append(name)
        return FakeConn()

    monkeypatch.setattr(ApiCaller, 'register_connection', fake_register)
    monkeypatch.setattr(mod, '_get_request_headers', lambda ws: {'X-Self-ID': '10001'})
    monkeypatch.setattr(mod, '_get_request_path', lambda ws, path=None: '/ws')
    monkeypatch.setattr(mod, '_extract_token', lambda *a: None)

    class FakeWS:
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
        async def close(self, **kw): pass

    async def run():
        await server_a._handle_connection(FakeWS())
        await server_b._handle_connection(FakeWS())

    asyncio.run(run())
    # 两个实例的同 self_id 被前缀隔离，api_caller 收到两个不同连接名
    assert 'a:10001' in captured['names']
    assert 'b:10001' in captured['names']
    assert len(set(captured['names'])) == 2


# ── register 入口：创建 N 个实例、共享 caller ──────────────────
def test_register_creates_one_instance_by_default(monkeypatch):
    class Services:
        def __init__(self): self.reg = {}
        def register(self, k, v): self.reg[k] = v

    class FW:
        config = {'onebot': {'listen_port': 6830, 'access_token': 't'}}
        services = Services()
        loop = None
        def build_ssl_context(self): return None

    started = []
    monkeypatch.setattr(ob.OneBotAdapter, 'start', lambda self: started.append(self))

    ctx = type('Ctx', (), {'_framework': FW(), 'log': lambda self, m: None})()
    ob.register(ctx)
    assert len(ob._adapter_instances) == 1
    assert len(started) == 1
    assert ob._adapter_instance is ob._adapter_instances[0]
    # 服务注册指向主实例
    assert ctx._framework.services.reg['protocol_adapter'] is ob._adapter_instance


def test_register_creates_n_instances_shared_caller(monkeypatch):
    class Services:
        def __init__(self): self.reg = {}
        def register(self, k, v): self.reg[k] = v

    class FW:
        config = {'onebot': {
            'listen_port': 6830, 'access_token': 'base',
            'instances': [
                {'listen_port': 80, 'access_token': '1234', 'bot_name_prefix': 'a'},
                {'listen_port': 800, 'access_token': '5678', 'bot_name_prefix': 'b'},
            ],
        }}
        services = Services()
        loop = None
        def build_ssl_context(self): return None

    started = []
    monkeypatch.setattr(ob.OneBotAdapter, 'start', lambda self: started.append(self))

    ctx = type('Ctx', (), {'_framework': FW(), 'log': lambda self, m: None})()
    ob.register(ctx)
    assert len(ob._adapter_instances) == 2
    assert len(started) == 2
    ports = sorted(i.config['listen_port'] for i in ob._adapter_instances)
    assert ports == [80, 800]
    # 共享同一个 api_caller
    assert (ob._adapter_instances[0].api_caller
            is ob._adapter_instances[1].api_caller)
    # 主实例是第一个
    assert ob._adapter_instance is ob._adapter_instances[0]


def test_register_disabled_creates_nothing(monkeypatch):
    class FW:
        config = {'onebot': {'enabled': False, 'instances': [{'listen_port': 80}]}}
        services = type('S', (), {'register': lambda self, k, v: None})()
        def build_ssl_context(self): return None

    ob._adapter_instances = []
    ob._adapter_instance = None
    ctx = type('Ctx', (), {'_framework': FW(), 'log': lambda self, m: None})()
    ob.register(ctx)
    assert ob._adapter_instances == []
    assert ob._adapter_instance is None


# ── 连接页自述 ────────────────────────────────────────────────
def test_connection_info_lists_all_endpoints():
    class FW:
        config = {'onebot': {
            'listen_port': 6830,
            'instances': [
                {'listen_host': '127.0.0.1', 'listen_port': 80, 'bot_name_prefix': 'a'},
                {'listen_host': '127.0.0.1', 'listen_port': 800, 'bot_name_prefix': 'b'},
            ],
        }}

        def build_ssl_context(self): return None

    fw = FW()
    first = ob._build_instance_configs(fw.config['onebot'])[0]
    info = ob.OneBotAdapter(fw, config=first).get_connection_info()
    assert info['endpoints'] == [
        'ws://127.0.0.1:80/ws', 'ws://127.0.0.1:800/ws']
    assert info['endpoint_hint'] == info['endpoints'][0]
    assert info['status_extra']['instance_count'] == 2
    assert 'instances' in info['restart_keys']


def test_connection_info_uses_wss_for_all_instances():
    class FW:
        config = {'onebot': {
            'listen_port': 6830,
            'instances': [{'listen_port': 6831}, {'listen_port': 6832}],
        }}

        def build_ssl_context(self): return object()

    fw = FW()
    info = ob.OneBotAdapter(fw).get_connection_info()
    assert len(info['endpoints']) == 2
    assert all(e.startswith('wss://') for e in info['endpoints'])


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
