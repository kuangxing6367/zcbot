"""
双请求防破解认证系统 (Dual-Request Anti-Cracking Auth System)
================================================================
通过“欺骗性防御”与“流程完整性检查”保护 ZCBOT 管理后台免受暴力破解与自动化脚本攻击。

核心机制：客户端必须按严格顺序发送两次请求
  1. 探针请求：携带 fake_token_len 位任意字符串 → 服务端下发 nonce（与 IP 绑定，nonce_expiry 秒有效）
  2. 真实认证：携带 real_token_len 位 Token + 上一步的 nonce → 校验通过即放行（一次性）

任何破坏该顺序的行为（跳过探针、nonce 缺失/错误、Token 长度异常）都会被判定为恶意，
对应 IP 将被拉入永久黑名单。

状态说明：
  - nonce 缓存：IP -> {nonce, expires_at}，内存存储，到期自动清理
  - 风险 IP：发送过探针请求的 IP（仅观察，不阻断）
  - 黑名单：触发恶意条件的 IP（永久阻断，可由超管手动解封）
  - 白名单：跳过所有检查的受信 IP
"""
import logging
import secrets
import string
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger('zcbot')

# 迷惑性数据默认值：认证通过后返回，诱导攻击者误以为得手（实际不可用于真实后台）
_DEFAULT_FAKE_DATA = {
    "token": "ZCBOT-DEADBEEF-FAKE-SESSION-TOKEN-PLEASE-DONOT-USE-IT",
    "username": "admin",
    "role": "super",
    "expire": 86400,
}


class DualRequestAuthSystem:
    """双请求防破解认证系统核心"""

    def __init__(self, config: Optional[dict] = None):
        sec = config or {}
        self.fake_token_len = int(sec.get("fake_token_len", 8))
        self.real_token_len = int(sec.get("real_token_len", 8192))
        self.nonce_len = int(sec.get("nonce_len", 16))
        self.fake_response_msg = sec.get(
            "fake_response_msg",
            "🎣 你上钩了！但这里只是蜜罐，请去 GitHub 点个 Star。"
        )
        self.nonce_expiry = int(sec.get("nonce_expiry", 60))
        self.blacklist_enabled = bool(sec.get("blacklist_enabled", True))
        self.whitelist_ips = set(sec.get("whitelist_ips", ["127.0.0.1"]))

        # 迷惑性数据：允许完全自定义，缺失或非 dict 时使用默认
        sfd = sec.get("success_fake_data")
        self.success_fake_data = sfd if isinstance(sfd, dict) else dict(_DEFAULT_FAKE_DATA)

        # IP -> {"nonce": str, "expires_at": float}
        self._nonce_cache = {}
        # 发送过探针请求的 IP（风险观察）
        self._risk_ips = set()
        # 永久黑名单
        self._blacklist = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _gen_nonce(self) -> str:
        """生成长度为 nonce_len 的随机字符串（字母+数字）"""
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(self.nonce_len))

    def _cleanup_expired(self):
        """清理过期 nonce（调用前需持有锁）"""
        now = time.time()
        for ip in [ip for ip, d in self._nonce_cache.items() if d["expires_at"] <= now]:
            self._nonce_cache.pop(ip, None)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def is_whitelisted(self, ip: str) -> bool:
        return ip in self.whitelist_ips

    def is_blacklisted(self, ip: str) -> bool:
        if not self.blacklist_enabled:
            return False
        with self._lock:
            return ip in self._blacklist

    def get_status(self) -> dict:
        """返回当前系统状态（供管理端点展示）"""
        with self._lock:
            self._cleanup_expired()
            return {
                "risk_ips": sorted(self._risk_ips),
                "blacklist": sorted(self._blacklist),
                "pending_nonces": len(self._nonce_cache),
                "whitelist_ips": sorted(self.whitelist_ips),
                "blacklist_enabled": self.blacklist_enabled,
                "config": {
                    "fake_token_len": self.fake_token_len,
                    "real_token_len": self.real_token_len,
                    "nonce_len": self.nonce_len,
                    "nonce_expiry": self.nonce_expiry,
                },
            }

    # ------------------------------------------------------------------
    # 黑名单管理
    # ------------------------------------------------------------------

    def blacklist(self, ip: str, reason: str = ""):
        """将 IP 加入永久黑名单"""
        if not self.blacklist_enabled or not ip:
            return
        with self._lock:
            self._blacklist.add(ip)
        logger.warning(f"[双请求认证] IP 已加入永久黑名单: {ip} ({reason})")

    def unblacklist(self, ip: str) -> bool:
        """从黑名单移除 IP，并清理其风险/nonce 记录"""
        with self._lock:
            existed = ip in self._blacklist
            self._blacklist.discard(ip)
            self._risk_ips.discard(ip)
            self._nonce_cache.pop(ip, None)
        if existed:
            logger.info(f"[双请求认证] IP 已从黑名单移除: {ip}")
        return existed

    # ------------------------------------------------------------------
    # 核心处理
    # ------------------------------------------------------------------

    def handle_request(self, ip: str, token, nonce) -> Tuple[int, dict]:
        """
        处理 /api/auth 请求，返回 (http_status, response_dict)
        :param ip: 客户端 IP
        :param token: 请求体中的 token（字符串）
        :param nonce: 请求体中的 nonce（第二次请求需携带）
        """
        # 白名单：跳过所有检查
        if self.is_whitelisted(ip):
            return 200, {
                "code": 0,
                "msg": "登录成功",
                "data": dict(self.success_fake_data),
            }

        # 黑名单：直接拒绝
        if self.is_blacklisted(ip):
            return 403, {"code": 403, "msg": "访问被拒绝"}

        # token 必须是字符串
        if not isinstance(token, str) or not token:
            self.blacklist(ip, "token 缺失或类型错误")
            return 403, {"code": 403, "msg": "恶意请求，IP 已被封禁"}

        token_len = len(token)

        # 第一次请求（探针）
        if token_len == self.fake_token_len:
            return self._handle_probe(ip)

        # 第二次请求（真实认证）
        if token_len == self.real_token_len:
            return self._handle_auth(ip, nonce)

        # 既非 fake 长度也非 real 长度 → 恶意
        self.blacklist(ip, f"Token 长度异常: {token_len}")
        return 403, {"code": 403, "msg": "恶意请求，IP 已被封禁"}

    def _handle_probe(self, ip: str) -> Tuple[int, dict]:
        """处理探针请求：下发 nonce 并标记风险 IP"""
        nonce = self._gen_nonce()
        with self._lock:
            self._cleanup_expired()
            self._nonce_cache[ip] = {
                "nonce": nonce,
                "expires_at": time.time() + self.nonce_expiry,
            }
            self._risk_ips.add(ip)
        logger.info(f"[双请求认证] 探针请求，已向风险 IP 下发 nonce: {ip}")
        return 200, {
            "code": 200,
            "msg": self.fake_response_msg,
            "nonce": nonce,
            "expires_in": self.nonce_expiry,
        }

    def _handle_auth(self, ip: str, nonce) -> Tuple[int, dict]:
        """处理真实认证请求：校验 nonce（一次性）与 IP 绑定"""
        valid = False
        with self._lock:
            self._cleanup_expired()
            entry = self._nonce_cache.get(ip)
            if nonce and entry and entry["nonce"] == nonce:
                # 一次性：校验通过后立即删除，防止重放
                self._nonce_cache.pop(ip, None)
                valid = True

        if not valid:
            # nonce 缺失 / 错误 / 过期 / 跳过探针直接发 8192 位 Token → 永久封禁
            self.blacklist(ip, "nonce 缺失/错误/过期或跳过探针")
            return 403, {"code": 403, "msg": "认证失败，IP 已被永久封禁"}

        logger.info(f"[双请求认证] 双请求流程校验通过: {ip}")
        return 200, {
            "code": 0,
            "msg": "登录成功",
            "data": dict(self.success_fake_data),
        }
