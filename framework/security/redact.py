"""审计详情落库前的敏感字段脱敏。

审计表回答「谁在什么时候对什么做了什么」，不是凭据仓库：请求体、配置变更、
插件参数里常带 password / token / api_key 这类字段，原样写进 `detail` 会让
审计表变成凭据泄露面。

这里只做两件事，且都在写入前完成：

1. 按**键名**递归匹配敏感词，命中写固定哨兵，值不进库；
2. 非敏感字符串按长度截断，避免单条详情把表撑大。

键名匹配忽略大小写与 `-` / `_` / 空格差异（`api-key` 与 `apiKey` 都命中）；
不做值内容猜测——正常业务字段里出现 token 字样但值不是凭据时不会误脱敏。
"""
import re

#: 命中即整值替换为哨兵
REDACTED = '__redacted__'

#: 敏感键片段。只覆盖凭据类，不含 `id` / `name` 这类定位字段。
SENSITIVE_KEYS = frozenset({
    'password', 'passwd', 'secret', 'token', 'apikey', 'credential',
    'privatekey', 'salt', 'totp', 'recoverycode', 'authorization',
    'cookie', 'sessionid',
})

#: 非敏感字符串的最大保留长度；超出截断并加省略标记。
MAX_TEXT = 320

_NON_ALNUM = re.compile(r'[^a-z0-9]')


def _normalize_key(key):
    return _NON_ALNUM.sub('', str(key).lower())


def is_sensitive_key(key) -> bool:
    """键名是否属于凭据类；`api_key` / `apiKey` / `API-KEY` 等价。"""
    norm = _normalize_key(key)
    return any(fragment in norm for fragment in SENSITIVE_KEYS)


def _clip_text(text):
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + '…'


def redact_detail(value):
    """递归脱敏任意 JSON 兼容结构，返回可直接交给 json.dumps 的新对象。

    dict 的键保持不变，仅替换值；list/tuple 统一转 list；不可序列化的对象
    转成字符串后截断，避免 json.dumps 在审计写入路径抛异常。
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive_key(key) else redact_detail(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_detail(item) for item in value]
    if isinstance(value, str):
        return _clip_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip_text(str(value))
