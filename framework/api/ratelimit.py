# -*- coding: utf-8 -*-
"""
全局 / 分级限速器
==================
此前 zcbot 仅对 /api/login(失败计数) 与 /api/version(60s<30) 等个别端点做了限速，
其余 ~136 个 /api/* 端点无任何限速，导致高并发请求「秒过」、无法被拦截
（实测：60 次 /api/version 静默 444 不触发封禁）。

本模块补齐「限速地基」，提供三类原语：
  - SlidingWindow  每 IP 滑动窗口（精确计数，杜绝固定窗口边界双倍漏洞）
  - TokenBucket    全局 QPS（保护数据库 / 后端不被打挂）
  - 路由分档       认证 / 挑战等高危端点更严，普通端点宽松

设计依据（公开案例）
--------------------
- ipferret《Rate limiting by IP — the right way》：IP 非用户标识，需 X-Forwarded-For
  受信任代理链约束；滑动窗口 > 固定窗口（边界可被 2× 双倍利用）。
- stacklesson / renode：《多维度同时限速》：全局(保护总 QPS) + 单 IP + 路由分档。
- blue.tymyrddin《Rate limiting》：固定窗口边界可被双倍利用；路径需归一化防 /api/v1/login
  与 /API/V1/LOGIN 被当成不同键；认证/枚举/资源创建端点分三档。
- 博客 phpwp.cn《IP 访问限频配置多少 QPS》：公开 API 起步 10 QPS；敏感操作 3 QPS；
  返回标准 429 + Retry-After。

本实现为纯内存（进程内）限速，与既有 _check_login_rate/_check_public_rate 同级；
如需跨多实例一致，可后续把状态后端换成 Redis（接口保持 allow() 不变）。
"""
import threading
import time

logger = __import__('logging').getLogger('zcbot')


class SlidingWindow:
    """每键滑动窗口限速（精确计数最近 window 秒内的请求数）。"""

    def __init__(self, max_requests: int, window_seconds: float, max_keys: int = 20000):
        self.max = int(max_requests)
        self.window = float(window_seconds)
        self.max_keys = int(max_keys)
        self._lock = threading.Lock()
        self._buckets = {}  # key -> list[float(timestamp)]

    def allow(self, key: str, cost: int = 1, now: float = None) -> bool:
        now = time.time() if now is None else now
        cutoff = now - self.window
        with self._lock:
            ts = self._buckets.get(key)
            if ts is None:
                ts = []
            ts = [t for t in ts if t > cutoff]
            if len(ts) + cost > self.max:
                # 仍写回裁剪后的列表，避免反复重算
                self._buckets[key] = ts
                return False
            for _ in range(cost):
                ts.append(now)
            self._buckets[key] = ts
            # 容量保护：超键数时丢弃最旧的一批（按最后时间戳排序）
            if len(self._buckets) > self.max_keys:
                oldest = sorted(self._buckets.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
                drop = len(self._buckets) - self.max_keys
                for k, _ in oldest[:drop]:
                    self._buckets.pop(k, None)
            return True

    def count(self, key: str, now: float = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - self.window
        with self._lock:
            ts = self._buckets.get(key)
            if not ts:
                return 0
            return len([t for t in ts if t > cutoff])

    def reset(self, key: str = None):
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


class TokenBucket:
    """令牌桶（用于全局 QPS 上限，平滑突发、保护后端）。"""

    def __init__(self, rate: float, capacity: float):
        self.rate = float(rate)          # 每秒补充令牌数
        self.capacity = float(capacity)  # 桶容量（最大突发）
        self._tokens = float(capacity)
        self._last = time.time()
        self._lock = threading.Lock()

    def allow(self, cost: int = 1, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            elapsed = now - self._last
            if elapsed > 0:
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last = now
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


# ---------------------------------------------------------------------------
# 默认分档（可在 config.yaml -> security.rate_limit 覆盖）
#   global_qps      : 全系统每秒总请求上限（保护数据库 / 后端）
#   per_ip_max      : 单 IP 滑动窗口内最大请求数
#   per_ip_window   : 单 IP 滑动窗口长度（秒）
#   strict_max/strict_window : 高危端点(认证/挑战)单 IP 更严档
#   strict_paths    : 命中即走 strict 档的路径前缀
# ---------------------------------------------------------------------------
DEFAULTS = {
    'enabled': True,
    'global_qps': 500,
    'per_ip_max': 60,          # 单 IP 5 秒内最多 60 次（平均 12/s，足够正常 UI）
    'per_ip_window': 5.0,
    'strict_max': 20,          # 高危端点 5 秒内最多 20 次
    'strict_window': 5.0,
    'strict_paths': ('/api/login', '/api/challenge', '/api/challenge/verify',
                     '/api/register', '/api/admin'),
    'whitelist_bypass': True,  # 白名单/内网 IP 豁免全局限速（便于本地/同网段排障）
}


class RateLimitGuard:
    """组合全局 QPS + 每 IP 滑动窗口 + 路由分档，输出 429 或 None。"""

    def __init__(self, cfg: dict = None, is_exempt=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get('enabled', DEFAULTS['enabled']))
        self.global_qps = float(cfg.get('global_qps', DEFAULTS['global_qps']))
        self.per_ip_max = int(cfg.get('per_ip_max', DEFAULTS['per_ip_max']))
        self.per_ip_window = float(cfg.get('per_ip_window', DEFAULTS['per_ip_window']))
        self.strict_max = int(cfg.get('strict_max', DEFAULTS['strict_max']))
        self.strict_window = float(cfg.get('strict_window', DEFAULTS['strict_window']))
        self.strict_paths = tuple(cfg.get('strict_paths', DEFAULTS['strict_paths']))
        self.whitelist_bypass = bool(cfg.get('whitelist_bypass', DEFAULTS['whitelist_bypass']))
        self._is_exempt = is_exempt  # callable(ip) -> bool
        self._qps = TokenBucket(self.global_qps, max(self.global_qps, 1.0) * 2)
        self._ip = SlidingWindow(self.per_ip_max, self.per_ip_window)
        self._strict = SlidingWindow(self.strict_max, self.strict_window)
        self._hits = 0
        self._blocked = 0
        self._lock = threading.Lock()

    def check(self, ip: str, path: str) -> tuple:
        """
        返回 (allowed: bool, retry_after: int)
        allowed=False 时应由调用方返回 429 + Retry-After。
        白名单/内网 IP 在 whitelist_bypass=True 时直接放行。
        """
        if not self.enabled:
            return True, 0
        if self.whitelist_bypass and self._is_exempt and self._is_exempt(ip):
            return True, 0

        # 全局 QPS（先于 IP，廉价地拦住系统级洪泛）
        if not self._qps.allow():
            with self._lock:
                self._blocked += 1
            return False, 1

        # 路由分档：命中高危前缀走 strict，否则普通档
        is_strict = any(path.startswith(p) for p in self.strict_paths)
        limiter = self._strict if is_strict else self._ip
        if not limiter.allow(ip):
            with self._lock:
                self._blocked += 1
            # Retry-After：取窗口剩余近似（严格档窗口更短）
            retry = int(self.strict_window if is_strict else self.per_ip_window) + 1
            return False, retry

        with self._lock:
            self._hits += 1
        return True, 0

    def stats(self) -> dict:
        with self._lock:
            return {'enabled': self.enabled, 'hits': self._hits, 'blocked': self._blocked,
                    'global_qps': self.global_qps, 'per_ip_max': self.per_ip_max,
                    'per_ip_window': self.per_ip_window, 'strict_max': self.strict_max}
