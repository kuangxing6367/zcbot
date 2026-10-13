# -*- coding: utf-8 -*-
"""
会话令牌协议（Session Tokens）—— 默认关闭
==========================================
给 WebUI 会话叠一层「服务端下发、客户端拆乱回传」的令牌集，提高抓包/重放/自动化
分析的复杂度。**默认关闭，开启前的行为完全不变。**

流程
----
1. POST /api/session/handshake
   服务端为来源 IP 生成一个 **2048 位主令牌**（256 字节 = 512 hex），一次性下发。
2. 客户端把主令牌**切成 6 份、随机打乱**，把 6 个令牌回传（可带排列序号）。
3. POST /api/session/confirm
   服务端按集合一致性校验（不依赖顺序），保存这 6 个令牌作为本次会话的令牌集，
   **返回 444（静默）**，并**立即废弃主令牌**（一次性）。
4. 之后每个请求客户端**上报全部 6 个令牌**，服务端校验集合匹配且未过期。
5. **固定 30 分钟**（不滚动续期）；全部过期后服务端对业务请求返回 444 →
   客户端现有逻辑（444 = 会话失效）自动回到登录流程，重新握手。

边界（诚实说明）
----------------
这是**抬高分析成本**的手段，不是密码学意义上的增强：主令牌与 6 个令牌都在同一次
握手中出现过。它解决的是「抓包看不到固定凭据、静态分析拿不到接口凭据、凭据 30 分钟
一换」，而不是「防住能读懂协议的攻击者」。

配置（config.yaml → security.session_tokens）
----------------------------------------------
    enabled: false        # 总开关（默认关）
    ttl: 1800             # 令牌有效期（秒，固定 30 分钟，不滚动）
    tokens: 6             # 令牌份数
    master_bits: 2048     # 主令牌位数
"""
import logging
import math
import secrets
import threading
import time

from flask import Response, jsonify, request

logger = logging.getLogger('zcbot')

_lock = threading.Lock()
_pending = {}    # ip -> {'master': str, 'issued_at': float}        握手态（未确认）
_sessions = {}   # sid -> {'tokens': set, 'expires_at': float, 'ip': str}

_DEFAULT_TTL = 1800          # 固定 30 分钟
_DEFAULT_TOKENS = 6
_PENDING_TTL = 120           # 握手态有效期
_MAX_SESSIONS = 4096
_MAX_PENDING = 4096


def _split_even(s: str, n: int) -> list:
    """把字符串尽量均分成 n 份（最后一份可能短一些）"""
    size = math.ceil(len(s) / n)
    return [s[i:i + size] for i in range(0, len(s), size)]


def _purge(now: float):
    for k in [k for k, v in _pending.items() if now - v['issued_at'] > _PENDING_TTL]:
        _pending.pop(k, None)
    for k in [k for k, v in _sessions.items() if v['expires_at'] <= now]:
        _sessions.pop(k, None)
    if len(_sessions) > _MAX_SESSIONS:
        for k in sorted(_sessions, key=lambda x: _sessions[x]['expires_at'])[:len(_sessions) // 2]:
            _sessions.pop(k, None)
    if len(_pending) > _MAX_PENDING:
        for k in list(_pending)[:len(_pending) // 2]:
            _pending.pop(k, None)


def issue_master(ip: str, master_bits: int = 2048) -> str:
    """下发一个一次性主令牌（2048 位 = 256 字节 = 512 hex）"""
    master = secrets.token_hex(max(1, master_bits // 8))
    now = time.time()
    with _lock:
        _purge(now)
        _pending[ip] = {'master': master, 'issued_at': now}
    return master


def confirm(ip: str, tokens, ttl: int = _DEFAULT_TTL):
    """
    校验客户端回传的 6 个令牌：与主令牌切分结果**集合一致**即通过（不依赖顺序）。
    成功后保存令牌集并废弃主令牌；返回 sid，失败返回 None。
    """
    if not isinstance(tokens, (list, tuple)) or not tokens:
        return None
    now = time.time()
    with _lock:
        _purge(now)
        entry = _pending.pop(ip, None)     # 主令牌一次性：无论成败都作废
    if not entry:
        return None

    expected = set(_split_even(entry['master'], _DEFAULT_TOKENS))
    got = set(str(t) for t in tokens)
    if got != expected:
        return None

    sid = secrets.token_urlsafe(24)
    with _lock:
        _sessions[sid] = {
            'tokens': got,
            'expires_at': now + max(60, int(ttl)),
            'ip': ip,
        }
    return sid


def parse_header(value: str):
    """解析 'sid:t1,t2,...' 形式的请求头；失败返回 (None, None)"""
    if not value or ':' not in value:
        return None, None
    sid, _, rest = value.partition(':')
    tokens = [t for t in rest.split(',') if t]
    return sid or None, tokens or None


def validate(sid, tokens) -> bool:
    """客户端上报全部令牌：集合必须完全一致，且未过期"""
    if not sid or not tokens:
        return False
    now = time.time()
    with _lock:
        s = _sessions.get(sid)
        if s is None:
            return False
        if s['expires_at'] <= now:
            _sessions.pop(sid, None)
            return False
        return set(str(t) for t in tokens) == s['tokens']


def drop(sid):
    with _lock:
        _sessions.pop(sid, None)


def stats() -> dict:
    now = time.time()
    with _lock:
        _purge(now)
        return {'pending': len(_pending), 'active_sessions': len(_sessions)}


def register(ctx):
    """注册握手端点 + 请求闸门（由 create_web_app 调用）"""
    app = ctx.app
    framework = ctx.framework
    sec = framework.config.get('security') or {}
    cfg = sec.get('session_tokens') or {}

    if not cfg.get('enabled'):
        logger.info('[会话令牌] 已禁用（security.session_tokens.enabled 未开启）')
        return

    ttl = int(cfg.get('ttl', _DEFAULT_TTL) or _DEFAULT_TTL)
    master_bits = int(cfg.get('master_bits', 2048) or 2048)
    get_client_ip = ctx.get_client_ip

    def _silent():
        return Response(b'', status=444, mimetype='text/plain')

    @app.route('/api/session/handshake', methods=['POST'])
    def _session_handshake():
        ip = get_client_ip()
        master = issue_master(ip, master_bits=master_bits)
        return jsonify({'code': 0, 'msg': 'ok', 'data': {'master': master}})

    @app.route('/api/session/confirm', methods=['POST'])
    def _session_confirm():
        ip = get_client_ip()
        data = request.get_json(silent=True) or {}
        tokens = data.get('tokens') or []
        sid = confirm(ip, tokens, ttl=ttl)
        if not sid:
            # 校验失败：静默丢弃，不告诉对方为什么
            return _silent()
        # 协议约定：确认成功返回 444（静默），sid 通过 HttpOnly Cookie 下发
        # —— JS 读不到 sid，只负责在每个请求里带上 6 个令牌，sid 由服务端自读。
        resp = _silent()
        resp.set_cookie('zcbot_sid', sid, max_age=ttl, path='/',
                        httponly=True, samesite='Lax',
                        secure=bool(request.is_secure))
        return resp

    # ---- 闸门：开启后，业务 /api 请求必须带有效令牌集 ----
    _EXEMPT = ('/api/session/handshake', '/api/session/confirm',
               '/api/challenge', '/api/challenge/verify', '/api/login')

    @app.before_request
    def _session_gate():
        path = request.path or ''
        if not path.startswith('/api/'):
            return None
        if request.method == 'OPTIONS':
            return None
        for p in _EXEMPT:
            if path == p or path.startswith(p + '/'):
                return None
        # sid 由服务端自读 HttpOnly Cookie；令牌由客户端放在 X-ZC-Set 请求头
        # （逗号分隔的 6 个令牌），Cookie 里的 zcbot_tset 作为兜底。
        sid = request.cookies.get('zcbot_sid') or ''
        raw = request.headers.get('X-ZC-Set') or request.cookies.get('zcbot_tset') or ''
        tokens = [t for t in raw.split(',') if t]
        if validate(sid, tokens):
            return None
        return _silent()

    logger.info(f'[会话令牌] 已启用（ttl={ttl}s, 主令牌={master_bits}bit, 令牌数={_DEFAULT_TOKENS}）')
