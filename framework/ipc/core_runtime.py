# -*- coding: utf-8 -*-
"""
CoreRuntime —— 双进程模式下进程1（核心）的运行时

职责（轻量，不加载用户插件）：
- 构造完整 Framework(role='core')：真实 Database + 白名单 core 插件
  （onebot_adapter / http_inject / http_api / webui）
- 启动 IpcServer（标准库 AF_INET + authkey 握手），accept 宿主连接
- 把 framework.dispatch_event 替换为"IPC 推送事件到宿主"
- 提供核心侧 RPC 服务：db.*（真实数据库执行）、api.call（转发 onebot 发送）、bots.list
- spawn 宿主进程（multiprocessing spawn），并监督其存活（Phase 1 提供基本 terminate）
"""
import asyncio
import logging
import multiprocessing
import os
import secrets
import signal
import time

logger = logging.getLogger('zcbot')


class CoreRuntime:
    """核心进程运行时"""

    def __init__(self, config_path=None):
        self.config_path = os.path.abspath(config_path) if config_path else None
        self.framework = None
        self.server = None
        self.host_proc = None
        self._token = secrets.token_bytes(32)
        self._dual_cfg = {}
        # 宿主崩溃重启状态
        self._restart_count = 0
        self._last_restart_ts = 0.0

    # ---- 构建 ----

    def _build(self):
        from framework.core import Framework
        from framework.ipc.ipc_server import IpcServer

        fw = Framework(self.config_path, role='core')
        self.framework = fw
        self._dual_cfg = fw.config.get('dual_process', {})

        self.server = IpcServer(self._token)
        self._register_core_handlers()
        self._register_remote_route_handler()

        # 跨进程事务：宿主 RemoteDatabase.transaction() → 核心 RemoteTxManager
        from framework.ipc.remote_tx import RemoteTxManager
        self._tx = RemoteTxManager(fw.db)
        self._register_tx_handlers()

        # 日志合并：宿主日志经 IPC 'log' 事件 → 核心 log_broker（WebUI 可见）
        from framework.log_broker import log_broker

        def _on_host_log(payload):
            try:
                log_broker.log(
                    'host', str(payload.get('level', 'INFO')),
                    str(payload.get('msg', '')),
                    {'source': str(payload.get('logger', 'host'))})
            except Exception:
                pass

        self.server.on('log', _on_host_log)

        # 替换事件分发：协议（onebot/http_inject）收到事件 → 推送到宿主
        async def _ipc_dispatch(event):
            await self.server.asend_event(event)

        fw.dispatch_event = _ipc_dispatch

    def _register_core_handlers(self):
        """注册核心侧 RPC 服务（宿主经 IPC 调用）"""
        fw = self.framework
        srv = self.server

        # db.*：转发到真实 Database（在核心事件循环的 executor 线程执行）
        def _db_handler(name):
            async def h(**kw):
                return await asyncio.to_thread(getattr(fw.db, name), **kw)
            return h

        for name in ('query', 'query_one', 'execute', 'execute_many', 'insert',
                     'scalar', 'exists', 'count', 'table_exists', 'table_info',
                     'table_has_column', 'pool_status'):
            srv.register(f'db.{name}', _db_handler(name))

        # api.call：插件发消息 → 核心的 onebot_adapter 经 WebSocket 发出
        async def _api_call(action, bot=None, params=None):
            caller = fw.services.get('api_caller')
            if caller is None:
                raise RuntimeError("核心进程无协议适配器（api_caller）")
            return await caller.acall(action, bot=bot, **(params or {}))

        srv.register('api.call', _api_call)

        def _bots_list():
            pa = fw.services.get('protocol_adapter')
            if pa is None:
                return []
            try:
                return pa.get_connected_bots()
            except Exception:
                return []

        srv.register('bots.list', _bots_list)

    def _make_remote_view(self, route_id):
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
                result = self.server.request_host(
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

    def _register_remote_route_handler(self):
        """远程 REST 路由（务实版）：宿主 route.register → 核心挂远程 stub 路由"""
        from framework.api import registry as _api_registry

        def _on_route_register(route_id, path, methods, auth):
            app = _api_registry.get_web_app()
            if app is None:
                return {'ok': False, 'error': 'web 未启用'}
            try:
                view = self._make_remote_view(route_id)
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

        self.server.register('route.register', _on_route_register)

    def _register_tx_handlers(self):
        """注册跨进程事务 RPC（宿主侧 RemoteDatabase.transaction 调用）"""
        tx = self._tx
        srv = self.server

        async def _begin():
            return await asyncio.to_thread(tx.begin)

        async def _run(tx_id, op, sql, params=None):
            return await asyncio.to_thread(tx.run, tx_id, op, sql, params)

        async def _commit(tx_id):
            return await asyncio.to_thread(tx.commit, tx_id)

        async def _rollback(tx_id):
            return await asyncio.to_thread(tx.rollback, tx_id)

        srv.register('tx.begin', _begin)
        srv.register('tx.run', _run)
        srv.register('tx.commit', _commit)
        srv.register('tx.rollback', _rollback)

    # ---- 生命周期 ----

    def _spawn_host(self):
        ctx = multiprocessing.get_context('spawn')
        from framework.ipc.host_entry import host_entry
        self.host_proc = ctx.Process(
            target=host_entry,
            args=(self.config_path, self.server.address, self._token, os.getpid()),
            daemon=False,
        )
        self.host_proc.start()
        logger.info(f"宿主进程已启动 pid={self.host_proc.pid}")

    def _restart_host(self) -> bool:
        """重启宿主进程（terminate 旧进程 → 重新 spawn）。

        返回是否真正重启；触发限流或配置关闭自动重启时返回 False。
        """
        dual = self._dual_cfg or {}
        max_restarts = int(dual.get('max_restarts', 5))
        restart_interval = float(dual.get('restart_interval', 30))  # 限流窗口（秒）
        now = time.monotonic()

        # 限流：限定时间窗口内超过最大重启次数则放弃（避免崩溃风暴）
        if max_restarts <= 0:
            return False
        if now - self._last_restart_ts > restart_interval:
            self._restart_count = 0  # 进入新窗口，重置计数
        if self._restart_count >= max_restarts:
            return False
        self._restart_count += 1
        self._last_restart_ts = now

        # 关掉可能仍存活的旧进程
        if self.host_proc is not None:
            try:
                if self.host_proc.is_alive():
                    self.host_proc.terminate()
                self.host_proc.join(5)
            except Exception:
                pass

        logger.warning(f"宿主进程重启（第 {self._restart_count}/{max_restarts} 次）")
        self._spawn_host()
        return True

    async def _amain(self):
        fw = self.framework
        loop = asyncio.get_running_loop()
        fw.loop = loop
        self.server.set_loop(loop)

        # 先启动 IPC accept，再加载插件，再 spawn 宿主（宿主 connect 会等待 accept）
        self.server.start_accept_thread()
        fw._load_core_plugins()
        self._spawn_host()

        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        # 监督宿主：崩溃则自动重启（带限流）；正常关停时不再重启
        async def _host_watchdog():
            while not stop_event.is_set():
                await asyncio.sleep(2)
                if self.host_proc is None:
                    continue
                if self.host_proc.is_alive():
                    continue
                if not self._restart_host():
                    logger.error("宿主进程已退出且达到重启上限，停止核心")
                    stop_event.set()

        asyncio.create_task(_host_watchdog())
        await stop_event.wait()
        await self._stop()

    async def _stop(self):
        if self.host_proc is not None and self.host_proc.is_alive():
            try:
                self.host_proc.terminate()
                self.host_proc.join(5)
            except Exception:
                pass
        if self.server is not None:
            self.server.close()
        try:
            await self.framework.stop()
        except Exception as e:
            logger.warning(f"核心框架停止异常: {e}")

    def run(self):
        """同步入口：进入事件循环"""
        self._build()
        try:
            asyncio.run(self._amain())
        except KeyboardInterrupt:
            print("\n核心进程收到 Ctrl+C，已退出。")
