# -*- coding: utf-8 -*-
"""
可插入的 Web API 路由注册表

让插件（或框架内部模块）能向运行中的 Web 应用注册自定义 REST 路由，
并复用框架自带的登录/API Key 鉴权。这是「把 ZCBOT 当IM 平台」的
关键接入点：外部系统/页面通过 http_inject 或自定义 API 与插件交互。

用法（在插件 register 里）：
    ctx.register_api('/api/my/stats', handler, methods=['GET'], auth=True)

实现要点：
    - create_web_app 构建完 Flask app 后调用 set_web_context(app, require_auth)
      把运行态 app 与鉴权装饰器暴露进来。
    - 用户插件在 webui 之后注册，register_api 时 app 已存在 → 立即 add_url_rule 挂载。
    - core 阶段（webui 之前）注册的路由，由 create_web_app 末尾 mount_all() 兜底挂载。
    - endpoint 由路径唯一决定：插件热重载时同路径覆盖登记项、只换 view 函数，
      不会在 url_map 上堆重复规则，也不会因新 endpoint 而丢路由。
    - Flask 在应用处理过首个请求后禁止 add_url_rule（_got_first_request 守卫）。
      热重载正是在这个时候注册路由，所以挂载时临时绕过该守卫：路由表与 matcher
      本身支持运行时增改，翻标志只影响 setupmethod 的检查，不影响请求分发。
"""
import hashlib
import logging
import re
from contextlib import contextmanager

logger = logging.getLogger('zcbot')

# 已注册的插件路由（顺序保持注册先后）
_plugin_routes = []
# path -> route，同路径重复登记视为覆盖（插件热重载换实现）
_routes_by_path = {}
# 运行态上下文（由 create_web_app 注入）
_web_app = None
_require_auth = None


def set_web_context(app, require_auth):
    """由 create_web_app 在构建完成后注入运行态 app 与鉴权装饰器"""
    global _web_app, _require_auth
    _web_app = app
    _require_auth = require_auth


def get_web_app():
    """返回当前运行的 Flask app（未构建时为 None）"""
    return _web_app


def _endpoint_for(path):
    """由路径决定 endpoint：热重载前后同一路线保持同一 endpoint"""
    slug = re.sub(r'[^0-9a-z]+', '_', str(path).lower()).strip('_')[:40]
    digest = hashlib.md5(str(path).encode('utf-8')).hexdigest()[:8]
    return f'_zcbot_api_{slug}_{digest}'


@contextmanager
def _late_setup_allowed(app):
    """临时解除 Flask「处理过首个请求后不许再 setup」的守卫

    插件热重载正是在 app 跑起来之后注册路由。该标志只被 _check_setup_finished
    读取（用于拦 setupmethod），路由表与 matcher 本身支持运行时增改，翻标志
    不会改变请求分发行为；下一个请求又会把它置回 True。
    """
    prev = getattr(app, '_got_first_request', False)
    app._got_first_request = False
    try:
        yield
    finally:
        app._got_first_request = prev


def add_route(path, methods, handler, auth=True):
    """登记一条插件路由；同路径重复登记视为覆盖旧登记项，供兜底挂载与热重载复用"""
    methods = tuple(dict.fromkeys(str(m).upper() for m in (methods or ['GET'])))
    route = _routes_by_path.get(path)
    if route is not None:
        route['methods'] = methods
        route['handler'] = handler
        route['auth'] = auth
        return route
    route = {
        'path': path,
        'methods': methods,
        'handler': handler,
        'auth': auth,
        'endpoint': _endpoint_for(path),
    }
    _routes_by_path[path] = route
    _plugin_routes.append(route)
    return route


def _wrapped_handler(route, require_auth):
    handler = route['handler']
    if route['auth']:
        auth_wrap = require_auth or _require_auth
        if auth_wrap is not None:
            handler = auth_wrap(handler)
    return handler


def _sync_rule_methods(app, endpoint, methods):
    """让 url_map 上已有规则的方法集合跟随新登记项"""
    wanted = {str(m).upper() for m in methods}
    for rule in app.url_map.iter_rules():
        if rule.endpoint != endpoint:
            continue
        current = set(rule.methods or ())
        if 'GET' in wanted:
            wanted.add('HEAD')
        if getattr(rule, 'provide_automatic_options', False):
            wanted.add('OPTIONS')
        if current != wanted:
            rule.methods = frozenset(wanted)


def mount_route(route, app=None, require_auth=None):
    """把单条路由挂到 Flask app 上；返回是否成功"""
    app = app or _web_app
    if app is None:
        return False
    handler = _wrapped_handler(route, require_auth)
    endpoint = route['endpoint']
    try:
        if endpoint in app.view_functions:
            # 路由早已挂上、这次只是换实现：替换 view，不动 url_map
            app.view_functions[endpoint] = handler
            _sync_rule_methods(app, endpoint, route['methods'])
            return True
        with _late_setup_allowed(app):
            app.add_url_rule(route['path'], endpoint=endpoint,
                             view_func=handler, methods=list(route['methods']))
        return True
    except Exception as e:
        logger.warning(f"挂载插件路由失败 {route['path']}: {e}")
        return False


def live_mount(route):
    """用户插件在 register 时对已运行 app 即时挂载"""
    if _web_app is None:
        return False
    return mount_route(route)


def mount_all(app=None, require_auth=None):
    """挂载所有已登记但尚未挂载的路由（create_web_app 末尾调用）"""
    app = app or _web_app
    if app is None:
        return 0
    mounted = 0
    for route in list(_plugin_routes):
        # 已挂载（同 endpoint 已在 view_functions）则跳过
        if route['endpoint'] in app.view_functions:
            continue
        if mount_route(route, app, require_auth):
            mounted += 1
    return mounted


def register_route(path, methods, handler, auth=True):
    """完整入口：登记 + 立即挂载（若 app 已运行）"""
    route = add_route(path, methods, handler, auth)
    live_mount(route)
    return route
