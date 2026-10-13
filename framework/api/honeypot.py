# -*- coding: utf-8 -*-
"""
蜜罐 / 诱饵端点模块（Honeypot / Decoy Endpoints）
=================================================
面向扫描器、自动化爆破与「顺手摸一下」的攻击者，提供大量**看起来极其真实**的
诱饵端点：假后台、假 .env、假 Git 泄露、假数据库导出、假密钥、假 OpenAPI、
假 Spring Actuator / Docker API、假 CI 配置……命中即记录、拉黑来源 IP，
并用随机延迟（tarpit）消耗扫描器预算。

本模块的全部行为由 `security.level` 档位统一驱动（见 framework/api/security_levels.py），
单个键可显式覆盖档位预设。

设计原则
--------
1. **零冲突**：诱饵路径绝不与真实路由重名；真实路由在 Werkzeug 路由表中权重更高，
   本模块只兜底「未被任何真实规则匹配」的路径。
2. **纯伪造**：所有返回的密钥 / 凭据 / Token 均为随机生成的假数据，
   不具备任何真实系统访问能力，仅用于钓鱼取证。
3. **内网豁免**：内网 / 白名单 IP 只记录不拉黑，避免误伤本机与局域网管理员。
4. **可关可调**：总开关 `security.level`（off/low/medium/high/paranoid）。

案例驱动的三项增强
------------------
- **canary（唯一追踪串）**：每次命中在响应体内植入一个唯一标识并记录映射关系。
  日后若某份"内部配置"在网上流出，可据此反查它是**哪一次访问**漏出去的。
  （源自公开案例：蜜罐作者用此法追溯泄露路径）
- **evasion_detect（绕过检测）**：识别 `/.//.env`、`%2eenv`、双重编码、路径穿越等
  路径变形手法 —— 会做这一步的说明**已经上钩**，属高价值告警。
- **诱饵集分级**：basic（核心）/ full（全量），由档位决定。
"""
import base64
import json
import logging
import random
import re
import string
import threading
import time

from flask import Response, jsonify, request

logger = logging.getLogger('zcbot')

# 诱饵系统对外自报的身份（用于让假数据前后一致，更像一个真实内网系统）
_FAKE_HOST = 'zcbot-node-01'
_FAKE_DOMAIN = 'internal.zcbot.local'
_FAKE_INTERNAL_IP = '10.20.30.15'

# ---------------------------------------------------------------------------
# 共享 tarpit 闸门
# ---------------------------------------------------------------------------
# 延迟会让请求线程休眠。若不设上限，攻击者可用海量请求把工作线程全部占满，
# 形成「自我 DoS」。这里对并发休眠请求设硬上限，超出即直接放行（不延迟）。
_tarpit_active = 0
_tarpit_lock = threading.Lock()
_MAX_CONCURRENT_TARPIT = 16


def tarpit(ms: int):
    """随机延迟 0~ms 毫秒；并发超过闸门上限时跳过延迟，避免拖垮工作线程"""
    if not ms or ms <= 0:
        return
    global _tarpit_active
    with _tarpit_lock:
        if _tarpit_active >= _MAX_CONCURRENT_TARPIT:
            return
        _tarpit_active += 1
    try:
        time.sleep(random.uniform(0, ms) / 1000.0)
    finally:
        with _tarpit_lock:
            _tarpit_active -= 1


# ---------------------------------------------------------------------------
# 命中统计（内存，进程级；供 /api/security/status 展示威胁画像）
# ---------------------------------------------------------------------------
_MAX_STAT_KEYS = 512
_stats = {
    'total_hits': 0,
    'evasion_hits': 0,
    'canary_issued': 0,
    'blacklisted': 0,
    'first_hit_at': None,
    'last_hit_at': None,
    'by_ip': {},
    'by_path': {},
    'by_reason': {},
}
_stats_lock = threading.Lock()


def _bump(mapping: dict, key: str):
    """受容量上限保护的计数器自增（超出则剔除计数最小的键）"""
    if not key:
        return
    mapping[key] = mapping.get(key, 0) + 1
    if len(mapping) > _MAX_STAT_KEYS:
        for k in sorted(mapping, key=lambda x: mapping[x])[:len(mapping) // 2]:
            mapping.pop(k, None)


def get_stats(top_n: int = 10) -> dict:
    """返回命中统计摘要（含 TOP IP / 路径 / 原因）"""
    with _stats_lock:
        def top(d):
            return [{'key': k, 'count': v}
                    for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]
        return {
            'total_hits': _stats['total_hits'],
            'evasion_hits': _stats['evasion_hits'],
            'canary_issued': _stats['canary_issued'],
            'blacklisted': _stats['blacklisted'],
            'first_hit_at': _stats['first_hit_at'],
            'last_hit_at': _stats['last_hit_at'],
            'top_ips': top(_stats['by_ip']),
            'top_paths': top(_stats['by_path']),
            'top_reasons': top(_stats['by_reason']),
            'unique_ips': len(_stats['by_ip']),
        }


def reset_stats():
    """清空统计（供管理端调用）"""
    with _stats_lock:
        _stats.update({'total_hits': 0, 'evasion_hits': 0, 'canary_issued': 0,
                       'blacklisted': 0, 'first_hit_at': None, 'last_hit_at': None,
                       'by_ip': {}, 'by_path': {}, 'by_reason': {}})


# ---------------------------------------------------------------------------
# canary：唯一追踪串
# ---------------------------------------------------------------------------
_canary_lock = threading.Lock()
_canary_index = {}          # canary -> {ip, path, ua, at}（有界）
_MAX_CANARY = 4096


def _new_canary() -> str:
    """生成一个形似内部构建号 / 请求 ID 的唯一追踪串"""
    return 'zcbot-' + ''.join(random.choice('0123456789abcdef') for _ in range(12))


def _remember_canary(canary: str, ip: str, path: str, ua: str):
    with _canary_lock:
        _canary_index[canary] = {'ip': ip, 'path': path, 'ua': ua,
                                 'at': time.strftime('%Y-%m-%d %H:%M:%S')}
        if len(_canary_index) > _MAX_CANARY:
            for k in list(_canary_index)[:len(_canary_index) // 2]:
                _canary_index.pop(k, None)


def lookup_canary(canary: str):
    """反查 canary 对应的访问记录（用于事后追溯泄露路径）"""
    with _canary_lock:
        return _canary_index.get(canary)


def _inject_canary(resp, canary: str):
    """把追踪串植入响应体（按内容类型选择最自然的落点）"""
    try:
        ctype = (resp.headers.get('Content-Type') or '').lower()
        if any(k in ctype for k in ('zip', 'octet-stream', 'image/', 'font/')):
            return
        if getattr(resp, 'direct_passthrough', False):
            resp.direct_passthrough = False
        data = resp.get_data(as_text=True)
        if not data:
            return
        if 'json' in ctype:
            try:
                obj = json.loads(data)
            except Exception:
                return
            if isinstance(obj, dict):
                obj['request_id'] = canary
                resp.set_data(json.dumps(obj, ensure_ascii=False))
            return
        if 'html' in ctype:
            tag = f'<!-- build {canary} -->'
            data = data.replace('</body>', f'{tag}\n</body>') if '</body>' in data else data + tag
        elif 'sql' in ctype:
            data = data + f"\n-- dump-id: {canary}\n"
        elif 'yaml' in ctype:
            data = data + f"\n# build: {canary}\n"
        else:
            # 文本类（.env / git config / 日志等）：伪装成运维留下的构建标记
            data = data + f"\n# deployed-build: {canary}\n"
        resp.set_data(data)
    except Exception as e:
        logger.debug(f"[蜜罐] canary 注入失败: {e}")


# ---------------------------------------------------------------------------
# 绕过检测：识别路径变形手法
# ---------------------------------------------------------------------------
_EVASION_RULES = [
    (re.compile(r'%25[0-9a-f]{2}', re.I), '双重 URL 编码'),
    (re.compile(r'%2e|%2f|%5c', re.I), 'URL 编码路径字符'),
    (re.compile(r'\.\./|\.\.\\|%2e%2e', re.I), '路径穿越'),
    (re.compile(r'%00'), '空字节注入'),
    (re.compile(r'//'), '重复斜杠'),
    (re.compile(r'\\\\'), '反斜杠路径'),
]


def _raw_uri() -> str:
    """尽力取得未经规范化的原始 URI（不同 WSGI 服务器字段不同）"""
    env = request.environ
    for key in ('RAW_URI', 'REQUEST_URI'):
        v = env.get(key)
        if v:
            return str(v)
    path = env.get('PATH_INFO') or request.path or ''
    qs = env.get('QUERY_STRING') or ''
    return path + (('?' + qs) if qs else '')


def detect_evasion() -> str:
    """返回命中的绕过手法名称；未命中返回空串"""
    uri = _raw_uri()
    for pattern, label in _EVASION_RULES:
        if pattern.search(uri):
            return label
    return ''


# ===========================================================================
# 假数据生成器（全部为随机伪造，不可用于任何真实系统）
# ===========================================================================

def _rand_hex(n: int) -> str:
    return ''.join(random.choice('0123456789abcdef') for _ in range(n))


def _rand_alnum(n: int) -> str:
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _rand_upper(n: int) -> str:
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def fake_jwt() -> str:
    """生成形似真实 JWT 的假令牌（签名段为随机串，无法通过任何真实校验）"""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"},
                                separators=(',', ':')).encode())
    payload = _b64url(json.dumps({
        "sub": "1", "name": "admin", "role": "super",
        "iat": int(time.time()) - 3600,
        "exp": int(time.time()) + 86400,
    }, separators=(',', ':')).encode())
    return f"{header}.{payload}.{_b64url(_rand_alnum(43).encode())}"


def fake_aws_key() -> str:
    return 'AKIA' + _rand_upper(16)


def fake_secret(n: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits + '+/'
    return ''.join(random.choice(alphabet) for _ in range(n))


def fake_bcrypt_hash() -> str:
    return '$2b$12$' + _rand_alnum(22) + _rand_alnum(31)


def fake_ip() -> str:
    return f'10.20.{random.randint(0, 30)}.{random.randint(2, 250)}'


def fake_username() -> str:
    return random.choice(['admin', 'root', 'operator', 'deploy', 'service',
                          'ops', 'sysadmin', 'zcbot'])


# ===========================================================================
# 诱饵响应体
# ===========================================================================

def body_env_file() -> Response:
    """假 .env —— 最经典的「配置泄露」，用来钓扫描器"""
    txt = f"""# {_FAKE_DOMAIN} runtime environment
APP_ENV=production
APP_NAME=zcbot
APP_KEY=base64:{fake_secret(44)}
APP_DEBUG=false

DB_CONNECTION=mysql
DB_HOST={_FAKE_INTERNAL_IP}
DB_PORT=3306
DB_DATABASE=zcbot
DB_USERNAME=zcbot
DB_PASSWORD={fake_secret(20)}

REDIS_HOST=10.20.30.16
REDIS_PORT=6379
REDIS_PASSWORD={fake_secret(24)}

JWT_SECRET={fake_secret(48)}
JWT_TTL=86400

AWS_ACCESS_KEY_ID={fake_aws_key()}
AWS_SECRET_ACCESS_KEY={fake_secret(40)}
AWS_DEFAULT_REGION=ap-guangzhou

SMTP_HOST=smtp.{_FAKE_DOMAIN}
SMTP_USER=alert@{_FAKE_DOMAIN}
SMTP_PASSWORD={fake_secret(18)}

# 割接时临时放的，割完记得删（别提交进仓库）
"""
    return Response(txt, mimetype='text/plain')


def body_git_config() -> Response:
    txt = f"""[core]
\trepositoryformatversion = 0
\tfilemode = true
\tbare = false
\tlogallrefupdates = true
[remote "origin"]
\turl = git@{_FAKE_DOMAIN}:ops/zcbot-deploy.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
[branch "main"]
\tremote = origin
\tmerge = refs/heads/main
[user]
\tname = deploy
\temail = deploy@{_FAKE_DOMAIN}
"""
    return Response(txt, mimetype='text/plain')


def body_git_head() -> Response:
    return Response('ref: refs/heads/main\n', mimetype='text/plain')


def body_git_ref() -> Response:
    return Response(_rand_hex(40) + '\n', mimetype='text/plain')


def body_sql_dump() -> Response:
    """假数据库导出：含假 admin 表与假 bcrypt 口令哈希"""
    admin_rows = '\n'.join(
        f"({i}, '{fake_username()}', '{fake_bcrypt_hash()}', 'super', 1, NOW())"
        for i in range(1, 4))
    txt = f"""-- MySQL dump 10.13  Distrib 8.0.36, for Linux (x86_64)
-- Host: {_FAKE_INTERNAL_IP}    Database: zcbot
-- ------------------------------------------------------
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
DROP TABLE IF EXISTS `admin_users`;
CREATE TABLE `admin_users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role` varchar(16) DEFAULT 'admin',
  `is_active` tinyint(1) DEFAULT '1',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
LOCK TABLES `admin_users` WRITE;
INSERT INTO `admin_users` VALUES {admin_rows};
UNLOCK TABLES;
DROP TABLE IF EXISTS `api_tokens`;
CREATE TABLE `api_tokens` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(64) NOT NULL,
  `token` varchar(128) NOT NULL,
  `role` varchar(16) DEFAULT 'admin',
  `is_active` tinyint(1) DEFAULT '1',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `api_tokens` VALUES (1,'ci-deploy','{_rand_hex(64)}','super',1);
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
"""
    return Response(txt, mimetype='application/sql')


def body_aws_credentials() -> Response:
    txt = f"""[default]
aws_access_key_id = {fake_aws_key()}
aws_secret_access_key = {fake_secret(40)}
region = ap-guangzhou
"""
    return Response(txt, mimetype='text/plain')


def body_ssh_key() -> Response:
    b64 = '\n'.join(_rand_alnum(64) for _ in range(6))
    txt = (f"-----BEGIN OPENSSH PRIVATE KEY-----\n{b64}\n"
           f"{_rand_alnum(24)}\n-----END OPENSSH PRIVATE KEY-----\n")
    return Response(txt, mimetype='text/plain')


def body_private_key() -> Response:
    b64 = '\n'.join(_rand_alnum(64) for _ in range(12))
    return Response(f"-----BEGIN PRIVATE KEY-----\n{b64}\n-----END PRIVATE KEY-----\n",
                    mimetype='text/plain')


def body_cert_pem() -> Response:
    b64 = '\n'.join(_rand_alnum(64) for _ in range(10))
    return Response(f"-----BEGIN CERTIFICATE-----\n{b64}\n-----END CERTIFICATE-----\n",
                    mimetype='text/plain')


def body_docker_config() -> Response:
    return jsonify({
        "auths": {
            f"registry.{_FAKE_DOMAIN}": {
                "auth": _b64url(f"deploy:{fake_secret(24)}".encode()),
                "email": f"deploy@{_FAKE_DOMAIN}",
            }
        }
    })


def body_kube_config() -> Response:
    return Response(
        f"apiVersion: v1\nkind: Config\nclusters:\n"
        f"- cluster:\n    server: https://k8s.{_FAKE_DOMAIN}:6443\n"
        f"    certificate-authority-data: {_b64url(fake_secret(40).encode())}\n"
        f"  name: prod\ncontexts:\n- context:\n    cluster: prod\n"
        f"    user: deploy\n    namespace: default\n  name: prod\n"
        f"users:\n- name: deploy\n  user:\n    token: {fake_jwt()}\n",
        mimetype='text/yaml')


def body_npmrc() -> Response:
    return Response(
        f"registry=https://registry.npmjs.org/\n"
        f"//registry.npmjs.org/:_authToken={fake_secret(36)}\n",
        mimetype='text/plain')


def body_env_json() -> Response:
    """假配置 JSON：伪装成一个真实服务的配置中心响应"""
    return jsonify({
        "code": 0,
        "msg": "ok",
        "data": {
            "app": {"name": "zcbot", "env": "production", "host": _FAKE_HOST},
            "database": {
                "host": _FAKE_INTERNAL_IP, "port": 3306,
                "database": "zcbot", "username": "zcbot",
                "password": fake_secret(20),
            },
            "redis": {"host": "10.20.30.16", "port": 6379, "password": fake_secret(24)},
            "jwt": {"secret": fake_secret(48), "ttl": 86400},
            "storage": {"access_key": fake_aws_key(), "secret_key": fake_secret(40)},
        },
    })


def body_keys_json() -> Response:
    return jsonify({
        "code": 0,
        "data": {
            "jwt_secret": fake_secret(48),
            "session_key": fake_secret(32),
            "api_keys": [
                {"name": "ci-deploy", "token": _rand_hex(64), "role": "super"},
                {"name": "grafana", "token": _rand_hex(64), "role": "readonly"},
            ],
            "aws": {"access_key_id": fake_aws_key(),
                    "secret_access_key": fake_secret(40)},
            "oauth": {"client_id": _rand_alnum(32), "client_secret": fake_secret(40)},
        },
    })


def body_users_json() -> Response:
    users = [{
        "id": i,
        "username": fake_username(),
        "role": "super" if i == 1 else "admin",
        "email": f"user{i}@{_FAKE_DOMAIN}",
        "is_active": True,
        "last_login_ip": fake_ip(),
        "token": _rand_hex(64),
    } for i in range(1, 6)]
    return jsonify({"code": 0, "msg": "ok", "total": len(users), "data": users})


def body_login_success_json() -> Response:
    return jsonify({
        "code": 0,
        "msg": "登录成功",
        "data": {"token": fake_jwt(), "username": "admin", "role": "super",
                 "expire": 86400},
    })


def body_actuator_env() -> Response:
    return jsonify({
        "activeProfiles": ["prod"],
        "propertySources": [{
            "name": "application-prod.yml",
            "properties": {
                "spring.datasource.url": {"value": f"jdbc:mysql://{_FAKE_INTERNAL_IP}:3306/zcbot"},
                "spring.datasource.username": {"value": "zcbot"},
                "spring.datasource.password": {"value": fake_secret(20)},
                "spring.redis.password": {"value": fake_secret(24)},
                "jwt.secret": {"value": fake_secret(48)},
            },
        }],
    })


def body_actuator_health() -> Response:
    return jsonify({"status": "UP", "components": {
        "db": {"status": "UP", "details": {"database": "MySQL", "validationQuery": "isValid()"}},
        "redis": {"status": "UP", "details": {"version": "7.2.4"}},
        "diskSpace": {"status": "UP"},
    }})


def body_actuator_mappings() -> Response:
    servlets = [
        {"handler": "ActuatorWebEndpointHandler", "predicate": "{GET [/actuator/env]}",
         "details": {"handlerMethod": {"className": "EnvEndpoint", "name": "environment"}}},
        {"handler": "ResourceHttpRequestHandler", "predicate": "{GET [/api/admin/users]}",
         "details": {"handlerMethod": {"className": "AdminController", "name": "listUsers"}}},
    ]
    return jsonify({"contexts": {"application": {"mappings": {
        "dispatcherServlets": {"dispatcherServlet": servlets}}}}})


def body_actuator_loggers() -> Response:
    return jsonify({"levels": ["OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"],
                    "loggers": {"ROOT": {"configuredLevel": "INFO", "effectiveLevel": "INFO"},
                                "com.zcbot": {"configuredLevel": "DEBUG", "effectiveLevel": "DEBUG"}}})


def body_actuator_threaddump() -> Response:
    threads = [{"threadName": f"http-nio-8080-exec-{i}", "threadId": 100 + i,
                "blockedTime": -1, "waitedTime": -1, "threadState": "RUNNABLE"}
               for i in range(1, 6)]
    return jsonify({"threads": threads})


def body_actuator_heapdump() -> Response:
    """假堆转储：返回带 JAVA PROFILE 魔数的伪二进制，诱导下载分析"""
    head = b'JAVA PROFILE 1.0.2\x00'
    payload = head + bytes(random.getrandbits(8) for _ in range(2048))
    resp = Response(payload, mimetype='application/octet-stream')
    resp.headers['Content-Disposition'] = 'attachment; filename="heapdump.hprof"'
    return resp


def body_actuator_jolokia() -> Response:
    return jsonify({"value": {"agent": "1.7.2", "protocol": "7.2",
                              "config": {"agentId": _rand_alnum(16)},
                              "info": {"product": "tomcat", "vendor": "Apache"}},
                    "status": 200})


def body_phpinfo() -> Response:
    return Response(
        "<!DOCTYPE html><html><head><title>phpinfo()</title></head><body>"
        "<table><tr><td>PHP Version</td><td>8.2.15</td></tr>"
        "<tr><td>Server API</td><td>FPM/FastCGI</td></tr>"
        f"<tr><td>DOCUMENT_ROOT</td><td>/var/www/{_FAKE_DOMAIN}</td></tr>"
        "<tr><td>Loaded Configuration File</td><td>/etc/php/8.2/fpm/php.ini</td></tr>"
        "</table></body></html>",
        mimetype='text/html')


def body_server_status() -> Response:
    return Response(
        f"Apache Server Status for {_FAKE_HOST}\n\n"
        "Server Version: Apache/2.4.58 (Ubuntu)\n"
        "Server Built: 2024-01-01T00:00:00\n"
        "Current Time: " + time.strftime('%a %b %d %H:%M:%S %Y') + "\n"
        "Server MPM: prefork\nServer Uptime: 12 days 4 hours 33 minutes\n"
        "Total accesses: 1284391 - Total Traffic: 14.2 GB\n",
        mimetype='text/plain')


def body_swagger() -> Response:
    """假 OpenAPI 文档：把攻击者引向一堆不存在的「接口」"""
    return jsonify({
        "openapi": "3.0.1",
        "info": {"title": "ZCBOT Internal API", "version": "1.8.3"},
        "servers": [{"url": f"https://{_FAKE_DOMAIN}/api/v1"}],
        "paths": {
            "/admin/users": {"get": {"summary": "list users"}},
            "/admin/token": {"post": {"summary": "issue token"}},
            "/config": {"get": {"summary": "read config"}},
            "/db/export": {"get": {"summary": "export database"}},
            "/keys": {"get": {"summary": "list secrets"}},
        },
        "components": {"securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}},
        "security": [{"bearerAuth": []}],
    })


def body_backup_zip() -> Response:
    """假备份包：返回一段 PK 开头的伪 zip 字节流，诱导下载"""
    head = b'PK\x03\x04\x14\x00\x00\x00\x08\x00'
    payload = head + bytes(random.getrandbits(8) for _ in range(1024))
    resp = Response(payload, mimetype='application/zip')
    resp.headers['Content-Disposition'] = 'attachment; filename="zcbot-backup.zip"'
    return resp


def body_admin_login_page() -> Response:
    """假后台登录页：一个像模像样的管理入口"""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>系统管理后台 - {_FAKE_DOMAIN}</title>
<meta name="generator" content="AdminPanel/2.4">
<style>
 body{{font-family:-apple-system,"Segoe UI",sans-serif;background:#f2f4f5;margin:0;
   display:flex;align-items:center;justify-content:center;height:100vh}}
 .box{{background:#fff;padding:36px 32px;border-radius:12px;width:340px;
   box-shadow:0 8px 30px rgba(0,0,0,.08)}}
 h1{{font-size:18px;margin:0 0 4px}} p{{color:#8a94a6;font-size:12px;margin:0 0 20px}}
 input{{width:100%;box-sizing:border-box;padding:10px 12px;margin-bottom:12px;
   border:1px solid #dbdfe1;border-radius:8px;font-size:14px}}
 button{{width:100%;padding:10px;border:0;border-radius:8px;background:#5D87FF;
   color:#fff;font-size:14px;cursor:pointer}}
 .f{{text-align:center;color:#a5adbb;font-size:11px;margin-top:16px}}
</style></head>
<body><form class="box" method="post" action="/admin/login">
 <h1>系统管理后台</h1><p>{_FAKE_DOMAIN} · AdminPanel v2.4</p>
 <input name="username" placeholder="用户名" autocomplete="off">
 <input name="password" type="password" placeholder="密码" autocomplete="off">
 <button type="submit">登 录</button>
 <div class="f">Copyright &copy; {time.strftime('%Y')} {_FAKE_DOMAIN}</div>
</form></body></html>"""
    return Response(html, mimetype='text/html')


# ---- 案例驱动新增的诱饵内容 ----

def body_spring_config() -> Response:
    """假 Spring Boot 配置（application.yml）—— 框架配置文件泄露"""
    return Response(
        f"""server:
  port: 8080
  servlet:
    context-path: /api
spring:
  application:
    name: zcbot
  datasource:
    url: jdbc:mysql://{_FAKE_INTERNAL_IP}:3306/zcbot?useSSL=false
    username: zcbot
    password: {fake_secret(20)}
    driver-class-name: com.mysql.cj.jdbc.Driver
  redis:
    host: 10.20.30.16
    port: 6379
    password: {fake_secret(24)}
management:
  endpoints:
    web:
      exposure:
        include: "*"
  endpoint:
    health:
      show-details: always
jwt:
  secret: {fake_secret(48)}
  expire: 86400
""", mimetype='text/yaml')


def body_package_json() -> Response:
    """假 package.json —— 泄露依赖版本，便于攻击者找已知漏洞"""
    return jsonify({
        "name": "zcbot-webui", "version": "1.8.3", "private": True,
        "dependencies": {"vue": "^3.4.38", "vue-router": "^4.4.3",
                         "element-plus": "^2.7.8", "axios": "^1.7.2"},
        "devDependencies": {"vite": "^5.4.2", "@vitejs/plugin-vue": "^5.1.2"},
        "scripts": {"dev": "vite", "build": "vite build"},
    })


def body_pom_xml() -> Response:
    return Response(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.zcbot</groupId>
  <artifactId>zcbot-server</artifactId>
  <version>1.8.3</version>
  <properties>
    <spring-boot.version>2.7.18</spring-boot.version>
  </properties>
  <repositories>
    <repository>
      <id>internal</id>
      <url>https://nexus.{_FAKE_DOMAIN}/repository/maven-public/</url>
    </repository>
  </repositories>
</project>
""", mimetype='application/xml')


def body_dockerfile() -> Response:
    return Response(
        f"""FROM openjdk:8-jre-slim
ENV TZ=Asia/Shanghai
WORKDIR /app
COPY target/zcbot-server.jar app.jar
ENV DB_HOST={_FAKE_INTERNAL_IP}
ENV DB_PASSWORD={fake_secret(20)}
EXPOSE 8080
ENTRYPOINT ["java","-jar","app.jar"]
""", mimetype='text/plain')


def body_compose_yaml() -> Response:
    return Response(
        f"""version: "3.8"
services:
  app:
    image: zcbot/server:1.8.3
    environment:
      DB_HOST: {_FAKE_INTERNAL_IP}
      DB_PASSWORD: {fake_secret(20)}
      JWT_SECRET: {fake_secret(48)}
    ports:
      - "8080:8080"
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: {fake_secret(18)}
""", mimetype='text/yaml')


def body_ci_config() -> Response:
    return Response(
        """name: deploy
on:
  push:
    branches: [ main ]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy
        env:
          SSH_HOST: %s
          SSH_USER: deploy
        run: ./deploy.sh
""" % _FAKE_INTERNAL_IP, mimetype='text/yaml')


def body_ide_config() -> Response:
    return Response(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="DeploymentTargetDropDown">
    <target selected="true">
      <serverName>prod-{_FAKE_INTERNAL_IP}</serverName>
      <userName>deploy</userName>
    </target>
  </component>
  <component name="RecentDirectoryProjectsManager">
    <option name="recentPaths" value="/opt/zcbot" />
  </component>
</project>
""", mimetype='application/xml')


def body_log_file() -> Response:
    """假运行日志：泄露内部路径、主机名与偶发报错"""
    lines = [
        f"2026-10-12 03:11:04 INFO  [main] c.z.Application - Starting Application v1.8.3 on {_FAKE_HOST}",
        f"2026-10-12 03:11:15 INFO  [main] c.z.DbConfig - Connecting to jdbc:mysql://{_FAKE_INTERNAL_IP}:3306/zcbot",
        f"2026-10-12 03:12:27 WARN  [http-nio-8080-exec-3] c.z.Auth - token 校验失败 ip={fake_ip()}",
        "2026-10-12 03:13:38 ERROR [http-nio-8080-exec-7] c.z.Job - task failed: connection reset",
    ]
    return Response('\n'.join(lines) + '\n', mimetype='text/plain')


def body_htaccess() -> Response:
    return Response(
        """# Apache .htaccess
RewriteEngine On
RewriteRule ^api/(.*)$ index.php?route=$1 [QSA,L]
<FilesMatch "\\.(env|sql|log|bak)$">
    Require all denied
</FilesMatch>
""", mimetype='text/plain')


def body_htpasswd() -> Response:
    users = '\n'.join(f"{fake_username()}:{fake_bcrypt_hash()}" for _ in range(3))
    return Response(users + '\n', mimetype='text/plain')


def body_ds_store() -> Response:
    return Response(b'\x00\x00\x00\x01Bud1\x00\x00\x10\x00\x00\x00\x08\x00\x00\x00\x00'
                    + bytes(random.getrandbits(8) for _ in range(256)),
                    mimetype='application/octet-stream')


def body_graphql_schema() -> Response:
    return jsonify({"data": {"__schema": {"queryType": {"name": "Query"},
                                          "types": [{"name": "User"}, {"name": "Token"},
                                                    {"name": "Config"}]}}})


def body_generic_decoy() -> Response:
    """未知 API 路径的通用诱饵：伪装成一个正常的业务响应"""
    return jsonify({
        "code": 0,
        "msg": "ok",
        "data": {
            "server": _FAKE_HOST,
            "env": "production",
            "token": _rand_hex(32),
            "operator": "admin",
            "updated_at": time.strftime('%Y-%m-%d %H:%M:%S'),
        },
    })


def body_generic_text() -> Response:
    return Response(
        "<!DOCTYPE html><html><head><title>404 Not Found</title></head>"
        "<body><h1>404 Not Found</h1>"
        "<p>The requested URL was not found on this server.</p>"
        "<hr><address>nginx/1.24.0</address></body></html>",
        mimetype='text/html', status=404)


# ---------------------------------------------------------------------------
# 真实感错误页（噪声诱饵用）
# ---------------------------------------------------------------------------
# 大多数探测路径在真实系统里只会得到 404/403/401/405…，而不是「有求必应」的 200。
# 用不同服务器的错误页风格，让整片噪声看起来像一台真实的 Web 服务器。
_ERR_SIG = {
    'nginx': 'nginx/1.24.0',
    'apache': 'Apache/2.4.58 (Ubuntu)',
    'tomcat': 'Apache Tomcat/9.0.85',
    'iis': 'Microsoft-IIS/10.0',
}
_ERR_TITLE = {
    400: '400 Bad Request', 401: '401 Unauthorized', 403: '403 Forbidden',
    404: '404 Not Found', 405: '405 Method Not Allowed',
    429: '429 Too Many Requests', 500: '500 Internal Server Error',
}


def body_error(status: int, style: str = 'nginx') -> Response:
    """生成一张「像真服务器」的错误页（状态码即传入值）"""
    title = _ERR_TITLE.get(status, f'{status} Error')
    short = title.split(' ', 1)[1] if ' ' in title else title
    if status in (301, 302, 307, 308):
        resp = Response('', status=status, mimetype='text/html')
        resp.headers['Location'] = '/login'
        return resp
    if style == 'tomcat':
        html = (f'<!DOCTYPE html><html><head><title>{title}</title></head><body>'
                f'<h1>{title}</h1><p><b>Message</b> {title}</p>'
                f'<p><b>Description</b> The requested resource is not available.</p>'
                f'<hr><h3>{_ERR_SIG["tomcat"]}</h3></body></html>')
    elif style == 'iis':
        html = (f'<!DOCTYPE html><html><head><title>{title}</title>'
                f'<style>body{{font-family:"Segoe UI",sans-serif;margin:40px}}</style></head><body>'
                f'<h1 style="font-weight:400">{title}</h1>'
                f'<p>Server Error in "/" Application.</p>'
                f'<hr><p>{_ERR_SIG["iis"]}</p></body></html>')
    elif style == 'apache':
        html = (f'<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN"><html><head>'
                f'<title>{status} {short}</title></head><body>'
                f'<h1>{title}</h1><hr><address>{_ERR_SIG["apache"]}</address></body></html>')
    else:
        html = (f'<html><head><title>{status} {short}</title></head>'
                f'<body><center><h1>{title}</h1></center>'
                f'<hr><center>{_ERR_SIG["nginx"]}</center></body></html>')
    return Response(html, status=status, mimetype='text/html')


# ---------------------------------------------------------------------------
# /admin 陷阱协议
# ---------------------------------------------------------------------------
# 诱饵不是「有求必应」的静态页，而是一条「看起来像普通后台登录」的链：
#   1. GET /admin         → 返回一个普通样子的登录页，并下发一个不透明会话令牌
#                           （藏在隐藏字段 / Cookie 里）。**绝不标注长度 / 算法 / 位数**
#                           —— 一旦自报「2048 位」，反而暴露这是伪造的（自报家门）。
#   2. 任何后续 POST/直连 → 判定为自动化脚本，立即封 IP：
#      - 真实用户不会 POST 一个自己从未登录过的后台；
#      - 脚本无论带不带令牌、怎么构造，都是脚本。
#   内部仍静默记录「提交长度是否恰为下发令牌的一半」用于取证画像，但页面上
#   不透露任何长度信息（连"校验码"这类字样都不出现）。
_TRAP_LOCK = threading.Lock()
_TRAP_STATE = {}          # ip -> {'token': str, 'issued_at': float}
_TRAP_TTL = 600           # 登录页有效期（秒）
_TRAP_MAX = 4096
_TRAP_TOKEN_HEX = 512     # 下发令牌长度（hex）；不对外标注位数


def _trap_issue(ip: str) -> str:
    """为来源 IP 下发一个 2048 位假令牌（覆盖旧值）"""
    token = _rand_hex(_TRAP_TOKEN_HEX)
    now = time.time()
    with _TRAP_LOCK:
        for k in [k for k, v in _TRAP_STATE.items() if now - v['issued_at'] > _TRAP_TTL]:
            _TRAP_STATE.pop(k, None)
        if len(_TRAP_STATE) > _TRAP_MAX:
            for k in list(_TRAP_STATE)[:len(_TRAP_STATE) // 2]:
                _TRAP_STATE.pop(k, None)
        _TRAP_STATE[ip] = {'token': token, 'issued_at': now}
    return token


def body_admin_challenge(ip: str) -> Response:
    """假后台登录页：普通后台的样子，下发不透明会话令牌（隐藏字段 + Cookie），
    页面不标注任何长度 / 算法 / 位数，避免「自报家门」暴露伪造。"""
    token = _trap_issue(ip)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>系统管理后台 - {_FAKE_DOMAIN}</title>
<meta name="generator" content="AdminPanel/2.4">
<style>
 body{{font-family:-apple-system,"Segoe UI",sans-serif;background:#f2f4f5;margin:0;
   display:flex;align-items:center;justify-content:center;height:100vh}}
 .box{{background:#fff;padding:36px 32px;border-radius:12px;width:340px;
   box-shadow:0 8px 30px rgba(0,0,0,.08)}}
 h1{{font-size:18px;margin:0 0 4px}} p{{color:#8a94a6;font-size:12px;margin:0 0 20px}}
 input{{width:100%;box-sizing:border-box;padding:10px 12px;margin-bottom:12px;
   border:1px solid #dbdfe1;border-radius:8px;font-size:14px}}
 button{{width:100%;padding:10px;border:0;border-radius:8px;background:#5D87FF;
   color:#fff;font-size:14px;cursor:pointer}}
 .f{{text-align:center;color:#a5adbb;font-size:11px;margin-top:16px}}
</style></head>
<body><form class="box" method="post" action="/admin/login">
 <h1>系统管理后台</h1><p>{_FAKE_DOMAIN} · AdminPanel v2.4</p>
 <input name="username" placeholder="用户名" autocomplete="off">
 <input name="password" type="password" placeholder="密码" autocomplete="off">
 <button type="submit">登 录</button>
 <div class="f">Copyright &copy; {time.strftime('%Y')} {_FAKE_DOMAIN}</div>
 <input type="hidden" name="csrf" value="{token}">
</form></body></html>"""
    resp = Response(html, mimetype='text/html')
    resp.set_cookie('admin_sid', token, path='/', httponly=True, samesite='Lax')
    return resp


# ===========================================================================
# 诱饵路由表
#   basic = 核心诱饵（最高频、最具代表性）
#   full  = 在 basic 之上追加的扩展诱饵
#   条目格式：path -> (methods, builder, reason)
# ===========================================================================
_DECOY_BASIC = {
    # ---- 配置 / 密钥泄露 ----
    '/.env': (('GET',), body_env_file, '配置文件/密钥探测'),
    '/.env.bak': (('GET',), body_env_file, '配置文件/密钥探测'),
    '/api/.env': (('GET',), body_env_file, '配置文件/密钥探测'),
    '/config.json': (('GET',), body_env_json, '配置文件/密钥探测'),
    '/credentials.json': (('GET',), body_keys_json, '配置文件/密钥探测'),
    '/api/keys': (('GET',), body_keys_json, '配置文件/密钥探测'),
    '/api/secrets': (('GET',), body_keys_json, '配置文件/密钥探测'),
    '/api/token': (('GET', 'POST'), body_login_success_json, '配置文件/密钥探测'),
    '/.aws/credentials': (('GET',), body_aws_credentials, '云凭据探测'),
    '/.ssh/id_rsa': (('GET',), body_ssh_key, '云凭据探测'),
    '/.kube/config': (('GET',), body_kube_config, '云凭据探测'),
    # ---- Git / SVN 泄露 ----
    '/.git/config': (('GET',), body_git_config, '版本控制泄露探测'),
    '/.git/HEAD': (('GET',), body_git_head, '版本控制泄露探测'),
    '/.git/refs/heads/main': (('GET',), body_git_ref, '版本控制泄露探测'),
    # ---- 备份 / 数据库导出 ----
    '/backup.sql': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/db.sql': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/dump.sql': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/backup.zip': (('GET',), body_backup_zip, '备份/数据库导出探测'),
    '/www.zip': (('GET',), body_backup_zip, '备份/数据库导出探测'),
    # ---- 假后台 / 面板 ----
    # 注意：/admin 与 /admin/login 不在此表 —— 它们由「陷阱协议」单独注册（见 _admin_trap）
    '/wp-admin': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/wp-login.php': (('GET', 'POST'), body_admin_login_page, '后台入口扫描'),
    '/phpmyadmin': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/manager/html': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/console': (('GET',), body_admin_login_page, '后台入口扫描'),
    # ---- 框架管理端点 ----
    '/actuator': (('GET',), body_actuator_health, '框架管理端点探测'),
    '/actuator/env': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/actuator/health': (('GET',), body_actuator_health, '框架管理端点探测'),
    '/actuator/heapdump': (('GET',), body_actuator_heapdump, '框架管理端点探测'),
    '/server-status': (('GET',), body_server_status, '框架管理端点探测'),
    '/phpinfo.php': (('GET',), body_phpinfo, '框架管理端点探测'),
    '/debug': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/api/debug': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/api/env': (('GET',), body_env_json, '框架管理端点探测'),
    # ---- 接口文档 ----
    '/api/swagger.json': (('GET',), body_swagger, '未公开 API 命名空间探测'),
    '/swagger-ui.html': (('GET',), body_swagger, '未公开 API 命名空间探测'),
    '/openapi.json': (('GET',), body_swagger, '未公开 API 命名空间探测'),
    '/v2/api-docs': (('GET',), body_swagger, '未公开 API 命名空间探测'),
    # ---- 假 API 命名空间 ----
    '/api/v1/users': (('GET',), body_users_json, '未公开 API 命名空间探测'),
    '/api/v1/auth/login': (('POST',), body_login_success_json, '未公开 API 命名空间探测'),
    '/api/v1/config': (('GET',), body_env_json, '未公开 API 命名空间探测'),
    '/api/internal/config': (('GET',), body_env_json, '未公开 API 命名空间探测'),
    '/api/internal/secrets': (('GET',), body_keys_json, '未公开 API 命名空间探测'),
    '/api/internal/users': (('GET',), body_users_json, '未公开 API 命名空间探测'),
}

_DECOY_FULL = {
    # ---- Actuator 全套（案例：/actuator/env → heapdump 的完整利用链）----
    '/actuator/mappings': (('GET',), body_actuator_mappings, '框架管理端点探测'),
    '/actuator/configprops': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/actuator/loggers': (('GET',), body_actuator_loggers, '框架管理端点探测'),
    '/actuator/threaddump': (('GET',), body_actuator_threaddump, '框架管理端点探测'),
    '/actuator/jolokia': (('GET',), body_actuator_jolokia, '框架管理端点探测'),
    '/actuator/trace': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/actuator/httptrace': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/actuator/caches': (('GET',), body_actuator_health, '框架管理端点探测'),
    '/actuator/scheduledtasks': (('GET',), body_actuator_threaddump, '框架管理端点探测'),
    '/jolokia': (('GET',), body_actuator_jolokia, '框架管理端点探测'),
    '/metrics': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/api/metrics': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/trace': (('GET',), body_actuator_env, '框架管理端点探测'),
    '/h2-console': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/druid/index.html': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/solr/admin/cores': (('GET',), body_actuator_env, '框架管理端点探测'),
    # ---- Docker Remote API（案例：未上锁的门 → 服务器沦陷）----
    '/version': (('GET',), lambda: jsonify({
        "Version": "20.10.24", "ApiVersion": "1.41", "MinAPIVersion": "1.12",
        "GitCommit": _rand_hex(7), "GoVersion": "go1.20.12",
        "Os": "linux", "Arch": "amd64", "KernelVersion": "5.15.0-91-generic"}),
        '容器/编排接口探测'),
    '/info': (('GET',), lambda: jsonify({
        "ID": _rand_hex(64), "Containers": 14, "ContainersRunning": 12,
        "Images": 47, "Driver": "overlay2", "Name": _FAKE_HOST,
        "ServerVersion": "20.10.24", "OperatingSystem": "Ubuntu 22.04.4 LTS"}),
        '容器/编排接口探测'),
    '/containers/json': (('GET',), lambda: jsonify([
        {"Id": _rand_hex(64), "Names": ["/zcbot-app"], "Image": "zcbot/server:1.8.3",
         "State": "running", "Status": "Up 12 days",
         "Ports": [{"PrivatePort": 8080, "PublicPort": 8080, "Type": "tcp"}]},
        {"Id": _rand_hex(64), "Names": ["/mysql"], "Image": "mysql:8.0",
         "State": "running", "Status": "Up 12 days",
         "Ports": [{"PrivatePort": 3306, "Type": "tcp"}]}]), '容器/编排接口探测'),
    '/images/json': (('GET',), lambda: jsonify([
        {"Id": "sha256:" + _rand_hex(64), "RepoTags": ["zcbot/server:1.8.3"],
         "Size": 412336691, "Created": int(time.time()) - 86400 * 30}]),
        '容器/编排接口探测'),
    '/v1.41/containers/json': (('GET',), lambda: jsonify([]), '容器/编排接口探测'),
    '/docker-compose.yml': (('GET',), body_compose_yaml, '容器/编排接口探测'),
    '/Dockerfile': (('GET',), body_dockerfile, '容器/编排接口探测'),
    # ---- 构建 / 依赖清单（用于找已知漏洞）----
    '/package.json': (('GET',), body_package_json, '依赖清单探测'),
    '/pom.xml': (('GET',), body_pom_xml, '依赖清单探测'),
    '/composer.json': (('GET',), body_package_json, '依赖清单探测'),
    # ---- 框架配置泄露 ----
    '/application.yml': (('GET',), body_spring_config, '配置文件/密钥探测'),
    '/application.properties': (('GET',), body_spring_config, '配置文件/密钥探测'),
    '/application-prod.yml': (('GET',), body_spring_config, '配置文件/密钥探测'),
    '/config.yaml': (('GET',), body_spring_config, '配置文件/密钥探测'),
    '/config.yml': (('GET',), body_spring_config, '配置文件/密钥探测'),
    '/.env.local': (('GET',), body_env_file, '配置文件/密钥探测'),
    '/.env.production': (('GET',), body_env_file, '配置文件/密钥探测'),
    '/.env.example': (('GET',), body_env_file, '配置文件/密钥探测'),
    '/secrets.json': (('GET',), body_keys_json, '配置文件/密钥探测'),
    '/api/credentials': (('GET',), body_keys_json, '配置文件/密钥探测'),
    '/.npmrc': (('GET',), body_npmrc, '配置文件/密钥探测'),
    '/.docker/config.json': (('GET',), body_docker_config, '云凭据探测'),
    '/id_rsa': (('GET',), body_ssh_key, '云凭据探测'),
    '/server.key': (('GET',), body_private_key, '证书/私钥探测'),
    '/private.key': (('GET',), body_private_key, '证书/私钥探测'),
    '/cert.pem': (('GET',), body_cert_pem, '证书/私钥探测'),
    # ---- 日志泄露 ----
    '/logs/error.log': (('GET',), body_log_file, '日志文件探测'),
    '/logs/app.log': (('GET',), body_log_file, '日志文件探测'),
    '/debug.log': (('GET',), body_log_file, '日志文件探测'),
    '/storage/logs/laravel.log': (('GET',), body_log_file, '日志文件探测'),
    # ---- CI / IDE / 服务器杂项 ----
    '/.github/workflows/deploy.yml': (('GET',), body_ci_config, 'CI 配置探测'),
    '/.gitlab-ci.yml': (('GET',), body_ci_config, 'CI 配置探测'),
    '/Jenkinsfile': (('GET',), body_ci_config, 'CI 配置探测'),
    '/.idea/workspace.xml': (('GET',), body_ide_config, 'IDE 配置探测'),
    '/.vscode/settings.json': (('GET',), body_env_json, 'IDE 配置探测'),
    '/.htaccess': (('GET',), body_htaccess, '服务器杂项探测'),
    '/.htpasswd': (('GET',), body_htpasswd, '服务器杂项探测'),
    '/.DS_Store': (('GET',), body_ds_store, '服务器杂项探测'),
    '/telescope': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/graphql': (('GET', 'POST'), body_graphql_schema, '未公开 API 命名空间探测'),
    '/api/graphql': (('GET', 'POST'), body_graphql_schema, '未公开 API 命名空间探测'),
    # ---- 更多备份形态 ----
    '/backup.tar.gz': (('GET',), body_backup_zip, '备份/数据库导出探测'),
    '/site.tar.gz': (('GET',), body_backup_zip, '备份/数据库导出探测'),
    '/db_backup.sql': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/data.sql': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/database.sql': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/api/db/dump': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    '/api/export': (('GET',), body_sql_dump, '备份/数据库导出探测'),
    # ---- 其余后台入口 ----
    '/administrator': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/pma': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/webadmin': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/cpanel': (('GET',), body_admin_login_page, '后台入口扫描'),
    '/login': (('GET', 'POST'), body_admin_login_page, '后台入口扫描'),
    '/.svn/entries': (('GET',), body_git_config, '版本控制泄露探测'),
    '/.git/refs/heads/master': (('GET',), body_git_ref, '版本控制泄露探测'),
    '/.git/logs/HEAD': (('GET',), body_log_file, '版本控制泄露探测'),
    # ---- 扩展 API 命名空间 ----
    '/api/v1/admin/users': (('GET',), body_users_json, '未公开 API 命名空间探测'),
    '/api/v1/admin/token': (('POST',), body_login_success_json, '未公开 API 命名空间探测'),
    '/api/v1/keys': (('GET',), body_keys_json, '未公开 API 命名空间探测'),
    '/api/v1/db/query': (('POST',), body_sql_dump, '未公开 API 命名空间探测'),
    '/api/internal/env': (('GET',), body_env_json, '未公开 API 命名空间探测'),
    '/api/internal/token': (('GET',), body_login_success_json, '未公开 API 命名空间探测'),
    '/api/internal/debug': (('GET',), body_actuator_env, '未公开 API 命名空间探测'),
    '/api/v2/users': (('GET',), body_users_json, '未公开 API 命名空间探测'),
}


# ===========================================================================
# 噪声诱饵表（decoy_set = full 时并入）
#   —— 让整片扫描面「像一台真实服务器」：绝大多数路径返回 404/403/401/405，
#      只有极少数（见 _DECOY_BASIC/_DECOY_FULL）才「碰巧泄露」出假 200。
#   条目格式：(path, methods, status, style)
# ===========================================================================
_NOISE = [
    # ---- CMS / 后台入口 ----
    ('/wp-admin/admin-ajax.php', ('GET', 'POST'), 403, 'nginx'),
    ('/wp-content/', ('GET',), 403, 'nginx'),
    ('/wp-json/wp/v2/users', ('GET',), 404, 'nginx'),
    ('/xmlrpc.php', ('POST',), 405, 'nginx'),
    ('/administrator/index.php', ('GET',), 404, 'apache'),
    ('/joomla/administrator/', ('GET',), 404, 'apache'),
    ('/admin.php', ('GET',), 404, 'nginx'),
    ('/admin1/', ('GET',), 404, 'nginx'),
    ('/admin_area/', ('GET',), 404, 'nginx'),
    ('/manage/', ('GET',), 404, 'nginx'),
    ('/management/', ('GET',), 404, 'nginx'),
    ('/portal/', ('GET',), 302, 'nginx'),
    ('/dashboard/', ('GET',), 302, 'nginx'),
    ('/login.php', ('GET',), 404, 'nginx'),
    ('/signin', ('GET',), 404, 'nginx'),
    ('/user/login', ('GET',), 404, 'nginx'),
    # ---- 配置 / 备份 ----
    ('/.env.dev', ('GET',), 404, 'nginx'),
    ('/.env.test', ('GET',), 404, 'nginx'),
    ('/.env.staging', ('GET',), 404, 'nginx'),
    ('/.env.backup', ('GET',), 404, 'nginx'),
    ('/env', ('GET',), 404, 'nginx'),
    ('/config.php.bak', ('GET',), 404, 'apache'),
    ('/config.yaml.bak', ('GET',), 404, 'nginx'),
    ('/settings.py', ('GET',), 404, 'nginx'),
    ('/web.config', ('GET',), 404, 'iis'),
    ('/nginx.conf', ('GET',), 403, 'nginx'),
    ('/httpd.conf', ('GET',), 403, 'apache'),
    ('/docker-compose.yaml', ('GET',), 404, 'nginx'),
    ('/docker-compose.prod.yml', ('GET',), 404, 'nginx'),
    ('/Makefile', ('GET',), 403, 'nginx'),
    ('/Gemfile', ('GET',), 404, 'nginx'),
    ('/go.mod', ('GET',), 404, 'nginx'),
    ('/composer.lock', ('GET',), 404, 'nginx'),
    ('/yarn.lock', ('GET',), 404, 'nginx'),
    ('/package-lock.json', ('GET',), 404, 'nginx'),
    # ---- 版本控制 ----
    ('/.git/index', ('GET',), 403, 'nginx'),
    ('/.git/packed-refs', ('GET',), 403, 'nginx'),
    ('/.git/COMMIT_EDITMSG', ('GET',), 403, 'nginx'),
    ('/.gitignore', ('GET',), 404, 'nginx'),
    ('/.svn/wc.db', ('GET',), 404, 'apache'),
    ('/.hg/hgrc', ('GET',), 404, 'nginx'),
    # ---- 假 API 命名空间（v1/v2/v3 / internal）----
    ('/api/v2/profile', ('GET',), 404, 'nginx'),
    ('/api/v2/auth', ('POST',), 404, 'nginx'),
    ('/api/v2/config', ('GET',), 404, 'nginx'),
    ('/api/v3/users', ('GET',), 404, 'nginx'),
    ('/api/v1/health', ('GET',), 404, 'nginx'),
    ('/api/v1/status', ('GET',), 404, 'nginx'),
    ('/api/v1/metrics', ('GET',), 404, 'nginx'),
    ('/api/v1/orders', ('GET',), 404, 'nginx'),
    ('/api/v1/callback', ('POST',), 500, 'nginx'),
    ('/api/v1/webhook', ('POST',), 400, 'nginx'),
    ('/api/v1/upload', ('POST',), 429, 'nginx'),
    ('/api/v1/files', ('GET',), 404, 'nginx'),
    ('/api/v1/backup', ('GET',), 403, 'nginx'),
    ('/api/v1/dump', ('GET',), 403, 'nginx'),
    ('/api/v1/shell', ('POST',), 403, 'nginx'),
    ('/api/v1/exec', ('POST',), 403, 'nginx'),
    ('/api/v1/eval', ('POST',), 403, 'nginx'),
    ('/api/v1/rpc', ('POST',), 404, 'nginx'),
    ('/api/v1/debug', ('GET',), 403, 'nginx'),
    ('/api/v1/admin', ('GET',), 404, 'nginx'),
    ('/api/internal/backup', ('GET',), 403, 'nginx'),
    ('/api/internal/exec', ('POST',), 403, 'nginx'),
    ('/api/internal/health', ('GET',), 404, 'nginx'),
    ('/api/internal/status', ('GET',), 401, 'nginx'),
    # ---- Actuator 扩展 ----
    ('/actuator/beans', ('GET',), 404, 'nginx'),
    ('/actuator/conditions', ('GET',), 404, 'nginx'),
    ('/actuator/startup', ('GET',), 404, 'nginx'),
    ('/actuator/prometheus', ('GET',), 404, 'nginx'),
    # ---- 健康检查 / 调试 / 运维 ----
    ('/nginx_status', ('GET',), 403, 'nginx'),
    ('/status', ('GET',), 404, 'nginx'),
    ('/health', ('GET',), 404, 'nginx'),
    ('/healthz', ('GET',), 404, 'nginx'),
    ('/readyz', ('GET',), 404, 'nginx'),
    ('/livez', ('GET',), 404, 'nginx'),
    ('/ping', ('GET',), 404, 'nginx'),
    ('/debug/pprof', ('GET',), 403, 'nginx'),
    ('/debug/vars', ('GET',), 403, 'nginx'),
    ('/_status', ('GET',), 404, 'nginx'),
    ('/_cat/indices', ('GET',), 403, 'nginx'),
    ('/_cluster/health', ('GET',), 403, 'nginx'),
    ('/solr/', ('GET',), 404, 'nginx'),
    ('/jenkins/script', ('POST',), 403, 'nginx'),
    ('/jenkins/manage', ('GET',), 403, 'nginx'),
    ('/script', ('POST',), 403, 'nginx'),
    ('/shell', ('POST',), 403, 'nginx'),
    ('/cgi-bin/', ('GET',), 404, 'apache'),
    ('/cgi-bin/luci', ('GET',), 404, 'apache'),
    ('/jmx-console/', ('GET',), 403, 'tomcat'),
    ('/invoker/JMXInvokerServlet', ('GET',), 403, 'tomcat'),
    ('/web-console/', ('GET',), 403, 'tomcat'),
    ('/weblogic/', ('GET',), 404, 'nginx'),
    # ---- 云元数据（SSRF 探测）----
    ('/latest/meta-data/', ('GET',), 403, 'nginx'),
    ('/latest/user-data/', ('GET',), 403, 'nginx'),
    ('/computeMetadata/v1/', ('GET',), 403, 'nginx'),
    ('/metadata/instance', ('GET',), 403, 'nginx'),
    # ---- IDE / 系统文件 ----
    ('/.idea/', ('GET',), 403, 'nginx'),
    ('/.vscode/', ('GET',), 403, 'nginx'),
    ('/.project', ('GET',), 404, 'nginx'),
    ('/.classpath', ('GET',), 404, 'nginx'),
    ('/Thumbs.db', ('GET',), 404, 'iis'),
    ('/desktop.ini', ('GET',), 404, 'iis'),
    # ---- 日志 ----
    ('/logs/', ('GET',), 403, 'nginx'),
    ('/log/', ('GET',), 403, 'nginx'),
    ('/app.log', ('GET',), 404, 'nginx'),
    ('/error.log', ('GET',), 404, 'nginx'),
    ('/access.log', ('GET',), 404, 'nginx'),
    ('/catalina.out', ('GET',), 404, 'tomcat'),
    # ---- 杂项 ----
    ('/phpinfo', ('GET',), 404, 'nginx'),
    ('/test.php', ('GET',), 404, 'nginx'),
    ('/info.php', ('GET',), 404, 'nginx'),
    ('/readme.html', ('GET',), 404, 'nginx'),
    ('/README.md', ('GET',), 404, 'nginx'),
    ('/CHANGELOG.md', ('GET',), 404, 'nginx'),
    ('/license.txt', ('GET',), 404, 'nginx'),
    ('/crossdomain.xml', ('GET',), 404, 'nginx'),
    ('/clientaccesspolicy.xml', ('GET',), 404, 'nginx'),
    ('/axis2/', ('GET',), 404, 'tomcat'),
    ('/services/', ('GET',), 404, 'tomcat'),
    ('/ws/', ('GET',), 404, 'nginx'),
    ('/rpc/', ('POST',), 404, 'nginx'),
    ('/.well-known/security.txt', ('GET',), 404, 'nginx'),
]


def _noise_builder(status: int, style: str):
    """为一条噪声诱饵生成「返回指定状态码」的构造器"""
    def _b():
        return body_error(status, style)
    return _b


_DECOY_NOISE = {
    # 噪声节点统一至少接受 GET/POST：否则换个方法就会落到 /api/<path> 兜底，
    # 反而返回「有求必应」的假 200，露馅。
    path: (tuple(sorted({'GET', 'POST'} | set(methods))),
           _noise_builder(status, style), '噪声诱饵探测')
    for (path, methods, status, style) in _NOISE
}


# register() 完成后由内部赋值，供 record_hit() 复用命中处理链
_hit_fn = None
_canary_fn = None


def record_hit(path, reason, canary=None, evasion='') -> bool:
    """
    供外部模块记录一次命中（端口隔离时由 webapp 代为调用）。
    蜜罐未启用时返回 False，调用方应回退为普通 404 / 静默丢弃。
    """
    fn = _hit_fn
    if fn is None:
        return False
    try:
        fn(path, reason, canary=canary, evasion=evasion)
        return True
    except Exception as e:
        logger.debug(f"[蜜罐] record_hit 失败: {e}")
        return False


def new_canary():
    """供外部模块生成追踪串（蜜罐未启用 canary 时返回 None）"""
    if _canary_fn is None:
        return None
    try:
        return _canary_fn()
    except Exception:
        return None


def _decoy_table(decoy_set: str) -> dict:
    """按诱饵集档位返回路由表"""
    if decoy_set == 'off':
        return {}
    if decoy_set == 'basic':
        return dict(_DECOY_BASIC)
    table = dict(_DECOY_BASIC)
    table.update(_DECOY_FULL)
    # full 档并入噪声诱饵：让整片扫描面像一台真实服务器（多数 404/403）
    table.update(_DECOY_NOISE)
    return table


def register(ctx):
    """注册蜜罐端点（由 create_web_app 调用）"""
    app = ctx.app
    framework = ctx.framework
    dual_auth = ctx.dual_auth
    get_client_ip = ctx.get_client_ip
    audit_log = ctx.audit_log

    from framework.api.security_levels import resolve
    sec = resolve(framework.config.get('security'))
    hp = sec['honeypot']
    level = sec['level']

    if not hp.get('enabled'):
        logger.info(f"[蜜罐] 已禁用（security.level: {level} → 不注册诱饵端点）")
        return

    tarpit_ms = int(hp.get('tarpit_ms', 600) or 0)
    do_blacklist = bool(hp.get('blacklist', True))
    blacklist_ttl = int(hp.get('blacklist_ttl', 3600) or 0)
    log_body = bool(hp.get('log_body', True))
    max_body_log = int(hp.get('max_body_log', 2048) or 2048)
    unknown_api_decoy = bool(hp.get('unknown_api_decoy', True))
    canary_enabled = bool(hp.get('canary', True))
    evasion_detect = bool(hp.get('evasion_detect', True))
    fake_404 = bool(hp.get('fake_404', True))
    decoy_set = str(hp.get('decoy_set', 'full') or 'full')

    def _tarpit():
        tarpit(tarpit_ms)

    def _hit(path, reason, canary=None, evasion='', ban=True):
        """命中诱饵：记录取证信息 + 统计 +（ban=True 时）拉黑来源 IP"""
        ip = get_client_ip()
        detail = {
            'path': path,
            'method': request.method,
            'query': request.query_string.decode('utf-8', 'replace')[:512],
            'ua': (request.headers.get('User-Agent') or '')[:256],
            'referer': (request.headers.get('Referer') or '')[:256],
            'x_forwarded_for': (request.headers.get('X-Forwarded-For') or '')[:256],
            'headers': {k: v[:256] for k, v in list(request.headers.items())[:20]},
        }
        if evasion:
            detail['evasion'] = evasion
        if canary:
            detail['canary'] = canary
        if log_body:
            try:
                raw = request.get_data(as_text=True) or ''
                if raw:
                    detail['body'] = raw[:max_body_log]
            except Exception:
                pass

        # 统计画像
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        with _stats_lock:
            _stats['total_hits'] += 1
            if evasion:
                _stats['evasion_hits'] += 1
            if canary:
                _stats['canary_issued'] += 1
            if _stats['first_hit_at'] is None:
                _stats['first_hit_at'] = now_str
            _stats['last_hit_at'] = now_str
            _bump(_stats['by_ip'], ip)
            _bump(_stats['by_path'], path)
            _bump(_stats['by_reason'], reason)

        logger.warning(
            f"[蜜罐] 命中诱饵 {request.method} {path} ← {ip} "
            f"UA={detail['ua']!r} 原因={reason}"
            + (f" 绕过手法={evasion}" if evasion else "")
            + (f" canary={canary}" if canary else ""))

        try:
            audit_log(None, f'honeypot:{ip}', 'honeypot_hit', 'security', path,
                      detail, 'failure', reason)
        except Exception:
            pass

        if do_blacklist and ban:
            # 内网 / 白名单 IP 豁免：既不拉黑也不计数（避免"已拉黑"统计虚高）
            try:
                exempt = dual_auth.is_internal(ip) or dual_auth.is_whitelisted(ip)
            except Exception:
                exempt = False
            if exempt:
                logger.debug(f"[蜜罐] 内网/白名单 IP 豁免拉黑: {ip}")
            else:
                try:
                    # 绕过手法属于「已上钩」，加重处置：拉黑时长翻倍
                    ttl = blacklist_ttl
                    if evasion and ttl > 0:
                        ttl *= 2
                    expires_at = (time.time() + ttl) if ttl > 0 else None
                    tag = f'蜜罐命中({evasion}): {path}' if evasion else f'蜜罐命中: {path}'
                    dual_auth.blacklist(ip, reason=tag, source='honeypot', expires_at=expires_at)
                    with _stats_lock:
                        _stats['blacklisted'] += 1
                except Exception as e:
                    logger.warning(f"[蜜罐] 拉黑失败: {e}")

    def _make_handler(path, builder, reason):
        def _handler():
            evasion = detect_evasion() if evasion_detect else ''
            canary = _new_canary() if canary_enabled else None
            if canary:
                _remember_canary(canary, get_client_ip(), path,
                                 (request.headers.get('User-Agent') or '')[:256])
            _hit(path, reason, canary=canary, evasion=evasion)
            _tarpit()
            try:
                resp = builder()
            except Exception as e:
                logger.warning(f"[蜜罐] 生成诱饵响应失败 {path}: {e}")
                resp = body_generic_decoy()
            if canary:
                _inject_canary(resp, canary)
            return resp
        _handler.__name__ = '_honeypot_' + (path.strip('/').replace('/', '_').replace('.', '_') or 'root')
        return _handler

    # ---- 注册诱饵路由 ----
    table = _decoy_table(decoy_set)
    # 真实路由（非蜜罐）已占用的路径：诱饵绝不覆盖，避免规则歧义
    real_paths = {r.rule for r in app.url_map.iter_rules()
                  if not r.endpoint.startswith('_honeypot')}
    registered = 0
    skipped = 0
    for path, (methods, builder, reason) in table.items():
        if path in real_paths:
            skipped += 1
            continue
        try:
            app.add_url_rule(
                path,
                endpoint=f'_honeypot{path.replace("/", "_").replace(".", "_")}',
                view_func=_make_handler(path, builder, reason),
                methods=list(methods),
            )
            registered += 1
        except Exception as e:
            logger.warning(f"[蜜罐] 注册诱饵路由失败 {path}: {e}")

    # ---- /admin 陷阱协议：下发令牌 → 后续任何动作即封 ----
    def _admin_trap():
        ip = get_client_ip()
        if request.method == 'GET':
            # 第一步：只记录 + 下发令牌（这是诱饵，不封）
            evasion = detect_evasion() if evasion_detect else ''
            canary = _new_canary() if canary_enabled else None
            if canary:
                _remember_canary(canary, ip, '/admin',
                                 (request.headers.get('User-Agent') or '')[:256])
            _hit('/admin', '后台入口扫描', canary=canary, evasion=evasion, ban=False)
            _tarpit()
            resp = body_admin_challenge(ip)
            if canary:
                _inject_canary(resp, canary)
            return resp

        # 后续任何 POST / 直连 → 判定为自动化脚本，直接封 IP
        submitted = ''
        try:
            j = request.get_json(silent=True) or {}
            submitted = str(j.get('token') or j.get('csrf') or '')
        except Exception:
            pass
        if not submitted:
            try:
                submitted = str(request.form.get('token') or request.form.get('csrf') or '')
            except Exception:
                pass
        # 静默标记：提交长度是否恰为下发令牌的一半（仅取证画像；页面不透露任何长度）
        issued_len = 0
        with _TRAP_LOCK:
            _ent = _TRAP_STATE.get(ip)
            if _ent:
                issued_len = len(_ent['token'])
        if not submitted:
            reason = '陷阱命中: 直连/无令牌探测'
        elif issued_len and len(submitted) == issued_len // 2:
            reason = '陷阱命中: 疑似按半长构造令牌(自动化脚本)'
        else:
            reason = '陷阱命中: 带令牌直连探测'
        _hit('/admin', reason)
        _tarpit()
        return body_error(403, 'nginx')

    for _tp in ('/admin', '/admin/login'):
        if _tp in real_paths:
            continue
        try:
            app.add_url_rule(
                _tp,
                endpoint=f'_honeypot_trap{_tp.replace("/", "_")}',
                view_func=_admin_trap,
                methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'],
            )
            registered += 1
        except Exception as e:
            logger.warning(f"[蜜罐] 注册陷阱路由失败 {_tp}: {e}")

    # ---- 兜底：未知 /api/* 路径 ----
    if unknown_api_decoy:
        def _api_fallback(subpath):
            evasion = detect_evasion() if evasion_detect else ''
            canary = _new_canary() if canary_enabled else None
            if canary:
                _remember_canary(canary, get_client_ip(), '/api/' + (subpath or ''),
                                 (request.headers.get('User-Agent') or '')[:256])
            _hit('/api/' + (subpath or ''), '未知 API 路径探测', canary=canary, evasion=evasion)
            _tarpit()
            resp = body_generic_decoy()
            if canary:
                _inject_canary(resp, canary)
            return resp

        app.add_url_rule('/api/<path:subpath>', endpoint='_honeypot_api_fallback',
                         view_func=_api_fallback, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
        registered += 1

    # ---- 兜底：未知非 API 路径 ----
    # 浏览器自动请求的良性资源（favicon / 静态资源 / 站点地图等）只返回普通 404，
    # 不计入攻击、不拉黑，避免误伤正常访问。
    _BENIGN_PREFIX = ('js/', 'css/', 'img/', 'assets/', 'fonts/', '.well-known/')
    _BENIGN_EXT = ('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
                   '.woff', '.woff2', '.ttf', '.map', '.txt', '.xml', '.webmanifest')
    _BENIGN_NAME = {'favicon.ico', 'robots.txt', 'manifest.json',
                    'manifest.webmanifest', 'apple-touch-icon.png', 'sitemap.xml'}

    def _page_fallback(subpath):
        low = (subpath or '').lower()
        base = low.rsplit('/', 1)[-1]
        if low.startswith(_BENIGN_PREFIX) or base in _BENIGN_NAME or base.endswith(_BENIGN_EXT):
            return body_generic_text()
        evasion = detect_evasion() if evasion_detect else ''
        canary = _new_canary() if canary_enabled else None
        if canary:
            _remember_canary(canary, get_client_ip(), '/' + (subpath or ''),
                             (request.headers.get('User-Agent') or '')[:256])
        _hit('/' + (subpath or ''), '未知路径探测', canary=canary, evasion=evasion)
        _tarpit()
        if not fake_404:
            return jsonify({'code': 404, 'msg': 'Not Found'}), 404
        resp = body_generic_text()
        if canary:
            _inject_canary(resp, canary)
        return resp

    app.add_url_rule('/<path:subpath>', endpoint='_honeypot_page_fallback',
                     view_func=_page_fallback, methods=['GET', 'POST'])

    # 暴露给其它模块复用（端口隔离场景下由 webapp 代为记录命中）
    global _hit_fn, _canary_fn
    _hit_fn = _hit
    _canary_fn = _new_canary if canary_enabled else None

    logger.info(
        f"[蜜罐] 安全等级={level}({sec['level_label']})，已注册 {registered} 个诱饵端点"
        f"（诱饵集={decoy_set}, tarpit={tarpit_ms}ms, 拉黑={do_blacklist}, "
        f"ttl={blacklist_ttl}s, canary={canary_enabled}, 绕过检测={evasion_detect}）")
