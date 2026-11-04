# -*- coding: utf-8 -*-
"""
事件循环卡顿看门狗

存在的理由：asyncio 单线程模型下，任何一处阻塞调用（`requests.get` 没写 timeout、
`time.sleep`、大批量同步 IO、正则灾难性回溯）都会让**整个机器人**停摆 ——
所有插件、所有接入端、定时任务一起停。而此时日志里通常什么都没有：
没有异常、没有报错，只有消息不回。用户看到的现象是「机器人死了」，
排查却要从几千行插件代码里靠猜。

本模块常驻一个守护线程，周期性把回调投递到事件循环并测量「从投递到执行」的延迟：

- 循环空闲 → 延迟通常在 1ms 以内；
- 循环被占 → 回调排队，延迟 ≈ 被占用时长；
- 循环彻底死掉 → 回调永远不执行，线程按 `stall_wait` 超时判定为卡死。

卡死时会额外抓取事件循环线程的栈顶若干帧，直接指出卡在哪个文件哪一行 ——
这是「阻塞」唯一能在事后被定位的方式（Python 无法中断同步阻塞）。

设计约束：
- 线程 daemon + 全部异常吞掉：看门狗本身绝不能影响被观测进程；
- 每次卡顿只在**开始**和**恢复**各打一条日志，不刷屏；
- 恢复后统计累积到 `stats()`，接入 `/metrics` 提供趋势（偶发 vs 持续性）；
- loop 未就绪时等待就绪，不在启动早期制造误报。
"""
import asyncio
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger('zcbot')

# 栈里这些帧对定位阻塞毫无帮助（看门狗自身、asyncio 调度层），取栈时跳过
_NOISE_FILES = ('loop_watchdog.py', 'base_events.py', 'events.py',
                'threading.py', 'selectors.py')


class LoopWatchdog:
    """事件循环卡顿检测器（一个进程一个实例）"""

    def __init__(self, interval: float = 0.25, threshold: float = 0.5,
                 stall_wait: float = 3.0, stack_frames: int = 6):
        self.interval = max(0.05, float(interval))
        self.threshold = max(0.05, float(threshold))
        # 回调多久没被执行就判定「循环完全卡死」（协程方案测不到这种场景）
        self.stall_wait = max(self.threshold * 2, float(stall_wait))
        self.stack_frames = max(1, int(stack_frames))

        self._loop = None
        self._loop_thread_id = None
        self._thread = None
        self._stop = threading.Event()
        self._started_at = None

        self._in_stall = False
        self._suspended = False
        self._stall_started = 0.0
        self._stall_peak_ms = 0.0
        self._stats = {
            'samples': 0,
            'stalls': 0,          # 触发阈值的次数
            'total_stall_ms': 0,  # 卡顿累计时长
            'max_lag_ms': 0,
            'startup_max_lag_ms': 0,
            'last_lag_ms': 0,
            'reported_stack': None,
        }

    # ---- 生命周期 ----

    def start(self, loop=None, framework=None):
        """启动看门狗；loop 未就绪时由线程等待（最多 60s，与 scheduler 一致）"""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(loop, framework), daemon=True,
            name='zcbot-loop-watchdog')
        self._thread.start()
        return self

    def stop(self):
        """停止看门狗（幂等）；未收敛的卡顿状态静默清除，不补一条「恢复」日志"""
        self._stop.set()
        self._in_stall = False
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def suspend(self):
        """暂停告警（仍采样）。

        用于启动期：安装依赖、建表、同步加载全部插件必然会阻塞循环，
        这段时间报警等于每次启动刷几条 warning —— 噪音一多，真卡顿就没人看了。
        """
        self._suspended = True

    def resume(self):
        """恢复告警；返回启动期观测到的最大延迟（毫秒）。

        启动期峰值单独记账后清零运行期峰值，否则 `/metrics` 的
        `lag_max` 会永远停在启动那一刻的数，运行期真卡顿反而看不出来。
        """
        self._suspended = False
        startup_peak = self._stats['max_lag_ms']
        self._stats['startup_max_lag_ms'] = startup_peak
        self._stats['max_lag_ms'] = 0
        # 启动完成时打这行：看门狗线程启动时框架的 logging 还没初始化，
        # 那时打 info 会被直接丢掉（实测真实启动日志里看不到）。
        logger.info(f"事件循环看门狗运行中（采样 {self.interval * 1000:.0f}ms，"
                    f"阻塞阈值 {self.threshold * 1000:.0f}ms）；"
                    f"卡顿时打印阻塞位置")
        if startup_peak >= self.threshold * 1000.0:
            logger.debug(f"启动期最大循环延迟 {startup_peak:.0f}ms"
                         f"（启动期不告警，已单独记录）")
        return startup_peak

    # ---- 采样 ----

    def _wait_loop(self, loop, framework=None):
        if loop is not None and loop.is_running():
            return loop
        for _ in range(120):
            if self._stop.is_set():
                return None
            candidate = loop or getattr(framework, 'loop', None)
            if candidate is not None and candidate.is_running():
                return candidate
            if self._stop.wait(0.5):
                return None
        return None

    def _run(self, loop, framework=None):
        loop = self._wait_loop(loop, framework)
        if loop is None:
            logger.debug('事件循环看门狗未启动（等待事件循环超时）')
            return
        self._loop = loop
        self._started_at = time.time()
        logger.debug(f"事件循环看门狗线程已就绪（采样 {self.interval * 1000:.0f}ms，"
                     f"阻塞阈值 {self.threshold * 1000:.0f}ms）")

        while not self._stop.is_set():
            self._probe(loop)

    def _probe(self, loop):
        """投递一次打点并测量延迟（三段式：正常 / 卡顿中抓现场 / 恢复）"""
        sent = time.monotonic()
        done = threading.Event()

        def _mark():
            # 回调在事件循环线程执行：顺便记录 loop 线程 id，供抓栈定位
            self._loop_thread_id = threading.get_ident()
            done.set()

        try:
            loop.call_soon_threadsafe(_mark)
        except RuntimeError:
            # loop 已关闭（进程退出/框架重载），安静退出
            self._stop.set()
            return

        # 阶段 1：阈值内正常返回
        if done.wait(timeout=self.threshold):
            self._record((time.monotonic() - sent) * 1000.0)
            self._stop.wait(self.interval)
            return

        # 阶段 2：已超阈值。**必须在此时抓栈** —— 循环大概率仍在阻塞中。
        # 等回调执行完再抓，只能看到恢复后的空闲栈（实测抓到 proactor 的
        # `_loop_self_reading.recv`，对定位毫无帮助）。
        self._enter_stall((time.monotonic() - sent) * 1000.0, ongoing=True)

        # 阶段 3：等恢复（或确认彻底卡死）
        if done.wait(timeout=self.stall_wait):
            lag_ms = (time.monotonic() - sent) * 1000.0
            self._note_lag(lag_ms)
            self._exit_stall(lag_ms)
            return
        while not self._stop.is_set():
            if done.wait(timeout=0.5):
                break
        lag_ms = (time.monotonic() - sent) * 1000.0
        self._note_lag(lag_ms)
        self._exit_stall(lag_ms)

    def _note_lag(self, lag_ms: float):
        """只更新采样统计，不改变卡顿状态"""
        self._stats['samples'] += 1
        self._stats['last_lag_ms'] = round(lag_ms, 1)
        if lag_ms > self._stats['max_lag_ms']:
            self._stats['max_lag_ms'] = round(lag_ms, 1)

    def _record(self, lag_ms: float):
        """正常采样路径：超阈值进入卡顿，回落则退出"""
        self._note_lag(lag_ms)
        if lag_ms >= self.threshold * 1000.0:
            self._enter_stall(lag_ms)
        elif self._in_stall:
            self._exit_stall(lag_ms)

    def _enter_stall(self, lag_ms: float, ongoing: bool = False):
        if self._in_stall:
            self._stall_peak_ms = max(self._stall_peak_ms, lag_ms)
            return
        if self._suspended:
            # 启动期：只累计延迟，不报警、不进入卡顿状态
            self._stats['max_lag_ms'] = max(self._stats['max_lag_ms'],
                                            round(lag_ms, 1))
            return
        self._in_stall = True
        self._stall_started = time.monotonic()
        self._stall_peak_ms = lag_ms
        self._stats['stalls'] += 1
        where = self._capture_stack()
        self._stats['reported_stack'] = where
        # ongoing 表示「到现在还没恢复」，此时只能给出下界，别把阈值写成实测值
        amount = (f"已持续 ≥{lag_ms:.0f}ms"
                  if ongoing else f"{lag_ms:.0f}ms")
        logger.warning(
            f"事件循环阻塞 {amount}（阈值 {self.threshold * 1000:.0f}ms）："
            f"整个机器人此时无法处理任何消息。阻塞位置：\n{where}")

    def _exit_stall(self, lag_ms: float = None):
        if not self._in_stall:
            return
        self._in_stall = False
        if lag_ms is not None:
            self._stall_peak_ms = max(self._stall_peak_ms, lag_ms)
        # 本次阻塞时长用实测峰值：lag 就是「回调被推迟了多久」，
        # 比「从检测到恢复」的区间更接近真实阻塞时长（后者漏掉了阈值之前那段）。
        elapsed_ms = self._stall_peak_ms
        self._stats['total_stall_ms'] = round(
            self._stats['total_stall_ms'] + elapsed_ms, 1)
        logger.warning(
            f"事件循环恢复：本次阻塞约 {elapsed_ms:.0f}ms。"
            f"若频繁出现，检查插件里的同步网络请求 / 大循环 / 阻塞 IO。")

    def _capture_stack(self) -> str:
        """抓事件循环线程的栈顶若干业务帧，直接指到卡住的那一行"""
        ident = self._loop_thread_id
        if ident is None:
            return '  （无法获取事件循环线程 id，栈不可用）'
        frame = sys._current_frames().get(ident)
        if frame is None:
            return '  （事件循环线程已退出）'
        lines = []
        try:
            # 先滤噪声再截断：反过来会让 asyncio 调度帧把有限的配额吃光，
            # 真正卡住的业务帧反而被截掉。
            frames = [it for it in traceback.extract_stack(frame)
                      # 必须按 basename 精确匹配：用子串匹配会把 `test_loop_watchdog.py`、
                      # `my_events.py` 这类业务文件当成噪声帧滤掉，栈直接变空。
                      if os.path.basename(it.filename) not in _NOISE_FILES]
            for item in frames[-self.stack_frames:]:
                lines.append(f"  {item.filename}:{item.lineno} in {item.name}"
                             f"  |  {(item.line or '').strip()[:100]}")
        except Exception as e:
            return f'  （抓栈失败: {e}）'
        if not lines:
            return '  （栈中只有 asyncio 调度帧，阻塞发生在 C 扩展内）'
        return '\n'.join(reversed(lines))

    # ---- 对外视图 ----

    def stats(self) -> dict:
        """供 /metrics 与运行时面板：能看出是偶发抖动还是持续卡顿"""
        data = dict(self._stats)
        data['threshold_ms'] = round(self.threshold * 1000.0, 1)
        data['interval_ms'] = round(self.interval * 1000.0, 1)
        data['blocked'] = self._in_stall
        if self._started_at:
            data['uptime_s'] = round(time.time() - self._started_at, 1)
        return data


# 进程内单例：框架启动时 configure() 并按需 start()
watchdog = LoopWatchdog()


def configure(interval=None, threshold=None, stall_wait=None) -> LoopWatchdog:
    """按配置重设参数（仅在未启动时生效，避免运行中改参数造成统计跳跃）"""
    if watchdog._thread is not None and watchdog._thread.is_alive():
        logger.debug('看门狗运行中，忽略参数变更')
        return watchdog
    if interval:
        watchdog.interval = max(0.05, float(interval))
    if threshold:
        watchdog.threshold = max(0.05, float(threshold))
    if stall_wait:
        watchdog.stall_wait = max(watchdog.threshold * 2, float(stall_wait))
    return watchdog
