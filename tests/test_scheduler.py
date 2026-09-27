# -*- coding: utf-8 -*-
"""scheduler 官方插件测试：start 必须落在事件循环线程（压测轮"启动必报错"防回归）"""
import asyncio

from core_plugins.scheduler.main import TaskScheduler


class _FW:
    def __init__(self):
        self.loop = None
        self.plugin_loader = None


def test_scheduler_start_waits_for_loop_thread():
    """无 loop 上下文调用 start()（register 工作线程的真实时序）不得报错，
    且 loop 就绪后调度器必须在 loop 线程上完成启动。
    修复前：兜底路径在当前线程直接 AsyncIOScheduler.start()，无运行循环必炸。"""
    fw = _FW()
    sched = TaskScheduler(fw)
    sched.start()                     # 此时 fw.loop=None → 走等待线程，不报错

    async def run():
        async def set_loop():
            await asyncio.sleep(0.2)
            fw.loop = asyncio.get_running_loop()
        asyncio.create_task(set_loop())

        async def poll():
            for _ in range(80):
                if sched._scheduler.running:
                    return
                await asyncio.sleep(0.05)
            raise TimeoutError('scheduler 未在 loop 就绪后启动')

        await asyncio.wait_for(poll(), timeout=6.0)
        sched.stop()          # 在 loop 存活时停（shutdown 需要触碰事件循环）

    asyncio.run(run())
    assert not sched._scheduler.running


def test_scheduler_start_on_running_loop():
    """loop 已运行时 start() 直接线程安全派发，立即启动。"""
    fw = _FW()

    async def run():
        fw.loop = asyncio.get_running_loop()
        sched = TaskScheduler(fw)
        sched.start()
        for _ in range(40):
            if sched._scheduler.running:
                break
            await asyncio.sleep(0.05)
        assert sched._scheduler.running
        sched.stop()

    asyncio.run(run())
