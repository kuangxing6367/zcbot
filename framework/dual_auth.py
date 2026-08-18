"""
双请求防破解认证系统 (Dual-Request Anti-Cracking Auth System)
================================================================
通过“欺骗性防御”与“流程完整性检查”保护 ZCBOT 管理后台免受暴力破解与自动化脚本攻击。

核心机制：客户端必须按严格顺序发送两次请求
  1. 探针请求：携带 fake_token_len 位任意字符串 → 服务端下发 nonce（与 IP 绑定，nonce_expiry 秒有效）
  2. 真实认证：携带 real_token_len 位 Token + 上一步的 nonce → 校验通过即放行（一次性）

任何破坏该顺序的行为（跳过探针、nonce 缺失/错误、Token 长度异常）都会被判定为恶意，
对应 IP 将被拉入黑名单（持久化到数据库 ip_blacklist 表）。

内网 IP 豁免：127.*、192.168.*、172.16-31.*（含 172.*）等内网地址不会被自动拉黑，
避免误伤本机/局域网内的真实管理员与扫描器自查。

状态说明：
  - nonce 缓存：IP -> {nonce, expires_at}，内存存储，到期自动清理
  - 风险 IP：发送过探针请求的 IP（仅观察，不阻断）
  - 黑名单：触发恶意条件的 IP（持久化，可设置过期时间，可由超管手动解封）
  - 白名单：跳过所有检查的受信 IP
"""
import datetime
import logging
import secrets
import string
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger('zcbot')

# nonce 缓存上限：超过后清理过期条目并剔除最老一半（防内存无限增长）
_MAX_NONCE_CACHE = 10000

# 迷惑性数据默认值：认证通过后返回，诱导攻击者误以为得手（实际不可用于真实后台）
_DEFAULT_FAKE_DATA = {
    "token": "ZCBOT-DEADBEEF-FAKE-SESSION-TOKEN-PLEASE-DONOT-USE-IT",
    "username": "admin",
    "role": "super",
    "expire": 86400,
}


def is_internal_ip(ip: str) -> bool:
    """
    判断 IP 是否为内网/回环地址（这些地址不自动拉黑）
    覆盖：127.*.*.*、192.168.*.*、172.16-31.*.*（宽松按 172.*.*.*）、::1、localhost
    """
    if not ip:
        return False
    ip = ip.strip().lower()
    if ip in ('localhost', '::1', '127.0.0.1'):
        return True
    # IPv6 内网前缀
    if ip.startswith('fc') or ip.startswith('fd') or ip.startswith('fe80'):
        return True
    if ip.startswith('127.') or ip.startswith('192.168.'):
        return True
    if ip.startswith('172.'):
        try:
            second = int(ip.split('.')[1])
            return 16 <= second <= 31
        except (ValueError, IndexError):
            return False
    # 10.*.*.* 内网
    if ip.startswith('10.'):
        return True
    return False


class DualRequestAuthSystem:
    """双请求防破解认证系统核心"""

    def __init__(self, config: Optional[dict] = None, db=None):
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

        # 数据库引用（用于黑名单持久化；为空则退化为纯内存模式）
        self.db = db

        # 迷惑性数据：允许完全自定义，缺失或非 dict 时使用默认
        sfd = sec.get("success_fake_data")
        self.success_fake_data = sfd if isinstance(sfd, dict) else dict(_DEFAULT_FAKE_DATA)

        # IP -> {"nonce": str, "expires_at": float}
        self._nonce_cache = {}
        # 发送过探针请求的 IP（风险观察）
        self._risk_ips = set()
        # 内存黑名单缓存：ip -> {"reason": str, "source": str, "expires_at": float|None}
        self._blacklist = {}
        self._lock = threading.Lock()

        # 启动时从数据库加载已有黑名单
        if self.db is not None:
            self._load_blacklist_from_db()

    # ------------------------------------------------------------------
    # 数据库持久化
    # ------------------------------------------------------------------

    def _load_blacklist_from_db(self):
        """启动时加载 ip_blacklist 表到内存缓存（已过期的跳过）"""
        try:
            rows = self.db.query("SELECT ip, reason, source, expires_at FROM ip_blacklist")
            now = time.time()
            for r in rows:
                expires_at = r.get('expires_at')
                ts = self._parse_db_time(expires_at)
                if ts is not None and ts <= now:
                    continue  # 已过期，跳过
                with self._lock:
                    self._blacklist[r['ip']] = {
                        'reason': r.get('reason') or '',
                        'source': r.get('source') or 'manual',
                        'expires_at': ts,
                    }
            if self._blacklist:
                logger.info(f"已加载 {len(self._blacklist)} 条 IP 黑名单")
        except Exception as e:
            logger.warning(f"加载 IP 黑名单失败: {e}")

    @staticmethod
    def _parse_db_time(value):
        """将数据库时间（str/datetime/None）转为 epoch 秒"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime.datetime):
            return value.timestamp()
        try:
            return datetime.datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S').timestamp()
        except (ValueError, TypeError):
            try:
                return datetime.datetime.fromisoformat(str(value)).timestamp()
            except (ValueError, TypeError):
                return None

    def _db_blacklist(self, ip: str, reason: str, source: str, expires_at):
        """写入数据库（insert or update，基于 ip 唯一键）"""
        if self.db is None:
            return
        try:
            existing = self.db.query_one(
                "SELECT id FROM ip_blacklist WHERE ip = %s", (ip,)
            )
            if existing:
                self.db.execute(
                    "UPDATE ip_blacklist SET reason=%s, source=%s, expires_at=%s, updated_at=NOW() "
                    "WHERE ip=%s",
                    (reason, source, expires_at, ip)
                )
            else:
                self.db.execute(
                    "INSERT INTO ip_blacklist (ip, reason, source, expires_at, created_at) "
                    "VALUES (%s, %s, %s, %s, NOW())",
                    (ip, reason, source, expires_at)
                )
        except Exception as e:
            logger.warning(f"IP 黑名单写入失败 [{ip}]: {e}")

    def _db_unblacklist(self, ip: str):
        """从数据库删除"""
        if self.db is None:
            return
        try:
            self.db.execute("DELETE FROM ip_blacklist WHERE ip = %s", (ip,))
        except Exception as e:
            logger.warning(f"IP 黑名单删除失败 [{ip}]: {e}")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _gen_nonce(self) -> str:
        """生成长度为 nonce_len 的随机字符串（字母+数字）"""
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(self.nonce_len))

    def _cleanup_expired(self):
        """清理过期 nonce（调用前需持有锁）；超上限时再剔除最老的一半，防内存无限增长"""
        now = time.time()
        for ip in [ip for ip, d in self._nonce_cache.items() if d["expires_at"] <= now]:
            self._nonce_cache.pop(ip, None)
        if len(self._nonce_cache) > _MAX_NONCE_CACHE:
            sorted_items = sorted(
                self._nonce_cache.items(), key=lambda kv: kv[1]["expires_at"])
            for ip, _ in sorted_items[:len(sorted_items) // 2]:
                self._nonce_cache.pop(ip, None)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def is_whitelisted(self, ip: str) -> bool:
        return ip in self.whitelist_ips

    def is_internal(self, ip: str) -> bool:
        """是否为内网/回环 IP（自动豁免拉黑）"""
        return is_internal_ip(ip)

    def is_blacklisted(self, ip: str) -> bool:
        if not self.blacklist_enabled or not ip:
            return False
        if self.is_whitelisted(ip):
            return False
        with self._lock:
            entry = self._blacklist.get(ip)
            if entry is None:
                return False
            # 过期自动解封
            if entry.get('expires_at') is not None and entry['expires_at'] <= time.time():
                self._blacklist.pop(ip, None)
                return False
            return True

    def get_status(self) -> dict:
        """返回当前系统状态（供管理端点展示）"""
        with self._lock:
            self._cleanup_expired()
            blacklist_entries = [
                {
                    'ip': ip,
                    'reason': d.get('reason', ''),
                    'source': d.get('source', 'manual'),
                    'expires_at': d.get('expires_at'),
                }
                for ip, d in self._blacklist.items()
            ]
            return {
                "risk_ips": sorted(self._risk_ips),
                "blacklist": [e['ip'] for e in blacklist_entries],
                "blacklist_details": sorted(blacklist_entries, key=lambda x: x['ip']),
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

    def get_blacklist(self) -> list:
        """返回黑名单详情列表（含来源/原因/过期时间）"""
        with self._lock:
            return sorted(
                [
                    {
                        'ip': ip,
                        'reason': d.get('reason', ''),
                        'source': d.get('source', 'manual'),
                        'expires_at': d.get('expires_at'),
                    }
                    for ip, d in self._blacklist.items()
                ],
                key=lambda x: x['ip']
            )

    # ------------------------------------------------------------------
    # 黑名单管理
    # ------------------------------------------------------------------

    def blacklist(self, ip: str, reason: str = "", source: str = "honeypot",
                  expires_at=None):
        """
        将 IP 加入黑名单（持久化 + 内存缓存）
        :param ip: 目标 IP
        :param reason: 拉黑原因
        :param source: 来源（honeypot=蜜罐自动 / manual=手动）
        :param expires_at: 解封时间（epoch 秒，None=永久）
        """
        if not self.blacklist_enabled or not ip:
            return
        # 内网 IP 自动豁免（不自动拉黑，避免误伤本机/局域网）
        if source == 'honeypot' and self.is_internal(ip):
            logger.info(f"[双请求认证] 内网 IP 豁免拉黑: {ip}")
            return
        with self._lock:
            self._blacklist[ip] = {
                'reason': reason or '',
                'source': source,
                'expires_at': expires_at,
            }
        self._db_blacklist(ip, reason or '', source, self._fmt_expires(expires_at))
        if expires_at:
            logger.warning(f"[双请求认证] IP 已加入黑名单（{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at))} 解封）: {ip} ({reason})")
        else:
            logger.warning(f"[双请求认证] IP 已加入永久黑名单: {ip} ({reason})")

    @staticmethod
    def _fmt_expires(expires_at) -> Optional[str]:
        """epoch 秒 → 数据库时间字符串；None 原样返回"""
        if expires_at is None:
            return None
        try:
            return datetime.datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError, OverflowError):
            return None

    def unblacklist(self, ip: str) -> bool:
        """从黑名单移除 IP，并清理其风险/nonce 记录"""
        with self._lock:
            existed = ip in self._blacklist
            self._blacklist.pop(ip, None)
            self._risk_ips.discard(ip)
            self._nonce_cache.pop(ip, None)
        if existed or True:
            self._db_unblacklist(ip)
        if existed:
            logger.info(f"[双请求认证] IP 已从黑名单移除: {ip}")
        return existed

    def add_manual_blacklist(self, ip: str, reason: str = "", expires_at=None) -> bool:
        """
        手动拉黑（业务接口，Web UI 调用）
        内网 IP 允许手动拉黑（管理员明确指定），但提示风险由调用方决定
        """
        if not ip:
            return False
        self.blacklist(ip, reason or '手动拉黑', source='manual', expires_at=expires_at)
        return True

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
            # nonce 缺失 / 错误 / 过期 / 跳过探针直接发 8192 位 Token → 封禁
            self.blacklist(ip, "nonce 缺失/错误/过期或跳过探针")
            return 403, {"code": 403, "msg": "认证失败，IP 已被封禁"}

        logger.info(f"[双请求认证] 双请求流程校验通过: {ip}")
        return 200, {
            "code": 0,
            "msg": "登录成功",
            "data": dict(self.success_fake_data),
        }
