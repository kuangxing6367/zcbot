# -*- coding: utf-8 -*-
"""framework.core —— 框架核心引擎包

旧路径兼容：
  from framework.core import Framework, AsyncStatsWriter  # 不变
  from framework.core.dispatch import FrameworkDispatchMixin  # → framework.core.dispatch
  from framework.core.runtime import FrameworkRuntimeMixin    # → framework.core.runtime
"""
from framework.core.base import Framework  # noqa: F401
from framework.core.stats_writer import AsyncStatsWriter  # noqa: F401  向后兼容 re-export
from framework.core.dispatch import FrameworkDispatchMixin  # noqa: F401
from framework.core.runtime import FrameworkRuntimeMixin  # noqa: F401

__all__ = ['Framework', 'AsyncStatsWriter', 'FrameworkDispatchMixin', 'FrameworkRuntimeMixin']
