# -*- coding: utf-8 -*-
"""
内存黑匣子（Memory Black Box）调试系统

独立于常规日志体系，纯内存、无磁盘落库。用于追踪「事件流转」——比任何常规日志都更细：
一条消息从适配器进入内核、入队是否等待、被哪个 worker 取出、路由到哪些候选插件、
最终命中谁、未命中时广播给哪些订阅者、准备发给谁、以及「卡在第几步」。

这些传统 info/debug 日志难以还原的现场，正是黑匣子要记录的。

设计要点（来自 V2 规划）：
- 纯内存环形缓冲，硬上限 max_bytes（默认 256KB），满后自动丢弃最旧帧。
- 极低开销：仅 append + 偶尔 pop，不触发 GC 抖动；默认开启，随时待命排障。
- 留下「最后几十秒的录像」：发生诡异 Bug / 死锁 / 消息凭空消失时，可经 API / 命令
  导出最近现场，或针对单条 event_id 拉出完整链路、定位「卡在第几步」。

事件流标准阶段（step 编号，用于回答「卡在第几步」）：
  INGEST=1       事件进入内核（适配器归一化后）
  ENQUEUE=2      进入分层缓冲（记录是否在等待 / 缓冲水位）
  DEQUEUE=3      worker 取出事件（记录取自哪一层、队列剩余）
  CLASSIFY=4     事件分类（message/notice/request/meta）+ 提取文本
  RAW_HANDLERS=5 原始消息处理器评估（列出订阅了原始消息的插件）
  ROUTE_START=6  路由开始（列出本次命令路由的候选插件队列）
  PLUGIN_EVAL=7  逐插件评估（适配器白名单 / 群开关 / 命令匹配）
  PLUGIN_HIT=8   命令命中某插件
  HANDLER_ENTER=9 进入插件 handler
  HANDLER_EXIT=11 handler 返回（传播控制：stop / continue）
  BROADCAST=12   未命中 → 广播 message 事件给订阅者（列出订阅者）
  KEYWORD=13     关键词自动回复兜底
  UNMATCHED=14   完全未命中（无接管）
  SEND=15        准备发送给谁（group/user/私聊）
  DONE=16        事件处理完成
"""
import itertools
import threading
import time
from collections import deque

_DEFAULT_MAX_BYTES = 256 * 1024  # 256KB（富追踪需要更大预算）

# 事件级稳定自增计数（进程内唯一）。黑匣子 event_id 用它而非 str(id(event))：
# dispatch_event 内的 normalize_event 会做 out = dict(raw) 浅拷贝、把事件替换成新对象，
# 用对象 id 当 event_id 会导致 ingest/enqueue 与下游 dequeue/route 记录的 id 对不上、
# 整条事件流链路断链。改为在事件 dict 上嵌一个稳定代号（浅拷贝会带过），所有 record
# 统一读 event['_bb_eid']，链路恒不断。
_eid_counter = itertools.count(1)


def new_event_id() -> str:
    """生成进程内唯一且稳定的事件标识（黑匣子 event_id 用）。"""
    return 'e' + str(next(_eid_counter))
_DEFAULT_ENABLED = True

# 事件流标准阶段 → 步骤编号（顺序即「第几步」，用于 stuck_at 定位）
STAGE_STEP = {
    'ingest': 1,
    'enqueue': 2,
    'dequeue': 3,
    'classify': 4,
    'raw_handlers': 5,
    'route_start': 6,
    'plugin_eval': 7,
    'plugin_hit': 8,
    'handler_enter': 9,
    'handler_ctx': 10,
    'handler_exit': 11,
    'broadcast': 12,
    'keyword': 13,
    'unmatched': 14,
    'send': 15,
    'send_fail': 15,   # 发送动作的失败变体（同属第 15 步：准备发 → 发给谁但失败）
    'done': 16,
}


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
                # 最小 16KB 兜底，避免误配 0 把黑匣子彻底压死
                self._max_bytes = max(16 * 1024, mb)
                self._trim_unsafe()

    @property
    def enabled(self):
        return self._enabled

    # ── 写入 ──
    def record(self, stage, plugin=None, fn=None, event_id=None, msg=None,
               level='trace', step=None, adapter=None, src=None, to=None,
               subscribers=None, waiting=None, queue=None, detail=None):
        if not self._enabled:
            return
        if msg is not None and len(msg) > 200:
            msg = msg[:200] + '…'
        # 阶段步骤编号：显式传入优先，否则按 STAGE_STEP 推断
        if step is None:
            step = STAGE_STEP.get(stage)
        frame = {
            't': time.time(),
            'step': step,
            'stage': stage,
            'plugin': plugin or '',
            'fn': fn or '',
            'eid': event_id or '',
            'msg': msg or '',
            'lvl': level,
            'adapter': adapter or '',
            'src': src or '',      # 事件来源（user/group）
            'to': to or '',        # 准备发送目标（group/user/私聊）
            'subs': subscribers or '',   # 订阅者名单（逗号分隔）
            'waiting': '' if waiting is None else ('1' if waiting else '0'),
            'queue': queue or '',  # 缓冲水位摘要
            'detail': detail or '',
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
    def get_frames(self, limit=None, since=None, event_id=None):
        with self._lock:
            frames = list(self._buf)
        if since is not None:
            frames = [f for f in frames if f['t'] >= since]
        if event_id is not None:
            frames = [f for f in frames if f['eid'] == event_id]
        if limit is not None and limit > 0:
            frames = frames[-limit:]
        return frames

    def get_event_trace(self, event_id):
        """返回某 event_id 的完整事件流链路（按时间排序），含每一步的步骤编号。"""
        frames = self.get_frames(event_id=event_id)
        return sorted(frames, key=lambda f: (f['t'], f.get('seq', 0)))

    def stuck_at(self, event_id):
        """返回某 event_id「卡在第几步」：已抵达 DONE(step 16) 视为未卡住（返回 None）；

        否则返回事件流中「走到的最远一步」（最大 step 对应的帧）——即它卡在了哪一步。
        用「是否存在 done 帧」而非「最后一条是否 done」判定，避免记录乱序（如 wait=True
        路径下 enqueue 写在 await done 之后）导致误判。
        """
        trace = self.get_event_trace(event_id)
        if not trace:
            return None
        _done_step = STAGE_STEP.get('done')
        if any(f.get('step') == _done_step for f in trace):
            return None
        last = max(trace, key=lambda f: f.get('step') or 0)
        return {
            'event_id': event_id,
            'step': last.get('step'),
            'stage': last.get('stage'),
            'plugin': last.get('plugin'),
            'msg': last.get('msg'),
            'to': last.get('to'),
        }

    def recent_event_ids(self, limit=20):
        """最近出现过的 event_id 列表（按最后出现时间倒序），便于「挑一条事件看链路」。"""
        with self._lock:
            frames = list(self._buf)
        ids = []
        seen = set()
        for f in reversed(frames):
            eid = f.get('eid')
            if eid and eid not in seen:
                seen.add(eid)
                ids.append(eid)
                if len(ids) >= limit:
                    break
        return ids

    def stats(self):
        with self._lock:
            return {
                'enabled': self._enabled,
                'max_bytes': self._max_bytes,
                'used_bytes': self._bytes,
                'frames': len(self._buf),
                'dropped': self._dropped,
                'seq': self._seq,
                'events': len({f['eid'] for f in self._buf if f['eid']}),
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


def get_event_trace(event_id):
    return blackbox.get_event_trace(event_id)


def stuck_at(event_id):
    return blackbox.stuck_at(event_id)


def recent_event_ids(limit=20):
    return blackbox.recent_event_ids(limit=limit)


def stats():
    return blackbox.stats()


def clear():
    blackbox.clear()
