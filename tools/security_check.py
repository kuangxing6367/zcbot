# -*- coding: utf-8 -*-
"""
安全加固断言测试（按用户要求：把「高并发必被拦」「票据计量化」写成断言）
=========================================================================
直接对限速器与 PoW 票据模块做单元级断言，并校验诱饵命名/token 规格。
不依赖完整 Web 启动（避免 DB/framework 装配开销），聚焦「地基」逻辑正确性。

运行：python tools/security_check.py
"""
import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.api import ratelimit as rl
from framework.api import human_check as hc
from framework.api import honeypot as hp

PASS, FAIL = 0, 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def solve_pow(challenge, difficulty):
    n = 0
    while True:
        digest = hashlib.sha256(f'{challenge}:{n}'.encode('utf-8')).hexdigest()
        if digest.startswith('0' * difficulty):
            return n
        n += 1
        if n > 5_000_000:
            raise RuntimeError('PoW solve failed')


print("== 1. 滑动窗口限速（高并发必被拦）==")
sw = rl.SlidingWindow(max_requests=60, window_seconds=5.0)
allow = sum(1 for _ in range(60) if sw.allow('ipA'))
block = sum(1 for _ in range(40) if not sw.allow('ipA'))   # 第 61~100 应被拦
check("单 IP 窗口内 60 次放行", allow == 60, f"allow={allow}")
check("超窗口 40 次全部拦截", block == 40, f"block={block}")

print("== 2. 全局 QPS（令牌桶）==")
tb = rl.TokenBucket(rate=10.0, capacity=10.0)
burst_ok = sum(1 for _ in range(10) if tb.allow())
burst_block = sum(1 for _ in range(50) if not tb.allow())
check("桶容量内突发放行", burst_ok == 10, f"ok={burst_ok}")
check("超容量被拦", burst_block == 50, f"block={burst_block}")

print("== 3. 分级限速闸门（模拟高并发来自单 IP）==")
guard = rl.RateLimitGuard({}, is_exempt=lambda ip: False)  # 不豁免，模拟外部攻击者
ok = 0
blocked = 0
for _ in range(120):
    allowed, _ = guard.check('203.0.113.9', '/api/something')
    if allowed:
        ok += 1
    else:
        blocked += 1
check("120 次突发中多数被拦", blocked > 50, f"ok={ok} blocked={blocked}")
check("稳态仍放行少量（非误杀）", ok > 0, f"ok={ok}")

print("== 4. PoW 票据计量化（绑 IP + 用量计数）==")
ip_a = '198.51.100.7'

# 4a 合法签发票据
ch = hc.issue_challenge(difficulty=4, ttl=120)
nonce = solve_pow(ch['challenge'], 4)
ticket = hc.verify(ch['challenge'], nonce, ticket_ttl=1800, client_ip=ip_a)
check("合法 PoW 签发票据", bool(ticket), "")

# 4b 同 IP 有效
check("票据绑定签发 IP：同 IP 有效", hc.is_valid_ticket(ticket, remote_ip=ip_a) is True, "")

# 4c 跨 IP 失效（独立票据，避免污染待验证的票据）
ch2 = hc.issue_challenge(difficulty=4, ttl=120)
t2 = hc.verify(ch2['challenge'], solve_pow(ch2['challenge'], 4), client_ip=ip_a)
check("票据跨 IP 失效（防盗用/轮转）", hc.is_valid_ticket(t2, remote_ip='9.9.9.9') is False, "")

# 4d 用量计数：超过上限后作废（独立票据；防止一次买断无限复用）
ch3 = hc.issue_challenge(difficulty=4, ttl=120)
t3 = hc.verify(ch3['challenge'], solve_pow(ch3['challenge'], 4), client_ip=ip_a)
used = 0
for _ in range(hc._MAX_TICKET_USES + 5):
    if hc.is_valid_ticket(t3, remote_ip=ip_a):
        used += 1
check("票据用量耗尽后失效", used == hc._MAX_TICKET_USES, f"used={used}")

# 4e 伪造/不存在的票据
check("伪造票据无效", hc.is_valid_ticket('not-a-real-ticket', remote_ip=ip_a) is False, "")

print("== 5. 签发限速（防票据农场）==")
# 同一 IP 在 60s 窗口内最多签发 _ISSUE_MAX 次
issued = 0
for _ in range(hc._ISSUE_MAX + 10):
    c = hc.issue_challenge(difficulty=4, ttl=1)
    t = hc.verify(c['challenge'], solve_pow(c['challenge'], 4), client_ip='203.0.113.50')
    if t:
        issued += 1
check("单 IP 签发超阈值后被限", issued <= hc._ISSUE_MAX, f"issued={issued} max={hc._ISSUE_MAX}")

print("== 6. 诱饵命名去 v1 + 陷阱 token 对齐真实规格 ==")
import itertools
v1_routes = []
for tbl in (hp._DECOY_BASIC, hp._DECOY_FULL):
    v1_routes += [p for p in tbl if '/api/v1/' in p]
v1_routes += [p for p in hp._NOISE if isinstance(p, tuple) and '/api/v1/' in p[0]]
check("诱饵路由已无 /api/v1/", len(v1_routes) == 0, f"残留={v1_routes}")
check("陷阱 token 对齐真实会话 token(2048 hex)", hp._TRAP_TOKEN_HEX == 2048,
      f"当前={hp._TRAP_TOKEN_HEX}")
check("前端诱饵端点已定义", set(hp._FRONTEND_DECOYS.keys()) ==
      {'/api/internal/telemetry', '/api/internal/bootstrap',
       '/api/internal/feature_flags', '/api/internal/debug-config'})

print(f"\n结果：PASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
