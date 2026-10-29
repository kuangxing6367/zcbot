# -*- coding: utf-8 -*-
"""
内存黑匣子（Memory Black Box）调试系统

独立于常规日志体系，纯内存、无磁盘落库。用于追踪事件流转、中间件路由、
插件生命周期、以及调用了哪个 ctx 函数——这些是传统 info/debug 日志难以还原的
“事件卡在哪个插件、调了哪个 ctx 方法”的现场。

设计要点（来自 V2 规划）：
- 纯内存环形缓冲，硬上限 max_bytes（默认 100KB），满后自动丢弃最旧帧。
- 极低开销：仅 append + 偶尔 pop，不触发 GC 抖动；默认开启，随时待命排障。
- 留下“最后几十秒的录像”：发生诡异 Bug / 死锁时，可经 API / 命令导出最近现场。
"""
import threading
import time
from collections import deque

_DEFAULT_MAX_BYTES = 102400  # 100KB
_DEFAULT_ENABLED = True


class BlackBox:
    def __init__(self):
        self._lock = threading.RLock()
        self._buf = deque()
        self._bytes = 0
        self._enabled = _DEFAULT_ENABLED
        self._max_bytes = _DEFAULT_MAX_BYTES
        self._seq = 0
        self._dropped = 0

    # ── 配置 ──
    def configure(self, enabled=None, max_bytes=None):
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            if max_bytes is not None:
                try:
                    mb = int(max_bytes)
                except (TypeError, ValueError):
                    mb = _DEFAULT_MAX_BYTES
                # 最小 4KB 兜底，避免误配 0 把黑匣子彻底压死
                self._max_bytes = max(4096, mb)
                self._trim_unsafe()

    @property
    def enabled(self):
        return self._enabled

    # ── 写入 ──
    def record(self, stage, plugin=None, fn=None, event_id=None,
               msg=None, level='trace'):
        if not self._enabled:
            return
        if msg is not None and len(msg) > 200:
            msg = msg[:200] + '…'
        frame = {
            't': time.time(),
            'stage': stage,
            'plugin': plugin or '',
            'fn': fn or '',
            'eid': event_id or '',
            'msg': msg or '',
            'lvl': level,
        }
        try:
            fbytes = len(repr(frame).encode('utf-8'))
        except Exception:
            fbytes = 128
        with self._lock:
            if not self._enabled:
                return
            self._seq += 1
            frame['seq'] = self._seq
            self._buf.append(frame)
            self._bytes += fbytes
            self._trim_unsafe()

    def _trim_unsafe(self):
        # 调用方须持锁
        while self._buf and self._bytes > self._max_bytes:
            old = self._buf.popleft()
            self._bytes -= max(1, len(repr(old).encode('utf-8')))
            self._dropped += 1

    # ── 读取 ──
    def get_frames(self, limit=None, since=None):
        with self._lock:
            frames = list(self._buf)
        if since is not None:
            frames = [f for f in frames if f['t'] >= since]
        if limit is not None and limit > 0:
            frames = frames[-limit:]
        return frames

    def stats(self):
        with self._lock:
            return {
                'enabled': self._enabled,
                'max_bytes': self._max_bytes,
                'used_bytes': self._bytes,
                'frames': len(self._buf),
                'dropped': self._dropped,
                'seq': self._seq,
            }

    def clear(self):
        with self._lock:
            self._buf.clear()
            self._bytes = 0
            self._dropped = 0


# 全局单例（跨模块共享同一份内存现场）
blackbox = BlackBox()


def record(*args, **kwargs):
    blackbox.record(*args, **kwargs)


def configure(*args, **kwargs):
    blackbox.configure(*args, **kwargs)


def get_frames(*args, **kwargs):
    return blackbox.get_frames(*args, **kwargs)


def stats():
    return blackbox.stats()


def clear():
    blackbox.clear()
