"""WebUI 应急管理员（break-glass admin）。

背景：SQLite 损坏或被锁住时，``admin_users`` / ``api_tokens`` 两张表都不可用，
正常登录和 token 校验会同时失效，运维被挡在 WebUI 外面无法做恢复。本模块提供
一份完全落在 ``data/`` 下、不依赖数据库的应急凭据与自包含签名 token：

  - 凭据文件 ``data/admin_emergency.yaml``，权限 0600，密码只存 bcrypt 哈希；
  - token 自带用户名 / 角色 / 过期时间，用文件里的 token_secret 做 HMAC，
    校验全过程不碰数据库（DB 已经坏了就不能再查它）；
  - 仅当数据库侧认证失败（查询异常 / 账号不存在 / 密码不符）时才兜底，
    不额外开一个永远有效的后门；
  - 应急身份返回时带 ``emergency=True`` 标记，操作写独立日志（审计表此时
    很可能同样写不进去）。

模块本身可被安全 import：不在 import 期生成任何文件，显式调用 ensure_config()
才会创建。可独立运行重置密码：``python -m framework.api.emergency_admin --reset``。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from framework.paths import state_dir

logger = logging.getLogger('zcbot')

CONFIG_NAME = 'admin_emergency.yaml'
AUDIT_NAME = 'emergency_admin.log'
TOKEN_PREFIX = 'eme1.'
DEFAULT_TTL = 86400

__all__ = [
    'AUDIT_NAME',
    'CONFIG_NAME',
    'DEFAULT_TTL',
    'TOKEN_PREFIX',
    'config_path',
    'ensure_config',
    'is_emergency_user',
    'issue_token',
    'load_config',
    'reset_password',
    'verify_password',
    'verify_token',
    'write_audit',
]


def config_path(base=None):
    """配置路径：默认 ``<state_dir>/admin_emergency.yaml``，可注入目录便于测试。"""
    return os.path.join(base or state_dir(), CONFIG_NAME)


def _yaml():
    import yaml
    return yaml


def _load(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = _yaml().safe_load(f) or {}
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.error('读取应急管理员配置失败: %s', e)
        return None


def load_config(base=None):
    """只读加载，文件不存在或损坏返回 None，不生成。"""
    return _load(config_path(base))


def _write(path, cfg):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        _yaml().safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning('应急管理员配置权限收紧失败: %s', e)


def _fresh_config():
    """生成一份全新的应急凭据。明文只通过返回值交给调用方，不写进文件。"""
    import bcrypt

    username = 'emerg_' + secrets.token_hex(4)
    password = secrets.token_urlsafe(18)
    cfg = {
        'enabled': True,
        'username': username,
        'password_hash': bcrypt.hashpw(
            password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8'),
        'token_secret': secrets.token_hex(32),
        'created_at': int(time.time()),
        'note': '数据库故障时 WebUI 应急登录凭据；密码明文不落盘，请离线保存。',
    }
    return cfg, password


def _announce(cfg, password, path):
    lines = [
        '[WebUI 应急管理员] 已生成应急登录凭据（明文仅显示这一次，请立即离线保存）：',
        '  用户名: %s' % cfg['username'],
        '  密码:   %s' % password,
        '  配置:   %s（权限 0600，文件内只有 bcrypt 哈希）' % path,
        '  忘记密码可执行: python -m framework.api.emergency_admin --reset',
    ]
    banner = '\n'.join(lines)
    print(banner)
    logger.warning(banner)


def ensure_config(base=None, announce=True):
    """确保配置存在。首次生成时打印一次明文密码；返回 (配置, 明文或 None)。"""
    path = config_path(base)
    cfg = _load(path)
    if cfg and cfg.get('password_hash') and cfg.get('token_secret'):
        return cfg, None
    cfg, password = _fresh_config()
    _write(path, cfg)
    if announce:
        _announce(cfg, password, path)
    return cfg, password


def reset_password(base=None, announce=True):
    """重置应急密码（忘记密码时用），返回 (配置, 新明文)；旧 token 同时失效。"""
    path = config_path(base)
    cfg = _load(path) or {}
    fresh, password = _fresh_config()
    cfg.update({
        'enabled': True,
        'username': fresh['username'],
        'password_hash': fresh['password_hash'],
        'token_secret': fresh['token_secret'],
        'created_at': fresh['created_at'],
        'note': fresh['note'],
    })
    _write(path, cfg)
    if announce:
        _announce(cfg, password, path)
    return cfg, password


def is_emergency_user(username, base=None):
    """用户名是否命中应急账号（DB 认证失败时用它决定是否尝试应急凭据）。"""
    cfg = load_config(base)
    if not cfg or not cfg.get('enabled', True):
        return False
    expected = str(cfg.get('username') or '')
    return bool(expected) and hmac.compare_digest(expected, str(username or ''))


def verify_password(username, password, base=None):
    """校验应急账号密码；bcrypt 比对，配置里绝不存明文。"""
    import bcrypt

    cfg = load_config(base)
    if not cfg or not cfg.get('enabled', True):
        return False
    if not is_emergency_user(username, base):
        return False
    try:
        stored = str(cfg.get('password_hash') or '').encode('utf-8')
        return bcrypt.checkpw(str(password or '').encode('utf-8'), stored)
    except (ValueError, TypeError) as e:
        logger.warning('应急密码校验异常: %s', e)
        return False


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64d(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def issue_token(username, role='super', ttl=DEFAULT_TTL, base=None):
    """签发自包含 token，格式 ``eme1.<payload>.<hmac>``，全程不查库。"""
    cfg = load_config(base)
    if not cfg or not cfg.get('enabled', True):
        return ''
    secret = str(cfg.get('token_secret') or '').encode('utf-8')
    if not secret or not is_emergency_user(username, base):
        return ''
    payload = {
        'u': str(username),
        'r': role or 'super',
        'exp': int(time.time()) + int(ttl or DEFAULT_TTL),
        'n': secrets.token_hex(8),
    }
    body = _b64e(json.dumps(payload, separators=(',', ':'),
                            ensure_ascii=False).encode('utf-8'))
    signed = TOKEN_PREFIX + body
    sig = hmac.new(secret, signed.encode('ascii'), hashlib.sha256).hexdigest()
    return signed + '.' + sig


def verify_token(token, base=None):
    """校验应急 token，成功返回 super 身份字典（含 emergency 标记），失败 None。"""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return None
    cfg = load_config(base)
    if not cfg or not cfg.get('enabled', True):
        return None
    secret = str(cfg.get('token_secret') or '').encode('utf-8')
    if not secret:
        return None
    try:
        signed, sig = token.rsplit('.', 1)
        expected = hmac.new(secret, signed.encode('ascii'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(signed[len(TOKEN_PREFIX):]).decode('utf-8'))
        if int(payload['exp']) < int(time.time()):
            return None
        username = str(payload.get('u') or '')
        if not is_emergency_user(username, base):
            return None
        return {
            'id': 'emergency',
            'username': username,
            'role': payload.get('r') or 'super',
            'emergency': True,
        }
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as e:
        logger.debug('应急 token 校验失败: %s', e)
        return None


def write_audit(action, detail=None, base=None, ip='', result='success'):
    """应急操作审计：数据库此刻不可用，落到独立日志文件。"""
    path = os.path.join(base or state_dir(), 'logs', AUDIT_NAME)
    record = {
        'at': int(time.time()),
        'action': action,
        'result': result,
        'ip': ip or '',
        'detail': detail or {},
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError as e:
        logger.error('应急审计日志写入失败: %s', e)


def _main():
    import argparse

    parser = argparse.ArgumentParser(description='WebUI 应急管理员凭据管理')
    parser.add_argument('--reset', action='store_true', help='重置应急密码与 token 密钥')
    parser.add_argument('--base', default=None, help='配置文件所在目录（默认 data/）')
    args = parser.parse_args()
    if args.reset:
        reset_password(args.base)
    else:
        cfg, password = ensure_config(args.base)
        if password:
            _announce(cfg, password, config_path(args.base))
        else:
            print('应急管理员已配置，用户名: %s（配置: %s）'
                  % (cfg.get('username'), config_path(args.base)))


if __name__ == '__main__':
    _main()
