"""终端命令注册表

定义命令注册表 TerminalCommand 与全局单例 terminal_commands。
"""

import logging

logger = logging.getLogger('zcbot')


class TerminalCommand:
    """终端命令注册表"""

    def __init__(self):
        self._commands = {}  # name -> handler
        self._aliases = {}   # alias -> name
        self._descriptions = {}  # name -> description

    def register(self, name: str, handler, description: str = "", aliases: list = None):
        """注册终端命令"""
        self._commands[name] = handler
        self._descriptions[name] = description
        if aliases:
            for alias in aliases:
                self._aliases[alias] = name

    def get(self, name: str):
        """获取命令处理器"""
        # 先查直接命令名
        if name in self._commands:
            return self._commands[name]
        # 再查别名
        real_name = self._aliases.get(name)
        if real_name and real_name in self._commands:
            return self._commands[real_name]
        return None

    def list_commands(self) -> dict:
        """列出所有命令"""
        result = {}
        for name, handler in self._commands.items():
            result[name] = self._descriptions.get(name, "")
        return result

    def help_text(self) -> str:
        """生成帮助文本"""
        lines = ["可用终端命令:"]
        lines.append("-" * 50)
        for name, handler in sorted(self._commands.items()):
            alias_str = ""
            for alias, real_name in self._aliases.items():
                if real_name == name:
                    alias_str = f" ({alias})"
                    break
            desc = self._descriptions.get(name, "")
            if not desc and hasattr(handler, '__doc__'):
                desc = handler.__doc__.strip().split('\n')[0] if handler.__doc__ else ""
            lines.append(f"  {name}{alias_str}: {desc}")
        lines.append("-" * 50)
        lines.append("用法: 命令名 参数，如: send 123456 你好")
        return "\n".join(lines)


# 全局终端命令注册表
terminal_commands = TerminalCommand()
