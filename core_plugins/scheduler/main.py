# -*- coding: utf-8 -*-
"""
定时任务调度器（官方插件）—— 薄封装
权威实现已收敛到 framework/scheduler.py（TaskScheduler 唯一实现），本模块只负责：
1. 以官方插件形态把权威实现注册为 services['scheduler']（服务名 / 加载语义不变）；
2. 兼容再导出 TaskScheduler，旧导入路径 core_plugins.scheduler.main.TaskScheduler 不变。
"""
import logging

from framework.scheduler import TaskScheduler  # noqa: F401  权威实现 + 兼容再导出

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "定时任务调度器",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "基于 APScheduler 的 cron 定时任务调度",
    "priority": 0,
    "official": True,
    "process": "host",
}

_scheduler = None
_fw = None


def register(ctx):
    """注册调度器为官方插件"""
    global _scheduler, _fw
    fw = ctx._framework
    _fw = fw

    # 防御：重复 register（插件重载）先停旧实例，避免旧等待线程 / 调度器残留
    if _scheduler is not None:
        try:
            _scheduler.stop()
        except Exception:
            pass
        _scheduler = None

    sched_cfg = fw.config.get('scheduler', {})
    if sched_cfg.get('enabled') is False:
        ctx.log("调度器已禁用 (scheduler.enabled: false)")
        fw.services.register('scheduler', None)
        return

    _scheduler = TaskScheduler(fw)
    fw.services.register('scheduler', _scheduler)
    _scheduler.start()

    ctx.log("调度器已注册")


def unregister():
    global _scheduler, _fw
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
    # 同步摘除服务注册：停用后 framework.scheduler 为 None（调用方均按 None 跳过）
    fw, _fw = _fw, None
    if fw is not None:
        try:
            fw.services.remove('scheduler')
        except Exception:
            pass
