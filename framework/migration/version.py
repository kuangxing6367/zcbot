"""内核版本检测（SemVer 2.0.0）。

依据 `semver.org` 规范：版本必须是 ``MAJOR.MINOR.PATCH``，可带
``-prerelease`` 与 ``+build``；优先层级按主/次/修订号数值比较，再比先行版
标识符（数字标识符优先层级低于字母数字标识符，纯数字按数值、其余按 ASCII）。
编译元数据不参与比较。

版本事实来源（按优先级，全部本地、不联网）：
  1. ``VERSION`` 文件（源码 checkout 的权威口径）；
  2. 已安装发行包元数据（``importlib.metadata``，wheel / pip 安装态的唯一来源）；
  3. ``pyproject.toml`` 的 ``[project] version``（打包口径）；
  4. 全都缺失/非法时返回 ``0.0.0+unknown``，并把漂移情况暴露给调用方。

本模块只用标准库，可在依赖自检之前安全 import。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from framework import paths

logger = logging.getLogger('zcbot')

__all__ = [
    'Version',
    'compare_versions',
    'current_version',
    'current_version_string',
    'parse_version',
    'version_sources',
]

# semver.org 官方推荐的 PCRE 命名组正则（原文见规范 FAQ）
_SEMVER_RE = re.compile(
    r'^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)'
    r'(?:-(?P<pre>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)'
    r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?'
    r'(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
)

_VERSION_FILE_LIMIT = 256          # 版本文件不可能更大，防止读炸弹
_PYPROJECT_LIMIT = 256 * 1024
_PYPROJECT_RE = re.compile(r'^\s*version\s*=\s*["\']([^"\']{1,64})["\']', re.MULTILINE)


@dataclass(frozen=True)
class Version:
    raw: str
    major: int
    minor: int
    patch: int
    prerelease: tuple = ()
    build: str = ''

    def __str__(self) -> str:
        return self.raw

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def as_dict(self) -> dict:
        return {
            'version': self.raw,
            'major': self.major,
            'minor': self.minor,
            'patch': self.patch,
            'prerelease': list(self.prerelease),
            'build': self.build,
        }


def parse_version(text) -> Version | None:
    """解析 SemVer 字符串；容忍常见的 ``v`` 前缀，非法返回 ``None``。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw or len(raw) > 128:
        return None
    if raw[:1] in ('v', 'V'):
        raw = raw[1:]
    m = _SEMVER_RE.match(raw)
    if not m:
        return None
    pre = m.group('pre')
    return Version(
        raw=raw,
        major=int(m.group('major')),
        minor=int(m.group('minor')),
        patch=int(m.group('patch')),
        prerelease=tuple(pre.split('.')) if pre else (),
        build=m.group('build') or '',
    )


def _cmp_pre(a: tuple, b: tuple) -> int:
    """先行版标识符比较；无先行版的版本优先层级更高。"""
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for x, y in zip(a, b):
        if x == y:
            continue
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            return -1 if int(x) < int(y) else 1
        if xn:
            return -1          # 数字标识符优先层级低于非数字
        if yn:
            return 1
        return -1 if x < y else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a, b) -> int:
    """按 SemVer 优先层级比较，返回 -1 / 0 / 1；任一非法时按字符串兜底。"""
    va = a if isinstance(a, Version) else parse_version(a)
    vb = b if isinstance(b, Version) else parse_version(b)
    if va is None or vb is None:
        sa = str(a) if a is not None else ''
        sb = str(b) if b is not None else ''
        return (sa > sb) - (sa < sb)
    for x, y in ((va.major, vb.major), (va.minor, vb.minor), (va.patch, vb.patch)):
        if x != y:
            return -1 if x < y else 1
    return _cmp_pre(va.prerelease, vb.prerelease)


def _read_version_file() -> tuple[str | None, str]:
    try:
        with open(paths.version_path(), 'rb') as f:
            raw = f.read(_VERSION_FILE_LIMIT + 1)
        if len(raw) > _VERSION_FILE_LIMIT:
            return None, 'VERSION 文件超过 256 字节'
        return raw.decode('utf-8-sig', errors='replace').strip() or None, 'VERSION'
    except OSError as e:
        return None, 'VERSION 不可读: %s' % type(e).__name__


def _read_pyproject_version() -> tuple[str | None, str]:
    try:
        path = os.path.join(paths.code_root(), 'pyproject.toml')
        with open(path, 'rb') as f:
            raw = f.read(_PYPROJECT_LIMIT + 1)
        if len(raw) > _PYPROJECT_LIMIT:
            return None, 'pyproject.toml 过大'
        m = _PYPROJECT_RE.search(raw.decode('utf-8-sig', errors='replace'))
        return (m.group(1).strip(), 'pyproject.toml') if m else (None, 'pyproject 无 version')
    except OSError as e:
        return None, 'pyproject.toml 不可读: %s' % type(e).__name__


def _read_installed_metadata() -> tuple[str | None, str]:
    """从已安装发行包的元数据取版本（wheel / pip 安装态唯一可靠的来源）。

    wheel 里既没有 ``VERSION`` 也没有 ``pyproject.toml``（两者都不属于包）。
    原实现只认这两个文件，于是 ``pip install zcbot`` 之后 ``zb -v`` 恒定输出
    ``0.0.0`` —— 版本自报在本发行形态下永远是错的。改读 ``dist-info``，
    这是 PEP 566 规定的metadata，pip 装出来的包必然带着。
    """
    try:
        from importlib import metadata
    except Exception:                       # pragma: no cover  py<3.8
        return None, 'importlib.metadata 不可用'
    try:
        return metadata.version('zcbot'), 'installed metadata'
    except Exception as e:                  # PackageNotFoundError 及各种坏元数据
        logger.debug('[_read_installed_metadata] 捕获异常后按降级路径继续: %s', e)
        return None, 'installed metadata 不可读: %s' % type(e).__name__


def version_sources() -> dict:
    """返回全部版本来源及漂移情况（供探测报告与排障使用）。"""
    file_raw, file_note = _read_version_file()
    meta_raw, meta_note = _read_installed_metadata()
    proj_raw, proj_note = _read_pyproject_version()
    fv, pv = parse_version(file_raw), parse_version(proj_raw)
    consistent = (fv is not None and pv is not None
                  and compare_versions(fv, pv) == 0 and fv.raw == pv.raw)
    return {
        'version_file': {'raw': file_raw, 'parsed': fv.as_dict() if fv else None,
                         'note': file_note},
        'installed_metadata': {'raw': meta_raw, 'parsed': (
            parse_version(meta_raw).as_dict() if parse_version(meta_raw) else None),
            'note': meta_note},
        'pyproject': {'raw': proj_raw, 'parsed': pv.as_dict() if pv else None,
                      'note': proj_note},
        'consistent': consistent,
        'drift': not consistent and fv is not None and pv is not None,
    }


def current_version() -> Version:
    """当前运行内核版本。

    优先级：``VERSION`` 文件（源码 checkout，发布口径）→ 已安装包元数据
    （wheel / pip 安装态）→ ``pyproject.toml`` → ``0.0.0+unknown``。
    元数据排在 VERSION 之后：源码目录里两者同时存在时应以仓库里的
    ``VERSION`` 为准，避免手上代码改了版本而环境里还是旧包时读到旧号。
    """
    file_raw, _ = _read_version_file()
    v = parse_version(file_raw)
    if v is not None:
        return v
    meta_raw, _ = _read_installed_metadata()
    v = parse_version(meta_raw)
    if v is not None:
        return v
    proj_raw, _ = _read_pyproject_version()
    v = parse_version(proj_raw)
    if v is not None:
        return v
    return parse_version('0.0.0+unknown')


def current_version_string() -> str:
    return current_version().raw
