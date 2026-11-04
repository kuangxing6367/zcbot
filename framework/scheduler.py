# -*- coding: utf-8 -*-
"""
定时任务调度器（唯一权威实现）
基于 APScheduler AsyncIOScheduler 实现 cron 任务调度

执行模型（v1.19.0 起与命令 handler 对齐，两条路径同一套标准）：
- async def handler：在当前事件循环 await，受 `scheduler.task_timeout` 约束；
- 普通 def handler：**转专用线程池执行**，不占事件循环；同样受超时约束。
  （v1.19.0 之前文档这么写但实现没做，同步 handler 直接跑在循环线程上，
   一个 requests.get 就能冻住整个机器人，且日志里看不出任何异常。）
- 超时只是「不再等待」，Python 无法强杀线程：超时后任务记为 timeout，
  线程仍在后台跑完。要真正可中断必须写 async + 内部支持取消。
- 每次执行记录耗时与状态；超过 `slow_task_threshold` 打 warning，
  供「定时任务到底慢在哪」直接定位，而不是靠猜。

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
import functools
import logging
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from datetime import timezone as _dt_timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger('zcbot')


def resolve_timezone(spec):
    """把配置里的时区写法解析成 tzinfo；返回 None 表示沿用系统本地时区。

    支持 `local`（默认，保持历史行为）/ `utc` / IANA 名（`Asia/Shanghai`）。
    解析失败不抛错：告警并退回本地时区——调度器不该因为一个拼错的时区名而拒绝启动。
    """
    text = (spec or 'local')
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or text.lower() in ('local', 'system', 'default'):
        return None
    if text.upper() == 'UTC':
        return _dt_timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(text)
    except Exception as e:
        # Windows 无 tzdata 时 ZoneInfo 会抛 ZoneInfoNotFoundError，给出可执行的修法
        hint = ''
        if e.__class__.__name__ == 'ZoneInfoNotFoundError':
            hint = '（Windows 需先 pip install tzdata，或改用 local/utc）'
        logger.warning(f"定时任务时区 '{text}' 无法解析{hint}，退回系统本地时区: {e}")
        return None


def describe_timezone(tz) -> str:
    """给日志/接口用的时区描述，让「差 8 小时」一眼可见。

    tz 为 None 时也要给出本地偏移：只说「本地时区」等于没说 ——
    部署到 TZ=UTC 的容器里，`0 9 * * *` 会在北京时间 17:00 执行，
    用户看日志依然是「本地时区」，完全发现不了。
    """
    if tz is None:
        try:
            from datetime import datetime
            local = datetime.now().astimezone()
            total = int((local.utcoffset() or 0).total_seconds())
            sign = '+' if total >= 0 else '-'
            total = abs(total)
            return f'本地时区 (UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d})'
        except Exception:
            return '本地时区'
    name = getattr(tz, 'key', None) or getattr(tz, 'tzname', None) or str(tz)
    offset = None
    try:
        from datetime import datetime
        offset = datetime.now(tz).utcoffset()
    except Exception:
        offset = None
    if offset is None:
        return str(name)
    total = int(offset.total_seconds())
    sign = '+' if total >= 0 else '-'
    total = abs(total)
    return f"{name} (UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d})"


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self, framework):
        self.framework = framework
        self._cfg = self._read_config()
        self._tz = resolve_timezone(self._cfg.get('timezone'))
        self._scheduler = AsyncIOScheduler(timezone=self._tz)
        self._plugin_tasks = {}
        self._waiting_boot = False
        self._stopped = False
        self._boot_abort = threading.Event()
        self._executor = None
        # label -> 执行记录（最近一次耗时/状态 + 累计次数），供 get_jobs 与面板展示
        self._task_records = {}
        self._stats = {'runs': 0, 'failures': 0, 'timeouts': 0, 'slow': 0,
                       'max_duration_ms': 0}

    # ---- 配置 ----

    def _read_config(self) -> dict:
        """读 scheduler 段。框架未注入 config（单测/早期阶段）时按默认值跑。"""
        try:
            cfg = (self.framework.config or {}).get('scheduler') or {}
        except Exception:
            cfg = {}
        return dict(cfg) if isinstance(cfg, dict) else {}

    def _cfg_num(self, key, default):
        """取非负数值配置；非法值告警并回退默认。

        用 float 而不是 int：超时/阈值常写成 0.5s 这类小数，
        int() 会把它截成 0，而 0 在这里的语义是「不限」，等于静默失效。
        """
        try:
            value = float(self._cfg.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"scheduler.{key} 配置非数值，按默认值 {default} 处理")
            return float(default)
        return value if value >= 0 else float(default)

    @property
    def task_timeout(self) -> float:
        """单任务执行超时（秒），0 = 不限。与命令 handler 的熔断语义一致。"""
        return self._cfg_num('task_timeout', 300)

    @property
    def slow_threshold(self) -> float:
        """慢任务告警阈值（秒），0 = 不告警。"""
        return self._cfg_num('slow_task_threshold', 10)

    @property
    def timezone(self):
        """解析后的 tzinfo；None 表示系统本地时区。"""
        return self._tz

    def describe(self) -> dict:
        """供启动日志与 Web 面板展示的实际调度参数"""
        return {
            'timezone': describe_timezone(self._tz),
            'task_timeout': self.task_timeout,
            'slow_task_threshold': self.slow_threshold,
            'workers': max(1, int(self._cfg_num('workers', 4))),
            'running': self._scheduler.running,
            'stats': dict(self._stats),
        }

    def _ensure_executor(self):
        """同步 handler 专用线程池：独立于默认 executor，避免长任务占满
        事件循环的默认线程池（to_thread 用的那个）而拖慢命令处理。"""
        if self._executor is None:
            workers = max(1, int(self._cfg_num('workers', 4)))
            self._executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix='zcbot-task')
            logger.debug(f"定时任务线程池已创建: workers={workers}")
        return self._executor

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
            # 时区必须打出来：容器里 TZ 默认 UTC，写到 09:00 的任务会变成 17:00 执行，
            # 而日志里此前没有任何线索能看出用的是哪个时区。
            logger.info(f"定时任务时区: {describe_timezone(self._tz)}"
                        f"（配置 scheduler.timezone，默认 local）")

    def stop(self):
        """停止调度器：置停止标志（迟到派发 no-op）→ 中断等待线程 → 关闭调度器。
        幂等，可重复调用。"""
        self._stopped = True
        self._boot_abort.set()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("定时任务调度器已停止")
        self._shutdown_executor()

    def _shutdown_executor(self):
        """关闭任务线程池；正在跑的任务允许跑完（不等，避免卡住退出）。"""
        ex, self._executor = self._executor, None
        if ex is not None:
            try:
                ex.shutdown(wait=False)
            except Exception as e:
                logger.debug(f"关闭定时任务线程池失败: {e}")

    # ---- 执行（唯一入口：插件任务与内置任务共用）----

    async def run_blocking(self, func, *args, **kwargs):
        """把同步可调用丢到任务线程池执行并 await 结果。

        内置任务（如权限过期清理）与同步插件 handler 都走这里，
        避免直接占用事件循环线程。
        """
        loop = asyncio.get_running_loop()
        ex = self._ensure_executor()
        return await loop.run_in_executor(ex, functools.partial(func, *args, **kwargs))

    async def _invoke(self, func, label: str):
        """统一的执行入口：协程 await、同步转线程、超时、耗时统计、慢告警。

        label 形如 `[插件名] handler名` / `[内置] 任务名`，出现在所有日志里，
        保证「谁的定时任务出问题」不需要靠猜。
        """
        started = time.monotonic()
        timeout = self.task_timeout
        status = 'ok'
        error = None
        try:
            if asyncio.iscoroutinefunction(func):
                coro = func()
                if timeout:
                    await asyncio.wait_for(coro, timeout=timeout)
                else:
                    await coro
            else:
                fut = self.run_blocking(func)
                if timeout:
                    await asyncio.wait_for(fut, timeout=timeout)
                else:
                    await fut
        except (asyncio.TimeoutError, TimeoutError):
            status = 'timeout'
            error = f'超时（>{timeout:g}s）'
            self._stats['timeouts'] += 1
            logger.error(f"{label} 定时任务执行超时（>{timeout:g}s）；"
                         f"注意：超时只是不再等待，线程仍会在后台跑完")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status = 'error'
            error = str(e)
            self._stats['failures'] += 1
            logger.error(f"{label} 定时任务异常: {e}", exc_info=True)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._stats['runs'] += 1
            if duration_ms > self._stats['max_duration_ms']:
                self._stats['max_duration_ms'] = duration_ms
            if status == 'ok' and self.slow_threshold and \
                    duration_ms >= self.slow_threshold * 1000:
                self._stats['slow'] += 1
                logger.warning(
                    f"{label} 定时任务执行缓慢: {duration_ms}ms"
                    f"（阈值 {self.slow_threshold:g}s）")
            # 回写到任务记录（任务可能已被卸载移除；缺失时补一条，便于排查
            # 「注册失败但仍被执行」这类异常状态）
            rec = self._task_records.setdefault(label, {'runs': 0})
            rec.update({
                'last_run': time.strftime('%Y-%m-%d %H:%M:%S'),
                'last_duration_ms': duration_ms,
                'last_status': status,
                'last_error': error,
            })
            rec['runs'] = rec.get('runs', 0) + 1
        return status

    def add_plugin_task(self, task: dict):
        """注册插件定时任务（job_id 为 `plugin:handler`，重复注册自动替换，
        同一任务不会因插件重载而重复堆积）"""
        plugin_name = task['plugin_name']
        handler_name = task['handler_name']
        cron_expr = task['cron_expression']
        job_id = f"{plugin_name}:{handler_name}"

        try:
            parts = (cron_expr or '').split()
            if len(parts) != 5:
                logger.error(
                    f"[{plugin_name}] 定时任务 cron 表达式必须是 5 段"
                    f"（分 时 日 月 周），实际 {len(parts)} 段: {cron_expr!r}")
                return
            trigger_kwargs = {
                'minute': parts[0],
                'hour': parts[1],
                'day': parts[2],
                'month': parts[3],
                'day_of_week': parts[4],
            }

            module = self.framework.plugin_loader.get_plugin_module(plugin_name)
            handler = getattr(module, handler_name, None) if module else None
            if handler is None:
                logger.warning(f"[{plugin_name}] 定时任务 handler 不存在: {handler_name}")
                return

            label = f"[{plugin_name}] {handler_name}"
            record = self._task_records.setdefault(label, {'runs': 0})
            record.update({'cron': cron_expr, 'plugin': plugin_name,
                           'handler': handler_name, 'job_id': job_id})

            async def _wrapper(_fn=handler, _label=label):
                await self._invoke(_fn, _label)

            trigger = CronTrigger(**trigger_kwargs, timezone=self._tz)
            self._scheduler.add_job(
                _wrapper, trigger,
                id=job_id, replace_existing=True, misfire_grace_time=600)
            task = dict(task)
            task['record'] = record
            task['next_run'] = None
            self._plugin_tasks[job_id] = task
            try:
                job = self._scheduler.get_job(job_id)
                nxt = getattr(job, 'next_run_time', None) if job else None
                task['next_run'] = str(nxt) if nxt else None
            except Exception:
                pass
            logger.debug(f"[{plugin_name}] 定时任务已注册: {cron_expr} -> "
                         f"{handler_name}（下次 {task['next_run']}）")
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
            self._forget_task(self._plugin_tasks.pop(jid, None))

    def remove_task(self, job_id: str):
        """移除单个任务"""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        self._forget_task(self._plugin_tasks.pop(job_id, None))

    def _forget_task(self, task):
        """任务移除后一并丢掉执行记录，避免面板上留着已卸载插件的僵尸条目"""
        if not task:
            return
        rec = task.get('record')
        if isinstance(rec, dict):
            for key, value in list(self._task_records.items()):
                if value is rec:
                    self._task_records.pop(key, None)
                    break

    def get_jobs(self) -> list:
        """获取所有任务（含下次执行时间、最近一次耗时与状态）"""
        jobs = []
        for job in self._scheduler.get_jobs():
            item = {
                'id': job.id,
                'next_run': str(job.next_run_time) if job.next_run_time else None,
            }
            task = self._plugin_tasks.get(job.id)
            if task is not None:
                item['plugin'] = task.get('plugin_name')
                item['handler'] = task.get('handler_name')
                item['cron'] = task.get('cron_expression')
            rec = (task or {}).get('record')
            if isinstance(rec, dict):
                for key in ('last_run', 'last_duration_ms', 'last_status',
                            'last_error', 'runs'):
                    if key in rec:
                        item[key] = rec[key]
            jobs.append(item)
        return jobs

    def stats(self) -> dict:
        """调度器运行统计（供 /metrics 与运行时面板）"""
        data = dict(self._stats)
        data['timezone'] = describe_timezone(self._tz)
        data['running'] = self._scheduler.running
        data['jobs'] = len(self._plugin_tasks)
        return data

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
