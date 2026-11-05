# -*- coding: utf-8 -*-
"""
出站闸门（Outbound Guard）
==========================
入站守卫（inbound_guard.py）管"进来的消息会不会被重复处理"，本模块管对称的另一半：

  1. **发送节流**——插件批量推送时把速率压在平台惩罚线以下，而不是等到被限频/禁言；
  2. **失败重试**——发送通道抖动时按指数退避重试一次，而不是让消息凭空丢掉。

挂载点是「动作调用」的统一入口，不是某个具体协议：

    ProtocolAdapter.acall / call   （遵守契约的接入端，基类一处覆盖全部）
    onebot_adapter.ApiCaller       （自建 API 调用器，不走基类，单独接一处）

只接管**发送类动作**（SEND_ACTIONS）；get_* 等查询动作原样直通，不受节流也不重试。

三条设计取舍
------------
- **节流是等待，不是丢弃**。发送是插件的明确意图，静默丢弃会让插件以为已发出。
  等待有上限（`max_wait`），超过上限才返回失败结构——失败可见，但不无限拖住事件循环。
- **只重试"确定没发出去"的失败**。`retcode=-3`（本地通道异常）重试；`-1` 未连接不重试
  （等一会儿也不会自己好）；`-2` 超时**不重试**——超时不代表没送达，重试只会产生重复消息，
  而重复消息比发送失败更难排查。
- **闸门自身故障一律放行**。节流统计出问题不能让消息发不出去，与入站守卫同一取向。
"""
import asyncio
import logging
import random
import threading
import time
from collections import deque

logger = logging.getLogger('zcbot')

# 需要节流/重试的发送类动作（查询类动作不在其中，必须原样直通）
SEND_ACTIONS = frozenset({
    'send_msg', 'send_private_msg', 'send_group_msg',
    'send_private_forward_msg', 'send_group_forward_msg',
    'send_group_notice', 'send_like',
    'send_poke', 'friend_poke', 'group_poke',
    'upload_group_file', 'upload_private_file',
})

# 只有「确定没发出去」的本地包装码才重试，见模块 docstring
RETRYABLE_RETCODES = frozenset({-3})

# 配置缺失时的兜底（与 config.py 默认段保持一致）
_DEFAULTS = {
    'rate_limit': {
        'enabled': True,
        'window': 60,
        'global_per_min': 180,   # 全局（所有目标合计）
        'group_per_min': 30,     # 单群
        'user_per_min': 20,      # 单用户（私聊）
        'max_wait': 5.0,         # 单次发送最多等多久，超过即拒绝
    },
    'retry': {
        'enabled': True,
        'attempts': 1,           # 额外重试次数（0=不重试）
        'backoff': 0.4,          # 首次重试前等待（秒），之后翻倍
    },
}

_REJECT_CODE = -4


def _cfg_get(cfg, section, key, fallback):
    try:
        val = (cfg or {}).get(section) or {}
        if key in val and val[key] is not None:
            return val[key]
    except Exception:  # noqa: BLE001
        pass
    return fallback


class OutboundGate:
    """发送节流 + 失败重试（模块级单例 gate）"""

    _HITS_CAP = 5000      # 受控目标键上限，防止私聊大量不同用户时内存无限增长

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}   # key -> deque[timestamp]
        self._stats = {'sent': 0, 'failed': 0, 'throttled': 0, 'retried': 0, 'waited_ms': 0}
        self.configure(None)

    # ---- 配置 ----

    def configure(self, cfg):
        """按配置段 `outbound` 生效（可在运行时热应用）"""
        rl = _DEFAULTS['rate_limit']
        rt = _DEFAULTS['retry']
        enabled = _cfg_get(cfg, 'rate_limit', 'enabled', rl['enabled'])
        self.enabled = bool(enabled)
        self.window = float(_cfg_get(cfg, 'rate_limit', 'window', rl['window']) or 60)
        self.global_per_min = int(_cfg_get(cfg, 'rate_limit', 'global_per_min', rl['global_per_min']) or 0)
        self.group_per_min = int(_cfg_get(cfg, 'rate_limit', 'group_per_min', rl['group_per_min']) or 0)
        self.user_per_min = int(_cfg_get(cfg, 'rate_limit', 'user_per_min', rl['user_per_min']) or 0)
        self.max_wait = float(_cfg_get(cfg, 'rate_limit', 'max_wait', rl['max_wait']) or 0)
        self.retry_enabled = bool(_cfg_get(cfg, 'retry', 'enabled', rt['enabled']))
        self.retry_attempts = max(0, int(_cfg_get(cfg, 'retry', 'attempts', rt['attempts']) or 0))
        self.retry_backoff = max(0.0, float(_cfg_get(cfg, 'retry', 'backoff', rt['backoff']) or 0))
        return self.describe()

    def describe(self):
        return {
            'rate_limit': {
                'enabled': self.enabled, 'window': self.window,
                'global_per_min': self.global_per_min,
                'group_per_min': self.group_per_min,
                'user_per_min': self.user_per_min,
                'max_wait': self.max_wait,
            },
            'retry': {
                'enabled': self.retry_enabled,
                'attempts': self.retry_attempts,
                'backoff': self.retry_backoff,
            },
        }

    def stats(self):
        with self._lock:
            return dict(self._stats)

    def reset_stats(self):
        with self._lock:
            for k in self._stats:
                self._stats[k] = 0

    # ---- 判定 ----

    @staticmethod
    def should_gate(action) -> bool:
        return str(action or '') in SEND_ACTIONS

    @staticmethod
    def _target(params):
        """从参数里取限流目标：群优先，其次私聊"""
        try:
            gid = params.get('group_id')
            if gid:
                return 'group', str(gid)
            uid = params.get('user_id')
            if uid:
                return 'user', str(uid)
        except Exception:  # noqa: BLE001
            pass
        return None, None

    @staticmethod
    def _failed(result) -> bool:
        if not isinstance(result, dict):
            return False
        status = result.get('status')
        if status is not None:
            return str(status) != 'ok'
        retcode = result.get('retcode')
        return bool(retcode) and retcode != 0

    # ---- 滑动窗口 ----

    def _need(self, key, limit, now):
        """返回该 key 还需要等多久才腾出一个名额（0=现在就能发）"""
        dq = self._hits.get(key)
        if not dq:
            return 0.0
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) < limit:
            return 0.0
        return max(0.0, dq[0] + self.window - now)

    def _record(self, keys, now):
        for key in keys:
            dq = self._hits.get(key)
            if dq is None:
                dq = self._hits[key] = deque()
            dq.append(now)
        self._shrink(now)

    def _shrink(self, now):
        """键数量超限时清掉过期窗口，仍超限则丢弃最早插入的键（普通 dict 保持插入序）"""
        if len(self._hits) <= self._HITS_CAP:
            return
        cutoff = now - self.window
        for k in [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]:
            self._hits.pop(k, None)
        while len(self._hits) > self._HITS_CAP:
            self._hits.pop(next(iter(self._hits)), None)

    def _keys(self, bot, kind, ident):
        keys = [('global',)]
        if kind:
            keys.append((bot or '', kind, ident))
        return keys

    def _limit_for(self, kind):
        if kind == 'group':
            return self.group_per_min
        if kind == 'user':
            return self.user_per_min
        return 0

    def _plan(self, bot, params):
        """返回 (需要等待秒数, keys)；不超限时同时登记时间戳"""
        if not self.enabled:
            return 0.0, None
        kind, ident = self._target(params)
        now = time.time()
        with self._lock:
            waits = []
            if self.global_per_min > 0:
                waits.append(self._need(('global',), self.global_per_min, now))
            limit = self._limit_for(kind)
            if kind and limit > 0:
                waits.append(self._need((bot or '', kind, ident), limit, now))
            wait = max(waits) if waits else 0.0
            if wait <= 0:
                self._record(self._keys(bot, kind, ident), now)
                return 0.0, None
            return wait, self._keys(bot, kind, ident)

    def _commit(self, keys):
        """等待结束后确认名额（等待期间可能被别人占走）"""
        with self._lock:
            self._record(keys, time.time())

    def _reject(self, action, wait):
        with self._lock:
            self._stats['throttled'] += 1
        msg = (f"出站限流：{action} 需等待 {wait:.1f}s 超过上限 "
               f"{self.max_wait:.1f}s，已放弃本次发送")
        logger.warning(msg)
        return {'status': 'failed', 'retcode': _REJECT_CODE, 'msg': msg,
                'throttled': True}

    def _note_wait(self, waited):
        if waited > 0:
            with self._lock:
                self._stats['waited_ms'] += int(waited * 1000)

    # ---- 重试 ----

    def _retryable(self, result) -> bool:
        if not isinstance(result, dict):
            return False
        if not self._failed(result):
            return False
        try:
            return int(result.get('retcode')) in RETRYABLE_RETCODES
        except (TypeError, ValueError):
            return False

    def _sleep_time(self, attempt):
        base = self.retry_backoff * (2 ** attempt)
        return base + random.uniform(0, base * 0.25) if base > 0 else 0.0

    def _count_result(self, result):
        with self._lock:
            if self._failed(result):
                self._stats['failed'] += 1
            else:
                self._stats['sent'] += 1

    # ---- 对外执行入口 ----

    async def execute_async(self, action, bot, params, call):
        """异步执行一次动作调用；call 是无参协程函数"""
        if not self.should_gate(action):
            return await call()

        waited = 0.0
        try:
            wait, keys = self._plan(bot, params)
            waited = wait
            if wait > 0:
                if wait > self.max_wait:
                    return self._reject(action, wait)
                await asyncio.sleep(wait)
                self._commit(keys)
                self._note_wait(wait)
        except Exception as e:  # noqa: BLE001 - 闸门故障一律放行
            logger.debug(f"出站节流判定异常，直通发送: {e}")

        return await self._retry_async(action, call, waited)

    def execute_sync(self, action, bot, params, call):
        """同步执行一次动作调用；call 是无参函数"""
        if not self.should_gate(action):
            return call()

        waited = 0.0
        try:
            wait, keys = self._plan(bot, params)
            waited = wait
            if wait > 0:
                if wait > self.max_wait:
                    return self._reject(action, wait)
                time.sleep(wait)
                self._commit(keys)
                self._note_wait(wait)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"出站节流判定异常，直通发送: {e}")

        return self._retry_sync(action, call, waited)

    async def _retry_async(self, action, call, waited):
        attempts = self.retry_attempts if self.retry_enabled else 0
        for i in range(attempts + 1):
            try:
                result = await call()
            except (TimeoutError, asyncio.TimeoutError):
                # 超时属于契约里的 -2：可能已经送达，重试只会产生重复消息。
                # 其它异常沿用原有重试语义（本地通道异常，确定没发出去）。
                with self._lock:
                    self._stats['failed'] += 1
                raise
            except Exception as e:
                if i < attempts:
                    self._note_retry(action, i, e)
                    delay = self._sleep_time(i)
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                with self._lock:
                    self._stats['failed'] += 1
                raise
            if i < attempts and self._retryable(result):
                self._note_retry(action, i, result.get('msg'))
                delay = self._sleep_time(i)
                if delay:
                    await asyncio.sleep(delay)
                continue
            self._count_result(result)
            return result
        return {'status': 'failed', 'retcode': -3, 'msg': '出站重试耗尽'}

    def _retry_sync(self, action, call, waited):
        attempts = self.retry_attempts if self.retry_enabled else 0
        for i in range(attempts + 1):
            try:
                result = call()
            except (TimeoutError, asyncio.TimeoutError):
                # 同 _retry_async：超时可能已送达，不重试。
                with self._lock:
                    self._stats['failed'] += 1
                raise
            except Exception as e:
                if i < attempts:
                    self._note_retry(action, i, e)
                    delay = self._sleep_time(i)
                    if delay:
                        time.sleep(delay)
                    continue
                with self._lock:
                    self._stats['failed'] += 1
                raise
            if i < attempts and self._retryable(result):
                self._note_retry(action, i, result.get('msg'))
                delay = self._sleep_time(i)
                if delay:
                    time.sleep(delay)
                continue
            self._count_result(result)
            return result
        return {'status': 'failed', 'retcode': -3, 'msg': '出站重试耗尽'}

    def _note_retry(self, action, attempt, reason):
        with self._lock:
            self._stats['retried'] += 1
        logger.warning(f"出站发送失败，准备第 {attempt + 1} 次重试（{action}）: {reason}")


# 模块级单例：Framework 启动时 configure(self.config.get('outbound'))
gate = OutboundGate()
