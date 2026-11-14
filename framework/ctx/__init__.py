"""framework.ctx —— 插件上下文包

旧路径兼容：from framework.ctx import PluginContext  # 不变
"""
from framework.ctx.base import PluginContext
from framework.ctx.db import PluginDatabaseMixin
from framework.ctx.events import PluginEventsMixin
from framework.ctx.messaging import PluginMessagingMixin
from framework.ctx.webui import PluginWebuiMixin

__all__ = [
    'PluginContext',
    'PluginDatabaseMixin',
    'PluginEventsMixin',
    'PluginMessagingMixin',
    'PluginWebuiMixin',
]
