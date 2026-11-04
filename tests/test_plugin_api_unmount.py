# -*- coding: utf-8 -*-
"""插件 API 路由回收回归测试（热重载/卸载的旧闭包残留）。

背景：插件用 ctx.register_api 注册的 REST 路由原先没有归属记录，卸载/热重载
时无人回收。后果：
  - 新版本删掉/改名的接口继续用旧模块闭包处理请求（跑旧代码）；
  - 旧模块被路由表强引用，gc 回收不掉；
  - 双进程模式下每次重载都 add_url_rule 一条新规则，旧规则先注册先匹配，
    等于永久钉死在旧实现上。

覆盖：本插件路由回收、同路径所有权转移、同路径只保留一条规则、
框架内部路由不被误删、远程注册器（宿主侧 + IPC 通知）、核心侧按 path 去重、
loader 卸载时调用回收。

运行：python tests/test_plugin_api_unmount.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from werkzeug.test import EnvironBuilder

from framework.api import registry
from framework.ipc import core_services
from framework.ipc.remote_route import RemoteRouteRegistry


def _request(app, path, method='GET'):
    env = EnvironBuilder(path=path, method=method).get_environ()
    box = {}

    def start_response(status, headers):
        box['status'] = int(status.split(' ', 1)[0])

    body = b''.join(app.wsgi_app(env, start_response))
    return box.get('status'), body.decode('utf-8')


def _reset():
    registry._plugin_routes.clear()
    registry._routes_by_path.clear()
    registry._web_app = None
    registry._require_auth = None


def _rule_count(app, endpoint):
    return sum(1 for r in app.url_map.iter_rules() if r.endpoint == endpoint)


def test_unregister_replaces_view_with_404():
    """插件卸载后，原接口必须变成 404 而不是继续跑旧闭包"""
    _reset()
    app = Flask(__name__)
    registry.set_web_context(app, None)
    registry.register_route('/api/demo/gone', ['GET'], lambda: 'old', auth=False,
                            plugin='demo')
    assert _request(app, '/api/demo/gone')[1] == 'old'
    endpoint = registry._routes_by_path['/api/demo/gone']['endpoint']

    assert registry.unregister_plugin('demo') == 1
    assert '/api/demo/gone' not in registry._routes_by_path
    assert registry._plugin_routes == []
    code, body = _request(app, '/api/demo/gone')
    assert code == 404, f'卸载后仍返回 {code}，旧闭包还在跑'
    assert 'removed' in body
    # 规则没被堆第二条（同 endpoint 只该有一条）
    assert _rule_count(app, endpoint) == 1


def test_reload_same_path_single_rule_new_handler():
    """热重载重注册同路径：一条规则、实现换成新的"""
    _reset()
    app = Flask(__name__)
    registry.set_web_context(app, None)
    registry.register_route('/api/demo/v', ['GET'], lambda: 'v1', auth=False,
                            plugin='demo')
    endpoint = registry._routes_by_path['/api/demo/v']['endpoint']
    registry.register_route('/api/demo/v', ['GET'], lambda: 'v2', auth=False,
                            plugin='demo')

    assert len(registry._plugin_routes) == 1
    assert _rule_count(app, endpoint) == 1
    assert _request(app, '/api/demo/v')[1] == 'v2'


def test_ownership_transfer_on_path_handover():
    """同路径被另一插件接管后，原插件卸载不得删掉新主人的路由"""
    _reset()
    app = Flask(__name__)
    registry.set_web_context(app, None)
    registry.register_route('/api/shared', ['GET'], lambda: 'from-a', auth=False,
                            plugin='a')
    registry.register_route('/api/shared', ['GET'], lambda: 'from-b', auth=False,
                            plugin='b')
    assert registry._routes_by_path['/api/shared']['plugin'] == 'b'

    assert registry.unregister_plugin('a') == 0, '已移交所有权的路由不该被旧主人回收'
    assert _request(app, '/api/shared')[1] == 'from-b'


def test_framework_routes_survive():
    """plugin=None 的框架内部路由不随任何插件卸载消失"""
    _reset()
    registry.add_route('/api/internal', ['GET'], lambda: 'core', auth=False)
    registry.register_route('/api/demo/x', ['GET'], lambda: 'x', auth=False,
                            plugin='demo')
    registry.unregister_plugin('demo')
    assert '/api/internal' in registry._routes_by_path
    assert len(registry._plugin_routes) == 1


def test_unregister_before_web_enabled():
    """Web 未启用时也照样摘登记项，之后 mount_all 不会把它挂回来"""
    _reset()
    registry.register_route('/api/demo/late', ['GET'], lambda: 'late', auth=False,
                            plugin='demo')
    registry.unregister_plugin('demo')
    app = Flask(__name__)
    assert registry.mount_all(app) == 0
    assert '/api/demo/late' not in {r.rule for r in app.url_map.iter_rules()}


class _FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, name, payload):
        self.calls.append((name, dict(payload)))
        return {'ok': True}


def test_remote_register_dedupes_and_unregisters():
    """远程注册器：同路径复用 route_id，卸载时摘 handler 并通知核心"""
    client = _FakeClient()
    rr = RemoteRouteRegistry(client)
    rr.register_route('/api/m/stats', ['GET'], lambda p: {'v': 1},
                      auth=True, plugin='m')
    rid = client.calls[-1][1]['route_id']
    assert client.calls[-1][1]['plugin_name'] == 'm'

    # 重载：同路径必须复用 route_id，不新增宿主侧 handler
    rr.register_route('/api/m/stats', ['GET'], lambda p: {'v': 2},
                      auth=True, plugin='m')
    assert client.calls[-1][1]['route_id'] == rid
    assert len(rr._handlers) == 1
    assert rr.dispatch(rid) == {'v': 2}, '重载后应执行新 handler'

    assert rr.unregister_plugin('m') == 1
    assert rr._handlers == {} and rr._by_path == {}
    assert client.calls[-1][0] == 'route.unregister'
    assert client.calls[-1][1]['plugin_name'] == 'm'
    try:
        rr.dispatch(rid)
    except RuntimeError:
        pass
    else:
        raise AssertionError('卸载后 dispatch 仍成功，handler 没摘干净')


def test_core_side_register_dedupes_by_path():
    """核心侧收到多次 route.register：同 path 只留一条登记项"""
    _reset()

    class _Server:
        def __init__(self):
            self.handlers = {}

        def register(self, name, fn):
            self.handlers[name] = fn

        def request_host(self, *a, **kw):
            return {}

    srv = _Server()
    core_services.register_remote_route_handler(srv, None)
    on_reg = srv.handlers['route.register']
    on_unreg = srv.handlers['route.unregister']
    app = Flask(__name__)
    registry.set_web_context(app, None)

    assert on_reg('rr1', '/api/remote/x', ['GET'], False, 'rp')['ok'] is True
    assert on_reg('rr2', '/api/remote/x', ['GET'], False, 'rp')['ok'] is True
    assert len(registry._plugin_routes) == 1, '核心侧按 path 去重失败'
    assert on_unreg('rp')['removed'] == 1
    assert registry._plugin_routes == []


class _LoaderStub:
    """只装配 unload 需要的字段，验证 loader 会不会真的去回收路由"""

    from framework.loader.lifecycle import PluginLifecycleMixin as _M
    _release_plugin_api_routes = _M._release_plugin_api_routes
    unload_plugin = _M.unload_plugin

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._loaded_plugins = {}
        self._failed_mtimes = {}
        self._plugin_mtimes = {}
        self.plugins_dir = '/tmp/zcbot-none'
        self.framework = type('F', (), {})()
        self.db = type('D', (), {'execute': lambda *a, **k: None})()


def test_loader_unload_releases_routes():
    """插件加载失败后没有 unload 记录，卸载分支仍要回收半初始化的路由"""
    _reset()
    app = Flask(__name__)
    registry.set_web_context(app, None)
    registry.register_route('/api/half/x', ['GET'], lambda: 'half', auth=False,
                            plugin='half')
    _LoaderStub().unload_plugin('half')
    assert registry._plugin_routes == []
    assert _request(app, '/api/half/x')[0] == 404


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'{len(fns)} 项通过')
