"""配置凭据加密（标准库实现，零新增依赖）。

为什么不用纯哈希：适配器 token / app_secret / api_key 必须拿到原文做握手，
哈希不可逆。为什么不用外部加密库：部署方明确要求「加密，但别太过」——
不引 KMS、不引第三方包、启动链路不增加服务。

方案：HMAC-SHA256 计数器模式（CTR）生成密钥流 + HMAC-SHA256 认证标签，
即 encrypt-then-MAC。HMAC 是标准库里的 PRF，用 master secret 派生出
enc_key / mac_key 两把独立子密钥；每次加密随机 12 字节 nonce，
密文格式 ``enc:v1:<base64url(nonce || ciphertext || tag)>``。

安全边界：
- 密钥来自环境变量 ``ZCBOT_TOKEN_KEY``（兼容别名 ``ZCBOT_CONFIG_SECRET``）。
  未设置时不加密也不解密，旧明文配置照常可用，只在加载时提示一次。
- 只保护配置里的凭据键（access_token / app_secret / api_key 等）。运行时
  config 里仍是明文——握手要用；明文不落盘才是目标。
- 认证标签校验失败时按「解密失败就原样返回」处理，绝不把猜测结果喂给适配器，
  同时打 warning；调用方会看到 enc:v1:... 这种明显异常值，便于排查。
- ``${VAR}`` 占位符不加密：它本来就不落明文，交给后续环境变量替换。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets

logger = logging.getLogger('zcbot')

PREFIX = 'enc:v1:'
ENV_KEYS = ('ZCBOT_TOKEN_KEY', 'ZCBOT_CONFIG_SECRET')

# 需要保护的键名。刻意不写泛化的 secret 之类，避免误伤普通业务字段。
SECRET_KEYS = frozenset({
    'access_token', 'app_secret', 'api_key', 'client_secret', 'bot_secret',
    'webhook_secret', 'secret', 'token', 'secret_key', 'metrics_token',
})

_MASKED = frozenset({'***', '******', '****', '(hidden)'})

_LINE_RE = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:(\s*)(.*)$')
_COMMENT_RE = re.compile(r'\s+#.*$')

_warned_missing_key = False
_warned_bad_cipher = False


def master_secret():
    """返回环境变量里的主密钥；未设置返回 None。"""
    for name in ENV_KEYS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def available() -> bool:
    return bool(master_secret())


def warn_if_no_key():
    """未配置密钥时提示一次（旧明文配置仍可用，便于平滑迁移）。"""
    if not available():
        _warn_missing_key_once()


def _warn_missing_key_once():
    global _warned_missing_key
    if _warned_missing_key:
        return
    _warned_missing_key = True
    logger.warning(
        "未设置 ZCBOT_TOKEN_KEY，适配器凭据仍以明文存储在配置文件中。"
        "设置该环境变量后重启，框架会自动把 access_token / app_secret / api_key 一次性加密落盘。")


def _warn_bad_cipher_once():
    global _warned_bad_cipher
    if _warned_bad_cipher:
        return
    _warned_bad_cipher = True
    logger.warning(
        "配置中存在无法解密的 enc:v1: 凭据：请检查 ZCBOT_TOKEN_KEY 是否与加密时一致；"
        "适配器会收到密文原文，握手将失败。")


def _derive_keys(secret: str):
    raw = secret.encode('utf-8', 'replace')
    prk = hmac.new(b'zcbot-secretbox-v1|prk', raw, hashlib.sha256).digest()
    enc_key = hmac.new(prk, b'enc', hashlib.sha256).digest()
    mac_key = hmac.new(prk, b'mac', hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(8, 'big'),
                         hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_value(value):
    """加密单个字符串；不该加密的值原样返回。"""
    secret = master_secret()
    if not secret:
        _warn_missing_key_once()
        return value
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(PREFIX) or '${' in value:
        return value
    if value.strip().lower() in _MASKED:
        return value

    enc_key, mac_key = _derive_keys(secret)
    nonce = secrets.token_bytes(12)
    data = value.encode('utf-8', 'replace')
    cipher = bytes(a ^ b for a, b in
                   zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()[:16]
    blob = base64.urlsafe_b64encode(nonce + cipher + tag).decode('ascii').rstrip('=')
    return PREFIX + blob


def decrypt_value(value):
    """解密单个字符串；失败时原样返回并告警（失败安全，不猜明文）。"""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    secret = master_secret()
    if not secret:
        _warn_bad_cipher_once()
        return value
    try:
        blob = value[len(PREFIX):]
        raw = base64.urlsafe_b64decode(blob + '=' * (-len(blob) % 4))
        if len(raw) < 12 + 16:
            raise ValueError('ciphertext too short')
        nonce, cipher, tag = raw[:12], raw[12:-16], raw[-16:]
        enc_key, mac_key = _derive_keys(secret)
        expect = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expect):
            raise ValueError('authentication failed')
        plain = bytes(a ^ b for a, b in
                      zip(cipher, _keystream(enc_key, nonce, len(cipher))))
        return plain.decode('utf-8')
    except Exception as exc:
        logger.warning("凭据解密失败，已按密文原样返回：%s", exc)
        _warn_bad_cipher_once()
        return value


def _walk(obj, fn):
    changed = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if isinstance(value, str):
                if key in SECRET_KEYS:
                    new = fn(value)
                    if new != value:
                        obj[key] = new
                        changed += 1
            else:
                changed += _walk(value, fn)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            if isinstance(value, (dict, list)):
                changed += _walk(value, fn)
    return changed


def encrypt_config_secrets(config: dict) -> int:
    """递归加密配置字典中的凭据字段，返回改动数。"""
    return _walk(config, encrypt_value)


def decrypt_config_secrets(config: dict) -> int:
    """递归解密配置字典中的凭据字段，返回改动数。"""
    return _walk(config, decrypt_value)


def rewrite_secret_file(path: str) -> int:
    """按行把配置文件里的明文凭据换成密文，保留缩进、注释与未知字段。

    只改「键名在 SECRET_KEYS 且值不是占位符/已加密/${VAR}」的行。
    返回改动行数。文件不存在或读取失败时返回 0，不影响启动。
    """
    if not available() or not path or not os.path.isfile(path):
        return 0
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError as exc:
        logger.warning("读取配置文件以加密凭据失败：%s", exc)
        return 0

    out = []
    changed = 0
    for line in lines:
        eol = '\n' if line.endswith('\n') else ''
        text = line[:-1] if eol else line
        stripped = text.strip()
        if not stripped or stripped.startswith('#'):
            out.append(line)
            continue
        m = _LINE_RE.match(text)
        if not m or m.group(2) not in SECRET_KEYS:
            out.append(line)
            continue
        indent, key, spaces, rest = m.groups()
        cm = _COMMENT_RE.search(rest)
        raw = (rest[:cm.start()] if cm else rest).strip()
        comment = cm.group(0) if cm else ''
        unquoted = raw
        if (len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'")):
            unquoted = raw[1:-1]
        if (not unquoted or unquoted.startswith(PREFIX)
                or '${' in unquoted or unquoted.strip().lower() in _MASKED):
            out.append(line)
            continue
        encrypted = encrypt_value(unquoted)
        if encrypted == unquoted:
            out.append(line)
            continue
        out.append('%s%s:%s%s%s%s' % (
            indent, key, spaces or ' ',
            json.dumps(encrypted, ensure_ascii=False), comment, eol))
        changed += 1

    if changed:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.writelines(out)
        except OSError as exc:
            logger.warning("写回加密后的凭据失败：%s", exc)
            return 0
    return changed


def migrate_file_and_config(path: str, config: dict) -> int:
    """供加载路径调用：内存字典加密 + 文件按行加密。返回文件改动行数。"""
    if not config:
        return rewrite_secret_file(path)
    encrypt_config_secrets(config)
    return rewrite_secret_file(path)
