"""日志合并过滤器（自动止刷屏）。

高并发下，同一个错误原因会被每条消息 / 每个连接重复触发，每条都落一条独立日志，
导致日志文件与 WebUI 实时日志被刷爆。本过滤器挂在 root logger 上，对归一化后相同的
WARNING+ 日志在滑动窗口内合并计数：只发首条，窗口到期再发一条
「[日志合并] 该错误在 Ns 内重复 M 次：<原信息>」汇总，从根上抑制刷屏。

设计要点：
- 每条 record 只过 root filter 一次，console / 文件 / WebUI 三个下游 handler 结果一致；
- INFO/DEBUG 不参与合并，保持完整可见；
- 数字（user_id / message_id / 时间戳等动态参数）归一化为 #，让同类错误归并；
- 滑动窗口由后台 daemon 线程定期 flush 过期桶，汇总不会无限延迟；
- flush 在锁外调用 root.handle，避免持锁触发下游 handler 引发重入 / 阻塞。
"""
import atexit
import logging
import re
import sys
import threading
import time
import weakref
from collections import OrderedDict

_NUM_RE = re.compile(r'\d+')

# 存活的合并器（弱引用）：解释器退出时统一停掉后台线程。
# 用 WeakSet 而不是 atexit.register(self.stop)：后者每建一个实例就永久多一条
# 回调，测试里会累积成内存泄漏。
_LIVE_COALESCERS = weakref.WeakSet()


@atexit.register
def _stop_all_coalescers():
    """退出前停掉所有 flush 线程。

    daemon 线程若在解释器 finalizing 阶段还在往 stderr 写日志，CPython 会直接
    抛 `Fatal Python error: _enter_buffered_busy ... at interpreter shutdown`
    并终止进程（实测全量 pytest 结束时偶发）。这里先让线程退出，再让解释器收尾。
    """
    for c in list(_LIVE_COALESCERS):
        try:
            c.stop(timeout=1.0)
        except Exception:
            pass


class LogCoalescer(logging.Filter):
    """挂在 root logger 上的重复日志合并过滤器。"""

    def __init__(self, window: float = 10.0, min_level: int = logging.WARNING,
                 max_buckets: int = 5000):
        super().__init__()
        self.window = float(window)
        self.min_level = int(min_level)
        self.max_buckets = int(max_buckets)
        self._lock = threading.Lock()
        # OrderedDict：超量时淘汰最旧桶，防止极端情况下内存增长
        self._buckets: "OrderedDict[str, dict]" = OrderedDict()
        self._root = logging.getLogger()
        self._stop = False
        # 停止唤醒事件：stop() 置位后立即唤醒 flush 线程，不必等完整个窗口周期
        self._wake = threading.Event()
        self._timer = threading.Thread(
            target=self._flush_loop, daemon=True, name='log-coalesce-flush')
        self._timer.start()
        _LIVE_COALESCERS.add(self)

    # ---- 工具 ----
    @staticmethod
    def _normalize(msg: str) -> str:
        # 把数字（user_id / message_id / 时间戳等动态参数）替成 #，
        # 让「用户 12345 超时」「用户 67890 超时」归并为同一条
        return _NUM_RE.sub('#', msg)[:200]

    def _key(self, record: logging.LogRecord) -> str:
        return f"{record.name}|{record.levelno}|{self._normalize(record.getMessage())}"

    # ---- 过滤（合并）逻辑 ----
    def filter(self, record: logging.LogRecord) -> bool:
        # 汇总日志自身跳过合并，避免递归 / 二次合并
        if getattr(record, '_coalesce_skip', False):
            return True
        # 低级别不参与合并，保持完整可见
        if record.levelno < self.min_level:
            return True
        # 同一 record 实例会流经 root 的多个 handler（console/file/WebUI），
        # 只决策一次并缓存，保证各 handler 行为一致、不重复计数
        if hasattr(record, '_coalesce_decision'):
            return record._coalesce_decision
        decision, flush_old = self._compute(record)
        if flush_old is not None:
            # 锁外发汇总，避免持锁调用下游 handler
            self._emit_summary(self._key(record), flush_old)
        record._coalesce_decision = decision
        return decision

    def _compute(self, record: logging.LogRecord):
        """返回 (是否放行, 需汇总的旧桶或 None)。调用方负责锁外 flush。"""
        key = self._key(record)
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                # 新桶：首发，正常放行
                self._buckets[key] = {
                    'count': 1, 'first': now, 'last': now,
                    'name': record.name, 'levelno': record.levelno,
                    'sample': record.getMessage(),
                }
                if len(self._buckets) > self.max_buckets:
                    self._buckets.popitem(last=False)
                return True, None
            # 窗口内：累计并丢弃（不重复落盘 / 推送）
            if now - b['first'] < self.window:
                b['count'] += 1
                b['last'] = now
                b['sample'] = record.getMessage()
                return False, None
            # 窗口已到期：取出旧桶准备汇总，同时开新桶承接后续
            old = b
            self._buckets[key] = {
                'count': 1, 'first': now, 'last': now,
                'name': record.name, 'levelno': record.levelno,
                'sample': record.getMessage(),
            }
            return True, old

    # ---- 汇总发送 ----
    def _emit_summary(self, key: str, b: dict):
        try:
            # 解释器正在收尾：此时写任何流都可能撞上已关闭/加锁的缓冲区，
            # 结果是整个进程 fatal error，而不是丢一条汇总日志。
            if sys.is_finalizing():
                return
            # count == 1 表示窗口内只出现过一次：首发已经原样打过了，
            # 再补一条「重复 1 次」纯属重复且措辞失真
            # （线上就出现过 webui 依赖已自愈成功、几秒后又补一条加载失败）。
            if b.get('count', 0) <= 1:
                return
            # 措辞跟随级别：合并的可能是 WARNING 甚至 INFO，一律叫「该错误」
            # 会让人以为线上真报错了（日志里「该错误…安全提示」就很刺眼）。
            label = {logging.ERROR: '该错误', logging.CRITICAL: '该错误',
                     logging.WARNING: '该告警'}.get(b['levelno'], '该日志')
            msg = (f"[日志合并] {label}在 {self.window:.0f}s 内重复 "
                   f"{b['count']} 次：{b['sample']}")
            summary = logging.LogRecord(
                name=b['name'], level=b['levelno'], pathname='', lineno=0,
                msg=msg, args=(), exc_info=None,
            )
            summary._coalesce_skip = True
            self._root.handle(summary)
        except Exception:
            pass

    # ---- 后台 flush ----
    def _flush_loop(self):
        while not self._stop and not sys.is_finalizing():
            # 等窗口到期或 stop() 唤醒（替代裸 sleep：停止时可立即退出，不再滞留一个窗口周期）
            self._wake.wait(self.window)
            if self._stop:
                break
            now = time.monotonic()
            with self._lock:
                expired = [(k, self._buckets.pop(k)) for k, b in list(self._buckets.items())
                           if now - b['first'] >= self.window]
            # 锁外发汇总
            for k, b in expired:
                self._emit_summary(k, b)

    def stop(self, timeout: float = 5.0) -> bool:
        """停止后台 flush 线程：置停止信号 → 唤醒 → 有限等待线程退出。

        幂等，可重复调用。返回线程是否已真正退出（daemon 线程超时未退也不阻塞
        进程关闭）。合并/去重决策不受影响——stop 后仅缺少「无人再触发同类日志」
        时的周期性兜底汇总。
        """
        self._stop = True
        self._wake.set()
        if self._timer.is_alive() and threading.current_thread() is not self._timer:
            self._timer.join(timeout)
        return not self._timer.is_alive()
