"""内置终端命令

register_builtins(fw) 向全局注册表注册 help / status / send / ... 等命令。
命令按域拆分到同包子模块：cmd_core / cmd_plugin / cmd_msg / cmd_info / cmd_update /
cmd_ops（运维扩展：restart / shell / dbdump，原 core_plugins/ops 迁入）。
"""
from .cmd_core import register as _reg_core
from .cmd_plugin import register as _reg_plugin
from .cmd_msg import register as _reg_msg
from .cmd_info import register as _reg_info
from .cmd_update import register as _reg_update
from .cmd_ops import register as _reg_ops
from .panel import register as _reg_panel
from .helper import installed_core_plugins  # noqa: F401  向后兼容 re-export


def register_builtins(fw):
    """注册内置终端命令（编排各域子模块）"""
    _reg_core(fw)
    _reg_plugin(fw)
    _reg_msg(fw)
    _reg_info(fw)
    _reg_update(fw)
    _reg_ops(fw)
    _reg_panel(fw)
