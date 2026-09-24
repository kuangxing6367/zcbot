"""内置终端命令

register_builtins(fw) 向全局注册表注册 help / status / send / ... 等命令。
命令按域拆分到同包子模块：cmd_core / cmd_plugin / cmd_msg / cmd_info / cmd_update。
"""
from .cmd_core import register as _reg_core
from .cmd_plugin import register as _reg_plugin
from .cmd_msg import register as _reg_msg
from .cmd_info import register as _reg_info
from .cmd_update import register as _reg_update
from .helper import installed_core_plugins  # noqa: F401  向后兼容 re-export


def register_builtins(fw):
    """注册内置终端命令（编排各域子模块）"""
    _reg_core(fw)
    _reg_plugin(fw)
    _reg_msg(fw)
    _reg_info(fw)
    _reg_update(fw)
