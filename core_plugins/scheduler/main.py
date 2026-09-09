"""
定时任务调度器（官方插件）
基于 APScheduler 的 cron 任务调度
"""
import asyncio
import logging
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "定时任务调度器",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "基于 APScheduler 的 cron 定时任务调度",
    "priority": 0,
    "official": True,
}


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self, framework):
        self.framework = framework
        self._scheduler = AsyncIOScheduler()
        self._plugin_tasks = {}

    def start(self, loop=None):
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("定时任务调度器已启动")

    def stop(self):
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("定时任务调度器已停止")

    def add_plugin_task(self, task: dict):
        """注册插件定时任务"""
        plugin_name = task['plugin_name']
        handler_name = task['handler_name']
        cron_expr = task['cron_expression']
        job_id = f"{plugin_name}:{handler_name}"

        try:
            parts = cron_expr.split()
            trigger_kwargs = {}
            if len(parts) >= 5:
                trigger_kwargs['minute'] = parts[0]
                trigger_kwargs['hour'] = parts[1]
                trigger_kwargs['day'] = parts[2]
                trigger_kwargs['month'] = parts[3]
                trigger_kwargs['day_of_week'] = parts[4]

            module = self.framework.plugin_loader.get_plugin_module(plugin_name)
            handler = getattr(module, handler_name, None) if module else None
            if handler is None:
                logger.warning(f"[{plugin_name}] 定时任务 handler 不存在: {handler_name}")
                return

            if asyncio.iscoroutinefunction(handler):
                async def _wrapper():
                    try:
                        await handler()
                    except Exception as e:
                        logger.error(f"[{plugin_name}] 定时任务异常: {handler_name} - {e}")
            else:
                def _wrapper():
                    try:
                        handler()
                    except Exception as e:
                        logger.error(f"[{plugin_name}] 定时任务异常: {handler_name} - {e}")

            self._scheduler.add_job(
                _wrapper, CronTrigger(**trigger_kwargs),
                id=job_id, replace_existing=True, misfire_grace_time=600)
            self._plugin_tasks[job_id] = task
        except Exception as e:
            logger.error(f"[{plugin_name}] 注册定时任务失败: {handler_name} - {e}")

    def remove_plugin_tasks(self, plugin_name: str):
        """移除某插件的全部任务"""
        to_remove = [jid for jid in self._plugin_tasks
                     if jid.startswith(f"{plugin_name}:")]
        for jid in to_remove:
            try:
                self._scheduler.remove_job(jid)
            except Exception:
                pass
            self._plugin_tasks.pop(jid, None)

    def remove_task(self, job_id: str):
        """移除单个任务"""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        self._plugin_tasks.pop(job_id, None)

    def get_jobs(self) -> list:
        """获取所有任务"""
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'next_run': str(job.next_run_time) if job.next_run_time else None,
            })
        return jobs


_scheduler = None


def register(ctx):
    """注册调度器为官方插件"""
    global _scheduler
    fw = ctx._framework

    sched_cfg = fw.config.get('scheduler', {})
    if sched_cfg.get('enabled') is False:
        ctx.log("调度器已禁用 (scheduler.enabled: false)")
        fw.services.register('scheduler', None)
        return

    _scheduler = TaskScheduler(fw)
    fw.services.register('scheduler', _scheduler)
    _scheduler.start()

    ctx.log("调度器已启动")


def unregister():
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
