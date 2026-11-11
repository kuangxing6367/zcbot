# -*- coding: utf-8 -*-
"""自动迁移开关策略：**默认开启，由配置文件决定，可手动关闭**。

优先级（高 → 低）：

  1. 环境变量 ``ZCBOT_AUTO_MIGRATE``（运维/CI 逃生口，``0/false/no/off`` 关闭）；
  2. ``config.yaml`` 的 ``framework.auto_migrate``（推荐写法）；
     兼容 ``framework.migration.auto_migrate``；
  3. 都没写 / 文件不存在 / 解析失败 → ``True``（默认开启）。

约束（对应「绝不」）：
- **只读配置文件，绝不改写用户 config.yaml 的任何字节**；
- 解析失败不阻断启动，也不把「读不懂」当成「用户关掉了」，而是回落到默认开启，
  并把原因写进 ``Decision.reason`` 供日志与排障；
- 非法布尔值（如 ``auto_migrate: maybe``）按未配置处理 → 默认开启，并给出告警。

本模块只用标准库（``yaml`` 为可选增强，缺失时退回极简文本扫描），可在
依赖自检之前安全 import。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict

from framework import paths

__all__ = ['Decision', 'auto_migrate_enabled', 'read_config_switch',
           'AUTO_MIGRATE_DEFAULT', 'CONFIG_KEY', 'ENV_KEY']

AUTO_MIGRATE_DEFAULT = True
CONFIG_KEY = 'framework.auto_migrate'
COMPAT_KEY = 'framework.migration.auto_migrate'
ENV_KEY = 'ZCBOT_AUTO_MIGRATE'
MAX_CONFIG_BYTES = 2 * 1024 * 1024

_FALSE_WORDS = frozenset(('0', 'false', 'no', 'off', 'disable', 'disabled', 'never'))
_TRUE_WORDS = frozenset(('1', 'true', 'yes', 'on', 'enable', 'enabled', 'always'))
_SCAN_RE = re.compile(r'^\s{1,24}auto_migrate\s*:\s*([^#\r\n]+)', re.M)
_TOP_RE = re.compile(r'^([A-Za-z_][\w.-]*)\s*:', re.M)


@dataclass(frozen=True)
class Decision:
    enabled: bool
    source: str          # env / config / default（粗粒度判定来源）
    reason: str
    config_path: str = ''
    warnings: tuple = ()
    parse_source: str = ''   # yaml / regex / regex-fallback / absent …（细粒度解析路径）

    def as_dict(self) -> dict:
        d = asdict(self)
        d['warnings'] = list(self.warnings)
        return d


def _coerce_bool(value):
    """严格布尔解析；无法判定时返回 None（绝不把陌生值当 False）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().strip('"\' ').lower()
        if v in _FALSE_WORDS:
            return False
        if v in _TRUE_WORDS:
            return True
    return None


def _scan_text(text: str):
    """无 yaml 时的兜底：只扫 framework 段（含 migration 子段）里的 auto_migrate。"""
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        top = _TOP_RE.match(line)
        if top:
            section = top.group(1)
            continue
        m = _SCAN_RE.match(line)
        if m and section == 'framework':
            return _coerce_bool(m.group(1))
    return None


def _extract(data: dict):
    if not isinstance(data, dict):
        return None
    fw = data.get('framework')
    if isinstance(fw, dict):
        if 'auto_migrate' in fw:
            return _coerce_bool(fw.get('auto_migrate'))
        nested = fw.get('migration')
        if isinstance(nested, dict) and 'auto_migrate' in nested:
            return _coerce_bool(nested.get('auto_migrate'))
    return None


def read_config_switch(config_path=None):
    """只读地取出开关值，返回 ``(value|None, source, reason, warnings)``。

    ``value is None`` 表示「配置里没有有效值」，调用方按默认开启处理。
    """
    warnings = []
    path = paths.resolve_config_path(config_path)
    if not os.path.isfile(path):
        return None, 'missing', '配置文件不存在，按默认开启', warnings
    try:
        with open(path, 'rb') as f:
            raw = f.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None, 'too-large', '配置文件超过 2 MiB，跳过解析并按默认开启', \
                ['config.yaml 超过 2 MiB，未解析开关']
        text = raw.decode('utf-8-sig', errors='replace')
    except OSError as e:
        return None, 'unreadable', '配置文件不可读 (%s)，按默认开启' % type(e).__name__, warnings

    try:
        import yaml  # 可选依赖：启动自检之前可能还没装
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            warnings.append('config.yaml 顶层不是映射，开关按默认开启')
            return None, 'not-mapping', 'YAML 顶层不是映射，按默认开启', warnings
        value = _extract(data)
        if value is None:
            fw = data.get('framework')
            if isinstance(fw, dict) and ('auto_migrate' in fw
                                         or (isinstance(fw.get('migration'), dict)
                                             and 'auto_migrate' in fw['migration'])):
                warnings.append('framework.auto_migrate 值非法，按默认开启')
                return None, 'invalid', '开关值无法判定为布尔，按默认开启', warnings
            return None, 'absent', '配置未写开关，按默认开启', warnings
        return value, 'yaml', '读取 %s' % CONFIG_KEY, warnings
    except ImportError:
        value = _scan_text(text)
        if value is None:
            return None, 'regex-absent', 'yaml 未安装且文本扫描未发现开关，按默认开启', warnings
        return value, 'regex', 'yaml 未安装，按文本扫描读取开关', warnings
    except Exception as e:  # yaml 解析失败不能变成「用户关了自动迁移」
        warnings.append('config.yaml YAML 解析失败: %s' % type(e).__name__)
        value = _scan_text(text)
        reason = 'YAML 解析失败，文本扫描%s' % ('读到开关' if value is not None else '未读到开关，按默认开启')
        return value, 'regex-fallback', reason, warnings


def auto_migrate_enabled(config_path=None) -> Decision:
    """返回本次启动是否执行自动迁移的判定结果。"""
    path = paths.resolve_config_path(config_path)
    env = os.environ.get(ENV_KEY)
    if env is not None:
        v = _coerce_bool(env)
        if v is not None:
            return Decision(v, 'env', '%s=%r 覆盖配置' % (ENV_KEY, env), path)
        value, source, reason, warnings = read_config_switch(config_path)
        warnings = list(warnings) + ['%s=%r 不是合法布尔值，已忽略' % (ENV_KEY, env)]
        return Decision(AUTO_MIGRATE_DEFAULT if value is None else value,
                        'config' if value is not None else 'default',
                        reason, path, tuple(warnings), source)

    value, source, reason, warnings = read_config_switch(config_path)
    return Decision(AUTO_MIGRATE_DEFAULT if value is None else value,
                    'config' if value is not None else 'default',
                    reason, path, tuple(warnings), source)
