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
        """执行终端命令"""
        parts = line.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler = terminal_commands.get(cmd_name)
        if handler:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(args)
                else:
                    await asyncio.to_thread(handler, args)
            except Exception as e:
                logger.error(f"终端命令 [{cmd_name}] 执行失败: {e}")
        else:
            logger.warning(f"未知命令: {cmd_name}，输入 help 查看可用命令")
