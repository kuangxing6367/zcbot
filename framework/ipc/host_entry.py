# -*- coding: utf-8 -*-
"""
host_entry —— 双进程模式下进程2（宿主）的入口函数

由核心进程 multiprocessing.spawn 启动（Windows 无 fork，target 必须是模块级函数，
只能传标量：config_path / ipc address / authkey / core_pid）。

宿主职责：
- 用 IpcClient 连接核心进程（authkey 握手鉴权）
- 构造完整 Framework(role='host')：db 被替换为 RemoteDatabase（经 IPC RPC 到核心）
- 注册 IpcAdapter 为协议适配器 + api_caller（事件注入 / 发消息转发）
- 绑定事件注入：核心推送的事件经此进入宿主 dispatch_event（raw_handlers→router→event_bus）
- 加载并执行全部用户插件（ctx 零改动）
"""
import asyncio
import logging
import os
import signal

logger = logging.getLogger('zcbot')


def host_entry(config_path, address, token, core_pid):
    """宿主进程入口（模块级函数，供 spawn 调用）"""
    from framework.ipc.ipc_client import IpcClient

    client = IpcClient(address, token)
    client.connect()

    from framework.core import Framework
    from framework.ipc.ipc_adapter import IpcAdapter

    # 构造宿主 Framework：db 由 RemoteDatabase 代理，排除协议/web 类 core 插件
    fw = Framework(config_path, role='host', ipc_client=client)

    # 注册 IPC 协议适配器（services['api_caller'] 供插件发消息）
    adapter = IpcAdapter(fw, client)
    fw.services.register('protocol_adapter', adapter)
    fw.services.register('api_caller', adapter)

    # 终端命令远程执行：核心进程的终端把宿主侧命令（plugins/tasks 等）转发过来执行
    async def _terminal_exec(name, args=''):
        return await fw.terminal_exec(name, args)
    client.register('terminal.exec', _terminal_exec)

    # 远程 REST 路由（务实版）：插件 ctx.register_api 在宿主侧注册，
    # 核心 Flask 请求经 http.dispatch 转发回来执行
    from framework.ipc.remote_route import RemoteRouteRegistry
    fw._remote_routes = RemoteRouteRegistry(client)
    client.register('http.dispatch', fw._remote_routes.dispatch)

    # 日志合并：宿主日志经 IPC 推送到核心 log_broker（WebUI 可见）。
    # 移除 root 的文件 handler，避免宿主与核心同时写同一 zcbot.log 造成竞争。
    import logging
    from framework.ipc.host_log import IpcLogHandler
    _root_logger = logging.getLogger()
    for _h in list(_root_logger.handlers):
        if isinstance(_h, logging.handlers.RotatingFileHandler):
            _root_logger.removeHandler(_h)
    _root_logger.addHandler(IpcLogHandler(client))

    # 父进程死亡自检：核心退出则优雅关闭宿主
    stop_event = asyncio.Event()

    async def _parent_watchdog():
        while not stop_event.is_set():
            await asyncio.sleep(2)
            try:
                if os.getppid() != core_pid:
                    logger.warning("核心进程已退出，宿主即将关闭")
                    stop_event.set()
                    return
            except Exception:
                pass

    async def _amain():
        loop = asyncio.get_running_loop()
        fw.loop = loop
        client.set_loop(loop)
        adapter.bind_event_loop(loop)
        client.start_heartbeat()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        asyncio.create_task(_parent_watchdog())
        try:
            await fw.start()
            # 阻塞等待停止信号 / 父进程退出，保持宿主常驻
            await stop_event.wait()
        finally:
            await fw.stop()
            client.close()

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        # Ctrl+C 在子进程里可能以裸 KeyboardInterrupt 形式抵达（Windows 上
        # add_signal_handler 对 SIGINT/SIGTERM 抛 NotImplementedError 被静默吞掉），
        # 照核心进程的做法吞掉它，让宿主随核心一起干净退出（finally 已做 stop/close）。
        pass


if __name__ == '__main__':
    # 极少直接运行（调试用）
    raise SystemExit("host_entry 由核心进程 spawn 启动，请勿直接运行")
