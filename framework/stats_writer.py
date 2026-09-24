# -*- coding: utf-8 -*-
"""framework.stats_writer —— 兼容 shim

AsyncStatsWriter 已移入 framework/core/stats_writer.py。
旧导入路径 from framework.stats_writer import AsyncStatsWriter 保持有效。
"""
from framework.core.stats_writer import AsyncStatsWriter  # noqa: F401

__all__ = ['AsyncStatsWriter']
