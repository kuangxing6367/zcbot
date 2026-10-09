# -*- coding: utf-8 -*-
"""
定时任务调度器（唯一权威实现）
基于 APScheduler AsyncIOScheduler 实现 cron 任务调度
任务 handler 支持 async def（直接 await）和普通 def（转线程执行），不阻塞事件循环

单一权威实现（核心架构债务收敛）：
- 本模块是 TaskScheduler 的唯一权威实现；官方插件 core_plugins/scheduler
  从这里导入并注册为 services['scheduler'] 服务。旧导入路径
  framework.scheduler.TaskScheduler 与 core_plugins.scheduler.main.TaskScheduler
  指向同一个类，服务名 / ctx.task / tasks 表字段 / 对外调用面均不变。
- 启动语义以官方插件「等待事件循环就绪」的异步派发为准：register 发生在
  工作线程（asyncio.to_thread），主循环未必就绪，无运行循环时由守护线程
  等待就绪后线程安全地派发（在当前线程直接 start 无运行循环必报错）。
- 保留 pause_task / resume_task 兼容面（framework.api.tasks 等旧调用点依赖）。
- stop() 置停止标志并中断等待线程；停止后迟到的启动派发为 no-op，
  保证服务停用后不残留线程、不出现「已停止又被拉起」的僵尸调度器。
"""
import asyncio
import logging
import threading
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger('zcbot')


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self, framework):
        self.framework = framework
        self._scheduler = AsyncIOScheduler()
        self._plugin_tasks = {}
        self._waiting_boot = False
        self._stopped = False
        self._boot_abort = threading.Event()

    def start(self, loop=None):
        """启动调度器；插件注册发生在工作线程（asyncio.to_thread），主循环
        运行在主线程，AsyncIOScheduler.start 必须在事件循环线程上执行。
        loop 未就绪时由守护线程等待就绪后线程安全地派发。
        stop() 之后 start 为 no-op（防止等待线程迟到启动已停用的调度器）。"""
        if self._stopped:
            return
        if loop is None:
            loop = getattr(self.framework, 'loop', None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._start)
            return
        if self._waiting_boot:
            return
        self._waiting_boot = True

        def _wait():
            for _ in range(120):
                if self._stopped:
                    self._waiting_boot = False
                    return
                lp = getattr(self.framework, 'loop', None)
                if lp is not None and lp.is_running():
                    self._waiting_boot = False
                    lp.call_soon_threadsafe(self._start)
                    return
                # 等 0.5s 或被 stop() 中断（stop 时立即退出，不再等满 60s）
                if self._boot_abort.wait(0.5):
                    self._waiting_boot = False
                    return
            self._waiting_boot = False
            logger.error('scheduler 等待框架事件循环超时（60s），定时任务未启动')

        threading.Thread(target=_wait, daemon=True,
                         name='scheduler-boot-wait').start()

    def _start(self):
        if self._stopped:
            return
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("定时任务调度器已启动")

    def stop(self):
        """停止调度器：置停止标志（迟到派发 no-op）→ 中断等待线程 → 关闭调度器。
        幂等，可重复调用。"""
        self._stopped = True
        self._boot_abort.set()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("定时任务调度器已停止")

    def add_plugin_task(self, task: dict):
        """注册插件定时任务（job_id 为 `plugin:handler`，重复注册自动替换，
        同一任务不会因插件重载而重复堆积）"""
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
        """移除某插件的全部任务（冒号分隔的 job_id 前缀不会误伤同名前缀插件）"""
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

    # ---- 兼容调用面（framework.api.tasks 等旧 framework 侧调用点）----

    def pause_task(self, task_key: str):
        """暂停指定任务"""
        try:
            self._scheduler.pause_job(task_key)
            logger.debug(f"定时任务已暂停: {task_key}")
        except Exception as e:
            logger.warning(f"暂停任务失败 {task_key}: {e}")

    def resume_task(self, task_key: str):
        """恢复指定任务"""
        try:
            self._scheduler.resume_job(task_key)
            logger.debug(f"定时任务已恢复: {task_key}")
        except Exception as e:
            logger.warning(f"恢复任务失败 {task_key}: {e}")
