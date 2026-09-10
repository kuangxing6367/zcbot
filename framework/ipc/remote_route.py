# -*- coding: utf-8 -*-
"""
RemoteRouteRegistry —— 宿主进程（进程2）的远程 REST 路由注册器（务实版）

双进程模式下 Web 服务器（Flask）在核心进程，而插件的 `ctx.register_api`
handler 在宿主进程。务实版方案：

- 宿主注册路由时，把 handler 保留在宿主侧路由表，并经 IPC 通知核心挂一条
  「远程 stub 路由」（route_id 关联）。
- 核心 Flask 收到请求 → 把请求数据打成 params dict → `request_host('http.dispatch')`
  → 宿主执行真 handler → 回传响应 → 核心生成 Flask JSON 响应。

handler 契约（务实版，与单进程 Flask view 风格不同）：
    def my_api(params: dict) -> dict
    # params 含 method/path/args/json/form/headers
    # 返回 dict 即 JSON 响应；可返回 (status, dict) 或 (status, dict, headers)
"""
import itertools


class RemoteRouteRegistry:
    """宿主侧远程路由表 + 核心请求转发回调"""

    def __init__(self, client):
        self._client = client
        self._handlers = {}          # route_id -> handler
        self._seq = itertools.count(1)

    def register_route(self, path, methods, handler, auth=True) -> bool:
        """登记路由并通知核心挂载远程 stub；返回是否成功"""
        route_id = f"rr{next(self._seq)}"
        self._handlers[route_id] = handler
        try:
            self._client.call('route.register', {
                'route_id': route_id,
                'path': path,
                'methods': list(methods),
                'auth': bool(auth),
            })
        except Exception as e:
            self._handlers.pop(route_id, None)
            raise RuntimeError(f"远程路由注册失败 {path}: {e}") from e
        return True

    def dispatch(self, route_id, params=None):
        """核心请求转发的入口：按 route_id 执行宿主侧真 handler（可被核心 request_host 调用）"""
        handler = self._handlers.get(route_id)
        if handler is None:
            raise RuntimeError(f"未知远程路由: {route_id}")
        return handler(params or {})

    def has(self, route_id) -> bool:
        return route_id in self._handlers
