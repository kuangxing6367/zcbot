# -*- coding: utf-8 -*-
"""
RemoteRouteRegistry —— 宿主进程（进程2）的远程 REST 路由注册器（务实版）

双进程模式下 Web 服务器（Flask）在核心进程，而插件的 `ctx.register_api`
handler 在宿主进程。务实版方案：

- 宿主注册路由时，把 handler 保留在宿主侧路由表，并经 IPC 通知核心挂一条
  「远程 stub 路由」（route_id 关联）。
- 核心 Flask 收到请求 → 把请求数据打成 params dict → `request_host('http.dispatch')`
  → 宿主执行真 handler → 回传响应 → 核心生成 Flask JSON 响应。

handler 契约（务实版，与单进程 Flask view 形式不同）：
    def my_api(params: dict) -> dict
    # params 含 method/path/args/json/form/headers
    # 返回 dict 即 JSON 响应；可返回 (status, dict) 或 (status, dict, headers)

插件归属：每条路由记 plugin（插件名），热重载/卸载时按插件一次性回收——
宿主侧摘 handler 并经 IPC 通知核心摘 stub 路由，避免「插件已经没了，
旧接口还在跑旧闭包」。
"""
import itertools
import logging

logger = logging.getLogger('zcbot')


class RemoteRouteRegistry:
    """宿主侧远程路由表 + 核心请求转发回调"""

    def __init__(self, client):
        self._client = client
        self._handlers = {}          # route_id -> handler
        self._owners = {}            # route_id -> plugin_name（None=框架内部）
        self._by_path = {}           # path -> route_id（同路径重载时复用，避免堆规则）
        self._seq = itertools.count(1)

    def register_route(self, path, methods, handler, auth=True, plugin=None) -> bool:
        """登记路由并通知核心挂载远程 stub；返回是否成功

        同路径重复登记（插件热重载）复用原 route_id：核心侧按 path 派生的
        endpoint 不变，只换视图实现，不会在 url_map 上越堆越多旧规则。
        """
        route_id = self._by_path.get(path)
        reused = route_id is not None
        if route_id is None:
            route_id = f"rr{next(self._seq)}"
        old = self._handlers.get(route_id)
        self._handlers[route_id] = handler
        self._owners[route_id] = plugin
        self._by_path[path] = route_id
        try:
            self._client.call('route.register', {
                'route_id': route_id,
                'path': path,
                'methods': list(methods),
                'auth': bool(auth),
                'plugin_name': plugin,
            })
        except Exception as e:
            # 回滚：恢复旧 handler（重载场景）或彻底摘除（首次登记）
            if reused and old is not None:
                self._handlers[route_id] = old
            else:
                self._handlers.pop(route_id, None)
                self._owners.pop(route_id, None)
                self._by_path.pop(path, None)
            raise RuntimeError(f"远程路由注册失败 {path}: {e}") from e
        return True

    def unregister_plugin(self, plugin_name) -> int:
        """回收某插件登记的全部远程路由（宿主侧 + 通知核心），返回条数"""
        if not plugin_name:
            return 0
        gone = [rid for rid, owner in self._owners.items() if owner == plugin_name]
        for route_id in gone:
            self._handlers.pop(route_id, None)
            self._owners.pop(route_id, None)
        for path, rid in list(self._by_path.items()):
            if rid in gone:
                self._by_path.pop(path, None)
        if not gone:
            return 0
        try:
            self._client.call('route.unregister', {'plugin_name': plugin_name})
        except Exception as e:
            # 通知失败不改本地状态：本地已摘干净，核心侧残留最多一条 404 兜底
            logger.warning(f"通知核心回收远程路由失败（{plugin_name}）: {e}")
        return len(gone)

    def dispatch(self, route_id, params=None):
        """核心请求转发的入口：按 route_id 执行宿主侧真 handler（可被核心 request_host 调用）"""
        handler = self._handlers.get(route_id)
        if handler is None:
            raise RuntimeError(f"未知远程路由: {route_id}")
        return handler(params or {})

    def has(self, route_id) -> bool:
        return route_id in self._handlers
