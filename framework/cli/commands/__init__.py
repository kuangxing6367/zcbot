"""命令处理器聚合（供 framework.cli 按 group 名分发）。"""
from .adapter import do_adapter
from .clean import do_clean
from .config import do_config
from .driver import do_driver
from .generate import do_generate
from .plugin import do_plugin
from .project import do_create, do_init
from .run import do_run
from .self import do_self

__all__ = [
    "do_adapter",
    "do_clean",
    "do_config",
    "do_create",
    "do_driver",
    "do_generate",
    "do_init",
    "do_plugin",
    "do_run",
    "do_self",
]
