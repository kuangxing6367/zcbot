"""framework.core —— 框架核心引擎包

旧路径兼容：
  from framework.core import Framework, AsyncStatsWriter  # 不变
  from framework.core.dispatch import FrameworkDispatchMixin  # → framework.core.dispatch
  from framework.core.runtime import FrameworkRuntimeMixin    # → framework.core.runtime
"""
from framework.core.base import Framework
from framework.core.dispatch import FrameworkDispatchMixin
from framework.core.runtime import FrameworkRuntimeMixin
from framework.core.stats_writer import AsyncStatsWriter

__all__ = ['AsyncStatsWriter', 'Framework', 'FrameworkDispatchMixin', 'FrameworkRuntimeMixin']
