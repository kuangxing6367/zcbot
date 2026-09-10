# -*- coding: utf-8 -*-
"""
可插入的 Web API 路由注册表

让插件（或框架内部模块）能向运行中的 Web 应用注册自定义 REST 路由，
并复用框架自带的登录/API Key 鉴权。这是「把 ZCBOT 当通用服务宿主」的
关键接入点：外部系统/页面通过 http_inject 或自定义 API 与插件交互。

用法（在插件 register 里）：
    ctx.register_api('/api/my/stats', handler, methods=['GET'], auth=True)

实现要点：
    - create_web_app 构建完 Flask app 后调用 set_web_context(app, require_auth)
      把运行态 app 与鉴权装饰器暴露进来。
    - 用户插件在 webui 之后注册，register_api 时 app 已存在 → 立即 add_url_rule 挂载。
    - core 阶段（webui 之前）注册的路由，由 create_web_app 末尾 mount_all() 兜底挂载。
    - 每个注册分配唯一 endpoint，避免 Flask 同名 endpoint 冲突。
"""
import logging

logger = logging.getLogger('zcbot')

# 已注册的插件路由（顺序保持注册先后）
_plugin_routes = []
# 唯一 endpoint 计数器
_route_counter = 0
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


def add_route(path, methods, handler, auth=True):
    """登记一条插件路由（不立即挂载，供 create_web_app 兜底挂载用）"""
    global _route_counter
    _route_counter += 1
    route = {
        'path': path,
        'methods': tuple(methods),
        'handler': handler,
        'auth': auth,
        'endpoint': f'_zcbot_api_{_route_counter}',
    }
    _plugin_routes.append(route)
    return route


def mount_route(route, app=None, require_auth=None):
    """把单条路由挂到 Flask app 上；返回是否成功"""
    app = app or _web_app
    if app is None:
        return False
    handler = route['handler']
    if route['auth']:
        auth_wrap = require_auth or _require_auth
        if auth_wrap is not None:
            handler = auth_wrap(handler)
    try:
        app.add_url_rule(route['path'], endpoint=route['endpoint'],
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
    for route in _plugin_routes:
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
