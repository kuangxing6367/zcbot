# -*- coding: utf-8 -*-
"""入站可靠性守卫：断线重连重放去重 + 消息流限流。

为什么放在这里（而不是在 ops / 运维层）：
成熟 IM 框架（OneBot / Koishi / dsh / 各类生产级 bot）的一致结论——
生产环境机器人先挂的往往是「限流没做」和「重连重放重复回复」，
不是命令逻辑。这些是「基础设施先建」的核心能力，不是可延后的运维项。

- 去重（dedup）：WebSocket 断线重连、客户端升级重推，同一 message_id 会被重复
  推送。在事件入队前用 (bot, message_id) 滑动窗口拦掉重放，避免对同一条消息
  回复两次 / 重复触发业务。notice / meta / request 类事件本身幂等，不做去重。
- 限流（rate_limit）：per-user / per-group / global 三级滑动窗口，挡住刷屏与
  DoS。可配置 skip（静默丢弃）或 notify（每窗口至多回复一条限流提示）。

设计约束：
- 纯内存、零依赖、线程安全（适配器线程与 worker 协程都会调用）。
- 入队前执行：被拦掉的事件不占用分层缓冲槽。
- 失败安全：守卫自身异常绝不能让事件丢失——外层 try 包住，异常时放行。
- 配置缺省安全：去重默认开（dedup_window=300，纯收益）；限流默认关
  （阈值带业务属性，由部署方按场景开启）。
"""
import threading
import time
from collections import deque


class InboundGuard:
    """入站事件去重 + 限流。无状态配置，状态全在内存。"""

    # 内存占用硬上限：超过则清掉已过期条目；仍超限再清最旧的，防失控。
    _SEEN_CAP = 20000
    _HITS_CAP = 5000

    def __init__(self, config: dict = None):
        cfg = config or {}
        dedup = cfg.get('dedup_window', 300)
        self.dedup_window = int(dedup) if dedup else 0

        rl = cfg.get('rate_limit') or {}
        self.rl_enabled = bool(rl.get('enabled', False))
        self.rl_window = int(rl.get('window', 60)) or 60
        self.rl_global = int(rl.get('global_per_min', 0) or 0)
        self.rl_group = int(rl.get('group_per_min', 0) or 0)
        self.rl_user = int(rl.get('user_per_min', 0) or 0)
        self.rl_action = rl.get('action', 'skip')
        self.rl_notify = rl.get('notify_text', '消息过于频繁，请稍后再试。')

        self._lock = threading.Lock()
        self._seen = {}          # (bot, message_id) -> expire_at
        self._hits = {}          # (kind, scope) -> deque(timestamps)
        self._notified = {}      # ('notify', kind, scope) -> expire_at

    # ── 主入口 ───────────────────────────────────────────────
    def check(self, event: dict, bot_name: str = 'default'):
        """返回 (allowed, reason, should_notify)。

        allowed:      False 表示事件应在入队前被丢弃。
        reason:       'dedup' / 'rate_limit:<scope>' / ''。
        should_notify: 仅限流且 action=notify 时，提示本窗口内尚未发过才为 True。
        """
        now = time.time()
        # 接入端优先取事件自带（归一化后必存在），参数仅作兜底
        bn = event.get('bot_name') or bot_name

        # 1) 去重：只对消息类事件、且 message_id 真实存在
        if self.dedup_window > 0:
            mid = event.get('message_id')
            post = event.get('post_type')
            if mid and post == 'message':
                key = (bn, str(mid))
                with self._lock:
                    exp = self._seen.get(key)
                    if exp is not None and exp > now:
                        return (False, 'dedup', False)
                    self._seen[key] = now + self.dedup_window
                    if len(self._seen) > self._SEEN_CAP:
                        self._sweep_seen(now)

        # 2) 限流：三级滑动窗口
        if self.rl_enabled:
            uid = event.get('user_id')
            gid = event.get('group_id')
            scopes = []
            if self.rl_global > 0:
                scopes.append(('global', 'global', self.rl_global))
            if self.rl_group > 0 and gid:
                scopes.append(('group', str(gid), self.rl_group))
            if self.rl_user > 0 and uid:
                scopes.append(('user', str(uid), self.rl_user))
            if scopes:
                with self._lock:
                    win_start = now - self.rl_window
                    for kind, scope, limit in scopes:
                        dq = self._hits.setdefault((kind, scope), deque())
                        while dq and dq[0] <= win_start:
                            dq.popleft()
                        if len(dq) >= limit:
                            notify = False
                            if self.rl_action == 'notify':
                                nkey = ('notify', kind, scope)
                                nexp = self._notified.get(nkey, 0)
                                if nexp <= now:
                                    self._notified[nkey] = now + min(self.rl_window, 60)
                                    notify = True
                            return (False, 'rate_limit:%s' % kind, notify)
                        dq.append(now)
                    if len(self._hits) > self._HITS_CAP:
                        self._sweep_hits(now)

        return (True, '', False)

    # ── 周期清理（可选，由框架在空闲时调用；不调用也靠惰性淘汰兜底）──
    def sweep(self):
        now = time.time()
        with self._lock:
            self._sweep_seen(now)
            self._sweep_hits(now)

    def _sweep_seen(self, now):
        expired = [k for k, v in self._seen.items() if v <= now]
        for k in expired:
            self._seen.pop(k, None)
        # 仍超限：清掉最早插入的（dict 3.7+ 保持插入序；注意 dict.popitem 不接受
        # 参数，只有 OrderedDict 才有 last=False，这里必须显式取首个键）
        while len(self._seen) > self._SEEN_CAP:
            self._seen.pop(next(iter(self._seen)), None)

    def _sweep_hits(self, now):
        win_start = now - self.rl_window
        for key in list(self._hits.keys()):
            dq = self._hits[key]
            while dq and dq[0] <= win_start:
                dq.popleft()
            if not dq:
                self._hits.pop(key, None)
        while len(self._hits) > self._HITS_CAP:
            self._hits.pop(next(iter(self._hits)), None)
