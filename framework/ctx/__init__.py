# -*- coding: utf-8 -*-
"""framework.ctx —— 插件上下文包

旧路径兼容：from framework.ctx import PluginContext  # 不变
"""
from framework.ctx.base import PluginContext  # noqa: F401
from framework.ctx.messaging import PluginMessagingMixin  # noqa: F401
from framework.ctx.events import PluginEventsMixin  # noqa: F401
from framework.ctx.webui import PluginWebuiMixin  # noqa: F401
from framework.ctx.db import PluginDatabaseMixin  # noqa: F401

__all__ = [
    'PluginContext',
    'PluginMessagingMixin', 'PluginEventsMixin', 'PluginWebuiMixin', 'PluginDatabaseMixin',
]
