"""终端交互模块

将控制台输入命令直接作用于框架，例如 status、plugins、reload 等。
对外暴露: TerminalInput / terminal_commands / register_builtins。
"""

from .command import TerminalCommand, terminal_commands
from .input import TerminalInput
from .builtins import register_builtins

__all__ = ['TerminalCommand', 'terminal_commands', 'TerminalInput', 'register_builtins']
