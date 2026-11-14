"""定时任务执行模型单测：时区解析 / 同步 handler 不占事件循环 / 超时 / 慢任务 / cron 校验。

核心回归点：v1.19.0 之前 `scheduler` 的文档写「普通 def 转线程执行，不阻塞事件循环」，
但实现里同步 handler 是直接调用的 —— 一个 `requests.get` 就能冻住整个机器人。
本文件的第 2 个用例就是钉住这条。

铁律：路径用 __file__ 推导、不写死时区偏移、不污染真实数据目录。
"""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from framework.scheduler import TaskScheduler, describe_timezone, resolve_timezone


class FakeLoader:
    def __init__(self, module=None):
        self.module = module

    def get_plugin_module(self, name):
        return self.module


class FakeFramework:
    def __init__(self, module=None, config=None):
        self.config = {} if config is None else config
        self.plugin_loader = FakeLoader(module)
        self.loop = None


def _sched(module=None, **cfg):
    return TaskScheduler(FakeFramework(
        module=module, config={'scheduler': cfg} if cfg else None))


# ---------------------------------------------------------------- 时区

def test_timezone_local_is_none():
    """local / 空值表示沿用系统时区（APScheduler 默认），不能强行改成 Asia/Shanghai
    —— 那会静默改变所有老用户的既有任务时间。"""
    assert resolve_timezone('local') is None
    assert resolve_timezone('') is None
    assert resolve_timezone(None) is None
    assert resolve_timezone('  system  ') is None


def test_timezone_utc_and_iana():
    utc = resolve_timezone('utc')
    assert utc is not None
    assert 'UTC' in describe_timezone(utc)

    tz = resolve_timezone('Asia/Shanghai')
    if tz is None:
        # Windows 缺 tzdata：解析失败必须退回 local 而不是启动失败
        pytest.skip('当前环境无 tzdata，跳过 IANA 时区断言')
    desc = describe_timezone(tz)
    assert 'Asia/Shanghai' in desc
    assert 'UTC+08:00' in desc, desc


def test_timezone_garbage_falls_back():
    """拼错的时区名不能把调度器打死，退回本地时区即可"""
    assert resolve_timezone('Not/AZone') is None
    assert resolve_timezone('Mars/Olympus') is None


def test_describe_local_label():
    assert '本地' in describe_timezone(None)


# ---------------------------------------------------------------- 执行模型

def test_sync_handler_runs_off_event_loop_thread():
    """同步 handler 必须在线程池里跑 —— 这是 v1.19.0 修的真实缺陷"""
    sched = _sched()
    seen = {}

    def work():
        seen['thread'] = threading.get_ident()
        return 'ok'

    async def main():
        loop_thread = threading.get_ident()
        status = await sched._invoke(work, '[t] work')
        return loop_thread, status

    try:
        loop_thread, status = asyncio.run(main())
    finally:
        sched._shutdown_executor()

    assert status == 'ok'
    assert 'thread' in seen
    assert seen['thread'] != loop_thread, '同步 handler 仍跑在事件循环线程上'


def test_async_handler_stays_on_loop():
    """协程 handler 不该被丢进线程池（会丢失 contextvars 与 loop 亲和性）"""
    sched = _sched()
    seen = {}

    async def work():
        seen['thread'] = threading.get_ident()
        return 'ok'

    async def main():
        loop_thread = threading.get_ident()
        status = await sched._invoke(work, '[t] awork')
        return loop_thread, status

    try:
        loop_thread, status = asyncio.run(main())
    finally:
        sched._shutdown_executor()

    assert status == 'ok'
    assert seen['thread'] == loop_thread


def test_timeout_marks_status_and_is_not_blocking():
    """超时要被记成 timeout，而不是当成成功"""
    sched = _sched(task_timeout=0.2)

    def slow():
        time.sleep(0.6)

    async def main():
        return await sched._invoke(slow, '[t] slow')

    try:
        started = time.monotonic()
        status = asyncio.run(main())
        elapsed = time.monotonic() - started
    finally:
        sched._shutdown_executor()

    assert status == 'timeout'
    assert elapsed < 0.55, f'超时后应立即返回，实际等了 {elapsed:.2f}s'
    assert sched._task_records['[t] slow']['last_status'] == 'timeout'
    assert sched.stats()['timeouts'] == 1


def test_fractional_threshold_kept():
    """小数超时不能被 int() 截成 0（0 的语义是「不限」= 完全失效）"""
    sched = _sched(task_timeout=0.5, slow_task_threshold=0.25)
    assert sched.task_timeout == 0.5
    assert sched.slow_threshold == 0.25


def test_slow_task_counted():
    sched = _sched(slow_task_threshold=0.05, task_timeout=5)

    def work():
        time.sleep(0.12)

    async def main():
        return await sched._invoke(work, '[t] slowish')

    try:
        status = asyncio.run(main())
    finally:
        sched._shutdown_executor()

    assert status == 'ok'
    stats = sched.stats()
    assert stats['slow'] == 1, stats
    rec = sched._task_records['[t] slowish']
    assert rec['last_duration_ms'] >= 100
    assert rec['runs'] == 1


def test_handler_exception_counted_not_raised():
    sched = _sched()

    def boom():
        raise ValueError('炸了')

    async def main():
        return await sched._invoke(boom, '[t] boom')

    try:
        status = asyncio.run(main())
    finally:
        sched._shutdown_executor()

    assert status == 'error'
    assert sched.stats()['failures'] == 1
    rec = sched._task_records['[t] boom']
    assert '炸了' in (rec.get('last_error') or '')


# ---------------------------------------------------------------- 注册

def test_bad_cron_rejected_without_raising():
    module = type(sys)('fake_plugin')
    module.job = lambda: None
    sched = _sched(module=module)

    sched.add_plugin_task({'plugin_name': 'p', 'handler_name': 'job',
                           'cron_expression': '0 9 *'})     # 3 段，非法
    sched.add_plugin_task({'plugin_name': 'p', 'handler_name': 'job',
                           'cron_expression': ''})
    assert sched.get_jobs() == []
    assert sched._plugin_tasks == {}


def test_missing_handler_rejected():
    module = type(sys)('fake_plugin')      # 没有 job 属性
    sched = _sched(module=module)
    sched.add_plugin_task({'plugin_name': 'p', 'handler_name': 'job',
                           'cron_expression': '0 9 * * *'})
    assert sched._plugin_tasks == {}


def test_register_records_cron_and_timezone():
    module = type(sys)('fake_plugin')
    module.job = lambda: None
    sched = _sched(module=module, timezone='utc')

    sched.add_plugin_task({'plugin_name': 'p', 'handler_name': 'job',
                           'cron_expression': '0 9 * * *'})
    assert len(sched._plugin_tasks) == 1
    task = sched._plugin_tasks['p:job']
    assert task['record']['cron'] == '0 9 * * *'
    # 任务实际挂在 scheduler 上（未 start 时 get_job 可能为空，不强求）
    assert sched.stats()['jobs'] == 1


def test_remove_task_drops_record():
    module = type(sys)('fake_plugin')
    module.job = lambda: None
    sched = _sched(module=module)
    sched.add_plugin_task({'plugin_name': 'p', 'handler_name': 'job',
                           'cron_expression': '0 9 * * *'})
    assert 'p:job' in sched._plugin_tasks

    sched.remove_plugin_tasks('p')
    assert sched._plugin_tasks == {}
    assert sched._task_records == {}, '任务移除后执行记录也要清掉，否则面板留僵尸条目'


def test_executor_shutdown_idempotent():
    sched = _sched()
    sched._ensure_executor()
    assert sched._executor is not None
    sched.stop()
    assert sched._executor is None
    sched.stop()          # 幂等
    assert sched._executor is None


# ---------------------------------------------------------------- 配置兜底

def test_config_missing_uses_defaults():
    """老用户 config.yaml 里没有 scheduler 段（或只有 enabled）时按默认值跑"""
    sched = TaskScheduler(FakeFramework(config={}))
    assert sched.task_timeout == 300
    assert sched.slow_threshold == 10
    assert sched.timezone is None
    assert sched.describe()['workers'] == 4

    sched2 = TaskScheduler(FakeFramework(config={'scheduler': {'enabled': True}}))
    assert sched2.task_timeout == 300


def test_invalid_config_values_fall_back():
    sched = _sched(task_timeout='abc', slow_task_threshold=-5, workers='x')
    assert sched.task_timeout == 300
    assert sched.slow_threshold == 10
    assert sched.describe()['workers'] == 4
