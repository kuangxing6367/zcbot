# -*- coding: utf-8 -*-
"""
JS 人机验证（Proof-of-Work Challenge）
======================================
给 Web 接口加一道「必须真正执行 JavaScript 才能通过」的门槛。

原理（hashcash 式工作量证明）
------------------------------
1. 客户端 GET  /api/challenge            → 服务端下发随机 challenge + 难度 difficulty
2. 客户端 JS 暴力搜索整数 nonce，使
       sha256("<challenge>:<nonce>") 的十六进制前 difficulty 位为 0
3. 客户端 POST /api/challenge/verify     → 服务端复核通过后签发一次性通行票据 ticket
4. 之后所有 API 请求携带请求头 X-ZC-Pass: <ticket>

为什么有效
----------
- **纯 curl / requests 脚本**：不执行 JS，拿不到 ticket → 直接被静默丢弃。
- **照抄协议的机器人**：需要自己实现 SHA-256 与搜索循环；且求解代码随前端一起被
  混淆，无法直接抄。
- **海量并发**：每次求解都要付出真实算力（默认约 6.5 万次哈希），显著抬高扫描成本。

边界（诚实说明）
----------------
这是**抬高成本**的手段，不是密码学意义上的身份认证。懂技术的人可以实现同样的算法。
它的定位与「安全响应头」「JS 混淆」一致：挡掉绝大多数自动化脚本，让定向攻击变贵。

配置（config.yaml → security.human_check）
------------------------------------------
    enabled: true             # 总开关
    difficulty: 4             # 前导 0 的十六进制位数（每 +1，成本 ×16）
    apply_to: all             # 校验范围：all（所有 /api）/ login（仅登录）/ none
    bypass_whitelist: false   # 白名单 IP 是否免检
    ticket_ttl: 1800          # 通行票据有效期（秒）
    challenge_ttl: 120        # challenge 有效期（秒）
"""
import hashlib
import logging
import secrets
import threading
import time

from framework.api.ratelimit import SlidingWindow

logger = logging.getLogger('zcbot')

_MAX_ENTRIES = 20000
_DEFAULT_DIFFICULTY = 4
_MIN_DIFFICULTY, _MAX_DIFFICULTY = 1, 6

# 计量化参数（解决「买断制」：一次 PoW 解出后票据被无限复用、零边际成本）
_MAX_TICKET_USES = 200     # 单张票据最多服务请求数，超出需重新求解
_ISSUE_MAX = 30           # 单 IP 滑动窗口内最多签发挑战次数（防票据农场 / 批量骗取）
_ISSUE_WINDOW = 60.0

_lock = threading.Lock()
_challenges = {}   # challenge -> {"expires_at": float, "difficulty": int}
_tickets = {}      # ticket -> {"expires_at": float, "ip": str, "uses": int}
_issue_limiter = SlidingWindow(_ISSUE_MAX, _ISSUE_WINDOW)


def _purge(store: dict, now: float):
    """清理过期条目；超上限时剔除最早到期的一半"""
    def _exp(item):
        return item.get('expires_at') if isinstance(item, dict) else item
    for k in [k for k, v in store.items() if _exp(v) <= now]:
        store.pop(k, None)
    if len(store) > _MAX_ENTRIES:
        for k in sorted(store, key=lambda x: _exp(store[x]))[:len(store) // 2]:
            store.pop(k, None)


def _clamp_difficulty(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return _DEFAULT_DIFFICULTY
    return max(_MIN_DIFFICULTY, min(_MAX_DIFFICULTY, n))


def issue_challenge(difficulty=_DEFAULT_DIFFICULTY, ttl: int = 120) -> dict:
    """下发一个 challenge（随机串 + 难度）"""
    d = _clamp_difficulty(difficulty)
    challenge = secrets.token_hex(16)
    now = time.time()
    with _lock:
        _purge(_challenges, now)
        _challenges[challenge] = {'expires_at': now + max(10, int(ttl)), 'difficulty': d}
    return {'challenge': challenge, 'difficulty': d, 'algo': 'sha256', 'expires_in': int(ttl)}


def _pow_ok(challenge: str, nonce, difficulty: int) -> bool:
    """复核工作量证明"""
    if nonce is None:
        return False
    try:
        # 只接受纯十进制整数，避免超长字符串拖慢哈希
        n = str(int(str(nonce).strip()))
    except (TypeError, ValueError):
        return False
    if len(n) > 20:
        return False
    digest = hashlib.sha256(f'{challenge}:{n}'.encode('utf-8')).hexdigest()
    return digest.startswith('0' * difficulty)


def verify(challenge: str, nonce, ticket_ttl: int = 1800, client_ip: str = None):
    """
    校验 PoW 并签发通行票据。
    成功返回 ticket 字符串；失败返回 None。
    challenge 一次性：无论成功失败都作废，防重放。
    难度取「下发时记录的难度」，不接受客户端自报，避免被降级绕过。

    计量化：
    - 票据绑定签发来源 IP（remote_ip），换 IP 使用直接作废 → 阻止票据跨 IP 共享/轮转。
    - 签发本身受单 IP 滑动窗口限速（_issue_limiter），阻止单 IP 批量骗取票据农场。
    - 后续每次有效使用计入 uses，超过 _MAX_TICKET_USES 需重新求解 → 持续高并发需反复付算力。
    """
    if not challenge or not isinstance(challenge, str):
        return None
    now = time.time()
    # 签发限速（先于 PoW 复核，廉价拦住票据农场）
    if client_ip and not _issue_limiter.allow(client_ip):
        logger.warning(f"[人机验证] 签发限速触发: {client_ip}")
        return None
    with _lock:
        _purge(_challenges, now)
        entry = _challenges.pop(challenge, None)   # 一次性
    if not entry or entry.get('expires_at', 0) <= now:
        return None

    if not _pow_ok(challenge, nonce, entry['difficulty']):
        return None

    ticket = secrets.token_urlsafe(24)
    with _lock:
        _purge(_tickets, now)
        _tickets[ticket] = {
            'expires_at': now + max(60, int(ticket_ttl)),
            'ip': client_ip or '',
            'uses': 0,
        }
    return ticket


def is_valid_ticket(ticket, remote_ip: str = None) -> bool:
    """
    校验通行票据是否有效（并计入使用次数，实现计量）。
    - 不存在 / 已过期 → 无效
    - 绑定 IP 与当前来源不一致 → 无效（阻止跨 IP 复用）
    - 使用次数超过 _MAX_TICKET_USES → 无效，需重新求解
    返回 True 时原子地 +1 uses。
    """
    if not ticket or not isinstance(ticket, str):
        return False
    now = time.time()
    with _lock:
        rec = _tickets.get(ticket)
        if rec is None:
            return False
        if rec['expires_at'] <= now:
            _tickets.pop(ticket, None)
            return False
        # IP 绑定：签发时记录了来源 IP 且当前来源不同 → 作废（防共享/轮转）
        if rec['ip'] and remote_ip and rec['ip'] != remote_ip:
            _tickets.pop(ticket, None)
            logger.info(f"[人机验证] 票据 IP 不匹配作废: {remote_ip} != {rec['ip']}")
            return False
        if rec['uses'] >= _MAX_TICKET_USES:
            _tickets.pop(ticket, None)
            logger.info(f"[人机验证] 票据用量耗尽作废: {remote_ip}")
            return False
        rec['uses'] += 1
        return True


def revoke_ticket(ticket):
    with _lock:
        _tickets.pop(ticket, None)


def stats() -> dict:
    now = time.time()
    with _lock:
        _purge(_challenges, now)
        _purge(_tickets, now)
        return {'pending_challenges': len(_challenges), 'active_tickets': len(_tickets)}
