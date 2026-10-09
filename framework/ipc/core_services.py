# -*- coding: utf-8 -*-
"""
core_services —— 核心进程侧 IPC 服务装配（内部模块，自 core_runtime.py 抽离）

只做装配，不做生命周期编排：CoreRuntime 负责构建 / 启动 / 监督 / 关闭，
本模块负责把核心侧能力挂到 IpcServer 上：

- db.*                                  宿主 RemoteDatabase → 核心真实 Database
- api.call / api.send_text / bots.list  宿主发消息 / 列 bot → 核心协议接入端
- tx.begin/run/commit/rollback          跨进程事务（RemoteTxManager）
- route.register                        宿主 route.register → 核心 Flask 挂远程 stub 路由
- log（事件）                           宿主日志事件 → 核心 log_broker（WebUI 可见）
- 事件分发替换                          fw.dispatch_event → IPC 推送到宿主

兼容约束（本任务不改协议）：RPC 名称、参数、返回值、Flask 行为与原先完全一致；
协议格式、传输、心跳、重连均不动。

依赖注入：framework / server / tx 全部经函数参数传入；模块级只 import 标准库，
flask / framework.api.registry / framework.log_broker 仍在函数内延迟导入
（单进程 dual_process.enabled=false 路径不加载本包）。
"""
import asyncio
import logging

logger = logging.getLogger('zcbot')


def register_core_handlers(server, framework):
    """注册核心侧 RPC 服务（宿主经 IPC 调用）：db.* / api.call / api.send_text / bots.list"""
    fw = framework
    srv = server

    # db.*：转发到真实 Database（在核心事件循环的 executor 线程执行）
    def _db_handler(name):
        async def h(**kw):
            return await asyncio.to_thread(getattr(fw.db, name), **kw)
        return h

    for name in ('query', 'query_one', 'execute', 'execute_many', 'insert',
                 'scalar', 'exists', 'count', 'table_exists', 'table_info',
                 'table_has_column', 'pool_status'):
        srv.register(f'db.{name}', _db_handler(name))

    # api.call：插件发消息 → 核心接入端经其协议发出
    async def _api_call(action, bot=None, params=None):
        caller = fw.services.get('api_caller')
        if caller is None:
            raise RuntimeError("核心进程无协议适配器（api_caller）")
        return await caller.acall(action, bot=bot, **(params or {}))

    srv.register('api.call', _api_call)

    # api.send_text：协议中立发文本 → 核心 protocol_adapter.send_text（宿主 IpcAdapter 转发）
    async def _send_text(text, user_id=None, group_id=None, source=None):
        adapter = fw.services.get('protocol_adapter')
        if adapter is None:
            raise RuntimeError("核心进程无协议适配器（protocol_adapter）")
        return await adapter.send_text(
            text, user_id=user_id, group_id=group_id, source=source)

    srv.register('api.send_text', _send_text)

    def _bots_list():
        pa = fw.services.get('protocol_adapter')
        if pa is None:
            return []
        try:
            return pa.get_connected_bots()
        except Exception:
            return []

    srv.register('bots.list', _bots_list)


def make_remote_view(server, route_id):
    """生成远程 stub 视图：Flask 收到请求 → 经 IPC 转发宿主执行 → 回传 JSON 响应"""
    import flask

    def view(**kw):
        try:
            params = {
                'method': flask.request.method,
                'path': flask.request.path,
                'args': dict(flask.request.args),
                'json': flask.request.get_json(silent=True),
                'form': dict(flask.request.form) if flask.request.form else None,
                'headers': {k: v for k, v in flask.request.headers.items()
                            if k.lower() in ('authorization', 'content-type',
                                             'x-api-key', 'accept')},
            }
            result = server.request_host(
                'http.dispatch', {'route_id': route_id, 'params': params})
        except Exception as e:
            return flask.jsonify({'code': -1, 'error': f'远程路由执行失败: {e}'}), 503

        # 契约：dict → 200 JSON；(status, dict) → 指定状态码；(status, dict, headers)
        status, headers, body = 200, {}, result
        if isinstance(result, tuple) and result:
            if len(result) == 1:
                body = result[0]
            elif len(result) == 2:
                status, body = result
            else:
                status, body, headers = result
        resp = flask.jsonify(body)
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp, status

    return view


def register_remote_route_handler(server, framework):
    """远程 REST 路由（务实版）：宿主 route.register → 核心挂远程 stub 路由"""
    from framework.api import registry as _api_registry

    def _on_route_register(route_id, path, methods, auth):
        app = _api_registry.get_web_app()
        if app is None:
            return {'ok': False, 'error': 'web 未启用'}
        try:
            view = make_remote_view(server, route_id)
            if auth:
                wrap = getattr(_api_registry, '_require_auth', None)
                if wrap is not None:
                    view = wrap(view)
            app.add_url_rule(
                path, endpoint=f'_zcbot_remote_{route_id}',
                view_func=view, methods=methods or ['GET'])
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    server.register('route.register', _on_route_register)


def register_tx_handlers(server, tx):
    """注册跨进程事务 RPC（宿主侧 RemoteDatabase.transaction 调用）"""
    async def _begin():
        return await asyncio.to_thread(tx.begin)

    async def _run(tx_id, op, sql, params=None):
        return await asyncio.to_thread(tx.run, tx_id, op, sql, params)

    async def _commit(tx_id):
        return await asyncio.to_thread(tx.commit, tx_id)

    async def _rollback(tx_id):
        return await asyncio.to_thread(tx.rollback, tx_id)

    server.register('tx.begin', _begin)
    server.register('tx.run', _run)
    server.register('tx.commit', _commit)
    server.register('tx.rollback', _rollback)


def register_host_log_relay(server):
    """宿主日志经 IPC 'log' 事件 → 核心 log_broker（WebUI 可见）。
    兼容两种格式：单条 dict（旧宿主）与批量 {'batch': [...]}（新宿主）。"""
    from framework.log_broker import log_broker

    def _on_host_log(payload):
        batch = payload.get('batch') if isinstance(payload, dict) else None
        entries = batch if isinstance(batch, list) else [payload]
        for p in entries:
            if not isinstance(p, dict):
                continue
            try:
                log_broker.log(
                    'host', str(p.get('level', 'INFO')),
                    str(p.get('msg', '')),
                    {'source': str(p.get('logger', 'host'))})
            except Exception:
                pass

    server.on('log', _on_host_log)


def bind_ipc_dispatch(server, framework):
    """替换事件分发：协议接入端收到事件 → 推送到宿主"""
    async def _ipc_dispatch(event):
        await server.asend_event(event)

    framework.dispatch_event = _ipc_dispatch
