"""
定时任务调度器
基于 APScheduler 实现 cron 任务调度
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger('zcbot')


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self, framework):
        self.framework = framework
        self._scheduler = BackgroundScheduler()
        self._plugin_tasks = {}  # task_id -> task_info

    def start(self):
        """启动调度器"""
        self._scheduler.start()
        logger.info("定时任务调度器已启动")

    def stop(self):
        """停止调度器"""
        self._scheduler.shutdown(wait=False)
        logger.info("定时任务调度器已停止")

    def add_plugin_task(self, task_info: dict):
        """
        添加插件定时任务
        :param task_info: {id, plugin_name, cron_expression, handler, handler_name, description}
        """
        task_id = f"plugin_{task_info['plugin_name']}_{task_info['id']}"
        cron_expr = task_info['cron_expression']

        # 解析 cron 表达式（5字段）
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            logger.error(f"cron表达式格式错误: {cron_expr}")
            return

        try:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone='Asia/Shanghai'
            )
        except Exception as e:
            logger.error(f"cron表达式解析失败: {cron_expr} - {e}")
            return

        # 获取插件模块的 handler 函数
        plugin_name = task_info['plugin_name']
        handler_name = task_info['handler_name']
        module = self.framework.plugin_loader.get_plugin_module(plugin_name)

        if module is None:
            logger.error(f"添加任务失败: 插件 [{plugin_name}] 未加载")
            return

        handler = getattr(module, handler_name, None)
        if handler is None or not callable(handler):
            logger.error(f"添加任务失败: 函数 {handler_name} 在 [{plugin_name}] 中不存在或不可调用")
            return

        self._scheduler.add_job(
            func=self._wrap_handler(handler, plugin_name),
            trigger=trigger,
            id=task_id,
            name=task_info.get('description', ''),
            replace_existing=True
        )

        self._plugin_tasks[task_id] = task_info
        logger.info(f"定时任务已注册: [{plugin_name}] {cron_expr} → {handler_name}")

    def _wrap_handler(self, handler, plugin_name: str):
        """包装 handler，捕获异常并记录执行状态"""
        def wrapper():
            try:
                logger.debug(f"定时任务执行: [{plugin_name}] {handler.__name__}")
                handler()
                self._update_task_status(plugin_name, handler.__name__, 'success')
            except Exception as e:
                logger.error(f"定时任务异常: [{plugin_name}] {handler.__name__} - {e}")
                self._update_task_status(plugin_name, handler.__name__, 'error')
        return wrapper

    def _update_task_status(self, plugin_name: str, handler_name: str, status: str):
        """更新任务执行状态"""
        try:
            now = datetime.now()
            self.framework.db.execute(
                "UPDATE tasks SET last_run_at=%s, run_count=run_count+1, last_status=%s "
                "WHERE plugin_name=%s AND handler=%s",
                (now, status, plugin_name, handler_name)
            )
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")

    def remove_plugin_tasks(self, plugin_name: str):
        """移除某插件的所有任务"""
        to_remove = [tid for tid, info in self._plugin_tasks.items()
                     if info['plugin_name'] == plugin_name]
        for tid in to_remove:
            try:
                self._scheduler.remove_job(tid)
                self._plugin_tasks.pop(tid, None)
            except Exception:
                pass

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