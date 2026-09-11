"""终端输入监听

在独立线程读取控制台输入，经事件循环执行已注册命令。
"""

import asyncio
import logging
import threading

from .command import terminal_commands

logger = logging.getLogger('zcbot')


class TerminalInput:
    """终端输入监听器"""

    def __init__(self, framework):
        self.framework = framework
        self._running = False
        self._thread = None

    def start(self):
        """启动终端监听"""
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="terminal-input")
        self._thread.start()
        logger.info("终端交互已启动，输入 help 查看可用命令")

    def stop(self):
        """停止终端监听"""
        self._running = False

    def _read_loop(self):
        """读取终端输入（在单独线程中运行）"""
        while self._running:
            try:
                line = input()
                if not line.strip():
                    continue
                # 在事件循环中执行命令
                if self.framework.loop and self.framework.loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._execute_command(line.strip()),
                        self.framework.loop
                    ).result(timeout=30)
            except EOFError:
                break
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"终端输入读取异常: {e}")

    async def _execute_command(self, line: str):
        """执行终端命令（按命令归属进程路由）

        双进程模式下，核心进程的终端负责接收输入；状态在宿主进程的命令
        （plugins / tasks 等）经 IPC 转发到宿主执行，实现跨进程终端。
        单进程模式（role=standard）下没有下游进程，一律本地执行。
        """
        parts = line.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler = terminal_commands.get(cmd_name)
        if handler is None:
            logger.warning(f"未知命令: {cmd_name}，输入 help 查看可用命令")
            return

        role = getattr(self.framework, '_role', 'standard')
        target = terminal_commands.get_target(cmd_name)

        # 非核心进程（单进程 standard / 宿主 host）：没有下游进程，一律本地执行
        if role != 'core':
            await self._run_local(cmd_name, handler, args)
            return

        # 核心进程（双进程）：按命令归属路由
        if target == 'host':
            await self._run_remote(cmd_name, args, label='宿主进程')
        elif target == 'both':
            print('--- 核心进程 ---')
            await self._run_local(cmd_name, handler, args)
            await self._run_remote(cmd_name, args, label='宿主进程')
        else:
            await self._run_local(cmd_name, handler, args)

    async def _run_local(self, cmd_name: str, handler, args: str):
        """在本进程执行命令"""
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(args)
            else:
                await asyncio.to_thread(handler, args)
        except Exception as e:
            logger.error(f"终端命令 [{cmd_name}] 执行失败: {e}")

    async def _run_remote(self, cmd_name: str, args: str, label: str = None):
        """经 IPC 把命令转发到宿主进程执行，并打印其返回输出"""
        fw = self.framework
        server = getattr(fw, 'ipc_server', None)
        if server is None or not getattr(server, 'connected', False):
            print(f"[{cmd_name}] 宿主进程未连接，无法执行该命令（其状态在宿主进程）")
            return
        try:
            text = await server.arequest_host(
                'terminal.exec', {'name': cmd_name, 'args': args})
        except Exception as e:
            print(f"[{cmd_name}] 转发到宿主进程失败: {e}")
            return
        if label:
            print(f"--- {label} ---")
        if text:
            print(str(text).rstrip())
