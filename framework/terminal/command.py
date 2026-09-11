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
        self._targets = {}   # name -> 'core' | 'host' | 'both'

    def register(self, name: str, handler, description: str = "", aliases: list = None,
                 target: str = 'core'):
        """注册终端命令

        :param target: 命令归属进程，双进程模式下生效：
            - 'core'（默认）：在本进程（核心）执行即可；
            - 'host'：状态在宿主进程（用户插件 / 调度器），需转发到宿主执行；
            - 'both'：核心与宿主都要跑一次（如 status/plugins 需要合并两侧视图）。
            单进程模式下所有命令一律本地执行。
        """
        self._commands[name] = handler
        self._descriptions[name] = description
        self._targets[name] = target if target in ('core', 'host', 'both') else 'core'
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

    def _resolve(self, name: str):
        """把命令名/别名解析为正式命令名（不存在返回 None）"""
        if name in self._commands:
            return name
        real_name = self._aliases.get(name)
        return real_name if real_name in self._commands else None

    def get_target(self, name: str) -> str:
        """获取命令归属进程（'core' | 'host' | 'both'）"""
        real = self._resolve(name)
        return self._targets.get(real, 'core') if real else 'core'

    def list_commands(self) -> dict:
        """列出所有命令"""
        result = {}
        for name, handler in self._commands.items():
            result[name] = self._descriptions.get(name, "")
        return result

    def help_text(self) -> str:
        """生成帮助文本"""
        marker = {'host': ' [宿主进程]', 'both': ' [核心+宿主]'}
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
            tag = marker.get(self._targets.get(name, 'core'), '')
            lines.append(f"  {name}{alias_str}: {desc}{tag}")
        lines.append("-" * 50)
        lines.append("用法: 命令名 参数，如: send 123456 你好")
        lines.append("注: 双进程模式下，[宿主进程] 命令会转发到宿主进程执行")
        return "\n".join(lines)


# 全局终端命令注册表
terminal_commands = TerminalCommand()
