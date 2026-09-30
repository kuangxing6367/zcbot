"""终端输入监听

在独立线程读取控制台输入，经事件循环执行已注册命令。
非交互环境（stdin 非 TTY / 已重定向，如 CI、守护进程、测试）不启动输入线程，
避免对终端交互的硬依赖。
"""

import asyncio
import logging
import os
import sys
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
        """启动终端监听（stdin 非交互 / terminal.enabled: false 时跳过）"""
        cfg = getattr(self.framework, 'config', {}) or {}
        term_cfg = cfg.get('terminal', {}) or {}
        if term_cfg.get('enabled', True) is False:
            logger.info("终端交互已禁用（terminal.enabled: false）")
            return
        try:
            interactive = bool(sys.stdin) and sys.stdin.isatty()
        except Exception:
            interactive = False
        if not interactive:
            logger.info(
                "终端交互已跳过：stdin 非交互终端（CI/守护进程/重定向环境），"
                "终端命令仍可通过 API/IPC 触发"
            )
            return
        # terminal.panel_autostart：先进入面板（独占 stdin），退出后再拉起输入
        # 线程，二者不会同时读 stdin；面板随框架停机自动退出
        if term_cfg.get('panel_autostart'):
            threading.Thread(target=self._autostart_tui, daemon=True,
                             name="ops-tui-autostart").start()
            return
        self._start_input_thread()

    def _start_input_thread(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="terminal-input")
        self._thread.start()
        logger.info("终端交互已启动，输入 help 查看可用命令")

    def _autostart_tui(self):
        """启动终端面板（框架内置，失败回退普通终端输入）"""
        try:
            from framework.terminal import panel as panel_mod
            term_cfg = (getattr(self.framework, 'config', {}) or {}).get('terminal', {}) or {}
            panel = panel_mod.TerminalPanel(
                self.framework,
                refresh=float(term_cfg.get('panel_refresh', 1.0) or 1.0),
                default_view=str(term_cfg.get('panel_default_view', 'monitor') or 'monitor'))
            panel.run()
        except Exception as e:
            logger.warning(f"终端面板自启动失败，回退普通终端: {e}")
        self._start_input_thread()

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
                # 在事件循环中执行命令（同步等待：长时交互命令如 ops tui 面板
                # 会一直占用到此返回，期间本线程不再读 stdin，天然不与 TUI 抢键）
                if self.framework.loop and self.framework.loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._execute_command(line.strip()),
                        self.framework.loop
                    ).result()
            except EOFError:
                break
            except KeyboardInterrupt:
                break
            except (ValueError, OSError):
                # 外部杀进程瞬间 stdin/stdout/stderr 被拔，input() 抛
                # ValueError/OSError（如 "lost sys.stdin" / "I/O operation on
                # closed file"）。此时再调 logger.error 写 stderr 会再次失败
                # 并触发微秒级死循环（bug#9）。直接停止线程，避免刷屏与轮转。
                self._running = False
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
