# -*- coding: utf-8 -*-
"""命令处理器聚合（供 framework.cli 按 group 名分发）。"""
from .project import do_create, do_init
from .run import do_run
from .generate import do_generate
from .plugin import do_plugin
from .adapter import do_adapter
from .driver import do_driver
from .self import do_self
from .clean import do_clean
from .config import do_config

__all__ = [
    "do_create", "do_init", "do_run", "do_generate", "do_plugin",
    "do_adapter", "do_driver", "do_self", "do_clean", "do_config",
]
