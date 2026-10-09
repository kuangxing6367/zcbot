# -*- coding: utf-8 -*-
"""
CoreRuntime —— 双进程模式下进程1（核心）的运行时

职责（只做编排 / 启动 / 监督 / 关闭，不加载用户插件）：
- 构造完整 Framework(role='core')：真实 Database + 白名单 core 插件
  （协议接入端 onebot_adapter/http_inject、http_api、webui）
- 启动 IpcServer（标准库 AF_INET + authkey 握手），accept 宿主连接
- 装配核心侧 IPC 服务：db.*（真实数据库执行）、api.call / api.send_text
  （转发接入端发送）、bots.list、tx.*（跨进程事务）、route.register（远程
  路由 stub）、宿主日志桥接与事件分发替换——接线细节全部收敛在内部模块
  framework/ipc/core_services.py，本类保留同名薄委托方法（方法名 / RPC 名 /
  参数 / 返回值 / Flask 行为兼容；协议格式 / 传输 / 心跳 / 重连不变）
- spawn 宿主进程（multiprocessing spawn），并监督其存活（Phase 1 提供基本 terminate）
"""
import asyncio
import logging
import multiprocessing
import os
import secrets
import signal
import time

# 核心 RPC 服务装配的内部实现（本包内模块，非跨层导入）
from framework.ipc.core_services import (
    bind_ipc_dispatch,
    make_remote_view,
    register_core_handlers,
    register_host_log_relay,
    register_remote_route_handler,
    register_tx_handlers,
)

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
        # 暴露给 framework：终端在核心进程运行，宿主侧命令经此转发（terminal.exec）
        fw.ipc_server = self.server

        # 核心 RPC 服务装配：db.* / api.* / bots.list / route.register —— 细节在
        # framework/ipc/core_services.py（本类只保留同名薄委托方法，兼容旧调用）
        self._register_core_handlers()
        self._register_remote_route_handler()

        # 跨进程事务：宿主 RemoteDatabase.transaction() → 核心 RemoteTxManager
        from framework.ipc.remote_tx import RemoteTxManager
        self._tx = RemoteTxManager(fw.db)
        self._register_tx_handlers()

        # 宿主日志桥接（IPC 'log' 事件 → 核心 log_broker，WebUI 可见）
        # + 事件分发替换（协议接入端收到事件 → IPC 推送到宿主）
        register_host_log_relay(self.server)
        bind_ipc_dispatch(self.server, fw)

    # ---- 核心 RPC 服务装配（薄委托，实现见 framework/ipc/core_services.py）----

    def _register_core_handlers(self):
        """注册核心侧 RPC 服务（宿主经 IPC 调用）：db.* / api.call / api.send_text / bots.list"""
        register_core_handlers(self.server, self.framework)

    def _make_remote_view(self, route_id):
        """生成远程 stub 视图：Flask 收到请求 → 经 IPC 转发宿主执行 → 回传 JSON 响应"""
        return make_remote_view(self.server, route_id)

    def _register_remote_route_handler(self):
        """远程 REST 路由（务实版）：宿主 route.register → 核心挂远程 stub 路由"""
        register_remote_route_handler(self.server, self.framework)

    def _register_tx_handlers(self):
        """注册跨进程事务 RPC（宿主侧 RemoteDatabase.transaction 调用）"""
        register_tx_handlers(self.server, self._tx)

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

        # 终端交互在核心进程（它是前台、占控制台）；宿主子进程无交互 stdin。
        # 核心进程不走 fw.start()（只加载核心侧插件，不加载用户插件），
        # 故这里显式注册终端命令并启动输入线程；宿主侧命令由终端经 IPC 转发执行。
        from framework.terminal import register_builtins
        register_builtins(fw)
        fw.terminal.start()
        fw.start_console_server()

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
