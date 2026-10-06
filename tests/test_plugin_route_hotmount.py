# -*- coding: utf-8 -*-
"""插件 API 路由热挂载回归测试。

背景：插件热重载发生在 Flask app 已经处理过请求之后，而 add_url_rule 被
_got_first_request 守卫拦住 —— 于是新接口全 404，页面只报"请求失败"。
这里覆盖三件事：首请求之后再注册的路由要能挂上、同路径重复登记要换实现而不是
堆规则、方法集合变更要跟随。

不用 test_client（线上 flask/werkzeug 版本组合下它会炸），直接走 WSGI 调用。

运行：python tests/test_plugin_route_hotmount.py
"""
import os
import sys
import types

from flask import Flask
from werkzeug.test import EnvironBuilder

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from framework.api import registry  # noqa: E402


def _request(app, method, path):
    env = EnvironBuilder(path=path, method=method).get_environ()
    box = {}

    def start_response(status, headers):
        box['status'] = int(status.split(' ', 1)[0])

    body = b''.join(app.wsgi_app(env, start_response))
    return types.SimpleNamespace(status_code=box.get('status'),
                                 data=body.decode('utf-8'))


def _reset():
    registry._plugin_routes.clear()
    registry._routes_by_path.clear()
    registry._web_app = None
    registry._require_auth = None


def _rule_count(app, endpoint):
    return sum(1 for r in app.url_map.iter_rules() if r.endpoint == endpoint)


def test_mount_after_first_request():
    """首请求之后再注册的路由必须立刻可用（热重载主场景）"""
    _reset()
    app = Flask(__name__)
    app.add_url_rule('/ping', 'ping', lambda: 'pong')
    registry.set_web_context(app, None)
    assert _request(app, 'GET', '/ping').status_code == 200
    # 走到这里 app 已经处理过请求，add_url_rule 的守卫会拒绝 setup
    assert app._got_first_request is True

    route = registry.register_route('/api/demo/stats', ['GET'],
                                    lambda: 'stats', auth=False)
    assert route is not None
    r = _request(app, 'GET', '/api/demo/stats')
    assert r.status_code == 200, f"热挂载后仍 404: {r.status_code}"
    assert r.data == 'stats'
    assert _rule_count(app, route['endpoint']) == 1, "路由规则被重复登记"
    assert app._got_first_request is True, "守卫标志没复原"


def test_reload_replaces_handler():
    """同路径重复登记 = 换实现，不新增规则"""
    _reset()
    app = Flask(__name__)
    registry.set_web_context(app, None)
    registry.register_route('/api/demo/v1', ['GET'], lambda: 'old', auth=False)
    assert _request(app, 'GET', '/api/demo/v1').data == 'old'

    first = registry._routes_by_path['/api/demo/v1']
    again = registry.add_route('/api/demo/v1', ['GET'], lambda: 'new', auth=False)
    assert again['endpoint'] == first['endpoint'], '同路径 endpoint 应稳定'
    assert registry.live_mount(again) is True
    assert len(registry._plugin_routes) == 1, '同路径不应追加登记项'

    assert _request(app, 'GET', '/api/demo/v1').data == 'new'
    assert _rule_count(app, first['endpoint']) == 1, '热重载堆出了重复规则'


def test_methods_change_follows():
    """重载后新增的 HTTP 方法要生效"""
    _reset()
    app = Flask(__name__)
    registry.set_web_context(app, None)
    registry.register_route('/api/demo/edit', ['GET'], lambda: 'got', auth=False)
    ep = registry._routes_by_path['/api/demo/edit']['endpoint']
    assert _request(app, 'POST', '/api/demo/edit').status_code == 405

    route = registry.add_route('/api/demo/edit', ['GET', 'POST'],
                               lambda: 'posted', auth=False)
    assert registry.mount_route(route, app) is True
    r = _request(app, 'POST', '/api/demo/edit')
    assert r.status_code == 200, f"方法集合没跟随: {r.status_code}"
    assert r.data == 'posted'
    assert _rule_count(app, ep) == 1


def test_auth_wrapped_once():
    """鉴权包装每次挂载只套一层，热重载不会层层包裹"""
    _reset()
    calls = []

    def require_auth(fn):
        def wrapper(*args, **kwargs):
            calls.append(1)
            return fn(*args, **kwargs)
        return wrapper

    app = Flask(__name__)
    registry.set_web_context(app, require_auth)
    registry.register_route('/api/demo/auth', ['GET'], lambda: 'ok', auth=True)
    assert _request(app, 'GET', '/api/demo/auth').status_code == 200
    assert len(calls) == 1, f"鉴权包装层数异常: {calls}"

    route = registry.add_route('/api/demo/auth', ['GET'], lambda: 'ok2', auth=True)
    assert registry.mount_route(route, app) is True
    assert _request(app, 'GET', '/api/demo/auth').data == 'ok2'
    assert len(calls) == 2, f"热重载后包装层数翻倍: {calls}"


def test_mount_all_idempotent():
    """mount_all 对已挂载路由不重复计数（create_web_app 可能多次调用）"""
    _reset()
    app = Flask(__name__)
    registry.add_route('/api/demo/a', ['GET'], lambda: 'a', auth=False)
    registry.add_route('/api/demo/b', ['GET'], lambda: 'b', auth=False)
    assert registry.mount_all(app) == 2
    assert registry.mount_all(app) == 0


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'{len(fns)} 项通过')
