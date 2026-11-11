# -*- coding: utf-8 -*-
"""ZIP 解包的安全校验与安全解包（生产代码共用）。

依据：
  · Python zipfile 文档 —— 不要解压来源不可信的归档；extractall 会自行剥离
    部分路径，但不会限制条目数、总展开体积或文件名合法性
  · OWASP WSTG "Test Upload of Malicious Files" —— Archive Directory Traversal
    与 Zip Bombs 需显式防护
  · CWE-409（高度压缩数据）/ CWE-400（资源消耗）/ CWE-22（路径穿越）
  · CWE-434（危险文件上传）/ CWE-73（外部控制文件名）
  · Microsoft Learn "Naming Files..." —— CON/NUL/COM1..9/LPT1..9、尾随点空格、
    冒号（NTFS ADS）在 Windows 上不可用或会写到非预期位置
  · Unicode UTR #36 / UTS #39 —— NFC + casefold 碰撞在不区分大小写的文件系统上
    会造成静默覆盖

说明：CPython 的 zipfile._extract_member 本身会剥离 `..` 与盘符，因此经典
Zip Slip 在 zcbot 上并不能直接写出目标目录之外；本模块仍做 canonical
containment 复核（纵深防御），重点补的是**资源配额与命名可控性**缺口。
"""
import os
import re
import unicodedata
import zipfile

__all__ = ['SafeArchiveError', 'DEFAULT_LIMITS', 'FRAMEWORK_UPDATE_LIMITS',
           'validate_zip', 'safe_extract_zip']

DEFAULT_LIMITS = {
    'max_entries': 1000,
    'max_total_bytes': 64 * 1024 * 1024,
    'max_single_bytes': 16 * 1024 * 1024,
    'max_ratio': 200,
    'max_depth': 16,
    'max_name_len': 255,
}
# 框架源码更新包来自 GitHub archive，条目数远多于插件包，单独放宽
FRAMEWORK_UPDATE_LIMITS = {
    'max_entries': 12000,
    'max_total_bytes': 512 * 1024 * 1024,
    'max_single_bytes': 64 * 1024 * 1024,
    'max_ratio': 200,
    'max_depth': 24,
    'max_name_len': 255,
}

_WINDOWS_RESERVED = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'] +
    ['COM%d' % i for i in range(1, 10)] +
    ['LPT%d' % i for i in range(1, 10)]
)
# UNIX 特殊文件类型（symlink / char dev / block dev / FIFO / socket 等）
_FORBIDDEN_FILE_TYPES = frozenset(
    [0x1000, 0x2000, 0x3000, 0x5000, 0x6000, 0xA000, 0xB000, 0xC000, 0xD000]
)


class SafeArchiveError(ValueError):
    """归档不合规，拒绝解压。"""


def _limits(overrides=None):
    lim = dict(DEFAULT_LIMITS)
    if overrides:
        lim.update(overrides)
    return lim


def _check_name(name, lim):
    if not isinstance(name, str) or not name:
        raise SafeArchiveError('empty archive member name')
    if len(name.encode('utf-8', 'surrogatepass')) > 255 or len(name) > lim['max_name_len']:
        raise SafeArchiveError('archive member name too long: %r' % name[:80])
    if '\x00' in name or any(ord(c) < 32 for c in name):
        raise SafeArchiveError('control character in archive member name')
    if '\\' in name:
        raise SafeArchiveError('backslash in archive member name: %r' % name[:80])
    if name.startswith('/'):
        raise SafeArchiveError('absolute archive member path: %r' % name[:80])
    if re.match(r'^[A-Za-z]:', name):
        raise SafeArchiveError('drive-qualified archive member path: %r' % name[:80])
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts:
        return
    if len(parts) > lim['max_depth']:
        raise SafeArchiveError('archive member path too deep')
    for part in parts:
        if part == '..':
            raise SafeArchiveError('path traversal segment in %r' % name[:80])
        if ':' in part:
            raise SafeArchiveError('NTFS alternate data stream / colon in %r' % part[:80])
        if part != part.strip() or part.endswith('.'):
            raise SafeArchiveError('trailing dot/space in %r' % part[:80])
        stem = part.rsplit('.', 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise SafeArchiveError('Windows reserved device name %r' % part[:80])


def validate_zip(zf, limits=None):
    """只校验、不落盘。返回统计字典；不合规抛 SafeArchiveError。

    必须先于「移动旧目录 / 覆盖代码」等不可逆动作调用。
    """
    lim = _limits(limits)
    infos = zf.infolist()
    n = len(infos)
    if n > lim['max_entries']:
        raise SafeArchiveError('too many archive entries: %d > %d'
                               % (n, lim['max_entries']))

    total = 0
    compressed = 0
    seen = set()
    for info in infos:
        name = info.filename
        _check_name(name, lim)
        # 大小写 / NFC 归一化后重名：Windows / macOS 默认文件系统会静默覆盖
        key = unicodedata.normalize('NFC', name).casefold()
        if key in seen:
            raise SafeArchiveError('case/normalization collision: %r' % name[:80])
        seen.add(key)

        mode = (info.external_attr >> 16) & 0xFFFF
        if (mode & 0xF000) in _FORBIDDEN_FILE_TYPES:
            raise SafeArchiveError('special file type (symlink/device/FIFO): %r' % name[:80])

        size = int(info.file_size or 0)
        csize = int(info.compress_size or 0)
        compressed += csize
        if size > lim['max_single_bytes']:
            raise SafeArchiveError('single member too large: %d bytes' % size)
        # 单条压缩比只对大文件判定，避免误伤正常的小文本/清单文件
        if size > 1024 * 1024 and csize > 0 and size / csize > lim['max_ratio']:
            raise SafeArchiveError('single member compression ratio too high: %.0f' % (size / csize))
        total += size
        if total > lim['max_total_bytes']:
            raise SafeArchiveError('total uncompressed size exceeds %d bytes'
                                   % lim['max_total_bytes'])

    ratio = total / max(compressed, 1)
    if ratio > lim['max_ratio']:
        raise SafeArchiveError('archive compression ratio too high: %.0f (> %d), zip bomb?'
                               % (ratio, lim['max_ratio']))
    return {'entries': n, 'compressed_bytes': compressed,
            'uncompressed_bytes': total, 'ratio': round(ratio, 2)}


def safe_extract_zip(zf, dest, limits=None, prevalidated=False):
    """校验并解压；解压前逐条做 canonical containment 复核。

    prevalidated=True 用于调用方已经在不可逆操作前跑过 validate_zip 的场景，
    避免重复遍历（不改变安全语义，containment 复核仍执行）。
    """
    stats = None if prevalidated else validate_zip(zf, limits)
    dest_real = os.path.realpath(dest)
    os.makedirs(dest_real, exist_ok=True)
    for info in zf.infolist():
        target = os.path.realpath(os.path.join(dest_real, info.filename))
        try:
            contained = os.path.commonpath([dest_real, target]) == dest_real
        except ValueError:
            contained = False
        if not contained:
            raise SafeArchiveError('member escapes destination: %r' % info.filename[:80])
    zf.extractall(dest_real)
    if stats is None:
        stats = validate_zip(zf, limits)
    return stats
