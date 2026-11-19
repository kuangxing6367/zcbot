"""SQLite 跨 Python 版本兼容层。

Python 3.12 起 ``sqlite3`` 的默认 datetime adapter/converter 被标记弃用，
不同 Python 版本对同声明列（DATETIME / TIMESTAMP）返回的类型会漂移：实测
3.13 下 DATETIME 仍是字符串而 TIMESTAMP 已是 datetime。框架里既有代码两种
都要吃，但把「靠运行时版本猜类型」变成显式契约更稳，也避免弃用告警。

这里按 Python 官方给出的 adapter/converter recipe 自己注册，不依赖默认实现。
另提供一组能力标记，供备份/恢复在 SQLite 版本偏旧时提前走降级路径，而不是
先抛一条看不懂的 SQL 错误再兜底。
"""
import datetime as _dt
import platform
import sqlite3

__all__ = [
    'capabilities',
    'ensure_registered',
    'has_recover_cli',
    'has_vacuum_into',
    'parse_date',
    'parse_datetime',
    'to_iso',
]

_DATE_FORMATS = ('%Y-%m-%d',)
_DATETIME_FORMATS = (
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%Y-%m-%dT%H:%M:%S',
)
_REGISTERED = False


def _text(raw):
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return str(raw)


def parse_datetime(raw):
    """DATETIME / TIMESTAMP 列统一转 datetime；格式不认识就原样返回字符串。"""
    value = _text(raw).strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return value


def parse_date(raw):
    """DATE 列统一转 date；格式不认识就原样返回字符串。"""
    value = _text(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return value


def to_iso(value):
    """datetime/date 参数统一写成 SQLite 能比较的 ISO 文本。"""
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=' ')
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def ensure_registered():
    """注册显式适配器/转换器。重复调用安全，避免多插件加载时互相踩。"""
    global _REGISTERED
    if _REGISTERED:
        return
    sqlite3.register_adapter(_dt.datetime, to_iso)
    sqlite3.register_adapter(_dt.date, to_iso)
    sqlite3.register_converter('datetime', parse_datetime)
    sqlite3.register_converter('timestamp', parse_datetime)
    sqlite3.register_converter('date', parse_date)
    _REGISTERED = True


def _version_tuple():
    v = sqlite3.sqlite_version_info
    return (int(v[0]), int(v[1]), int(v[2]) if len(v) > 2 else 0)


def capabilities() -> dict:
    """当前运行时的 SQLite 能力，供日志、备份与恢复分支判定。"""
    v = _version_tuple()
    return {
        'python': platform.python_version(),
        'sqlite': sqlite3.sqlite_version,
        'version_tuple': v,
        'vacuum_into': v >= (3, 27, 0),
        'recover_cli': v >= (3, 32, 0),
        'drop_column': v >= (3, 35, 0),
        'returning': v >= (3, 35, 0),
    }


def has_vacuum_into() -> bool:
    return capabilities()['vacuum_into']


def has_recover_cli() -> bool:
    return capabilities()['recover_cli']


# 导入即生效：任何走 db_conn / backup / recovery 的路径都不该再吃到
# Python 版本差异带来的类型漂移或弃用告警。
ensure_registered()
