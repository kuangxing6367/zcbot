# -*- coding: utf-8 -*-
"""当前目录下「可能是哪一代版本留下的布局」的探测。

只依据**本地可观测的磁盘特征**推断，不联网、不读 .git、不假装能精确报出
版本号。版本区间锚点全部取自本仓库 ``CHANGELOG.md`` 的真实记录：

- ``CHANGELOG.md`` [1.21.1] 节：1.21.0 起新增 ``data/`` 状态布局
  （``framework/paths.py`` 统一解析层），旧根目录布局保留兼容回退；
- ``CHANGELOG.md`` 1.8.9 及其后各版：``data/`` 只承载运行产物
  （``data/zcbot.lock``、``data/plugins_dat/``、``data/zcbot.db``、
  ``data/matrix_media/`` 等），而 ``config.yaml`` / ``core_plugins.yaml`` /
  ``plugins/`` 一直在根目录；
- 代际主线表记录 v0.0.1-alpha 起就有框架，但没有逐版本的目录结构记录，
  因此 1.8.9 之前只能给「早期版本」这一低置信度区间，不能报精确版本号。

判定的是**布局代际**，不是版本号。输出里 ``detected_candidates`` 是候选
区间 + 依据 + 置信度，供迁移策略决定是否执行、以及事后审计追溯。
"""
from __future__ import annotations

import os

from framework import paths

__all__ = ['probe', 'LAYOUT_ROOT', 'LAYOUT_DATA', 'LAYOUT_MIXED',
           'LAYOUT_EMPTY', 'LAYOUT_MISSING', 'LAYOUT_UNKNOWN',
           'LAYOUT_ANCHORS']

LAYOUT_ROOT = 'root'          # 旧布局：状态文件在 <home>/ 根
LAYOUT_DATA = 'data'          # 新布局：状态文件在 <home>/data/
LAYOUT_MIXED = 'mixed'        # 两代并存（配置分叉风险）
LAYOUT_EMPTY = 'empty'        # 全新 home，无状态文件（首次启动基线）
LAYOUT_MISSING = 'missing'    # home 目录不存在
LAYOUT_UNKNOWN = 'unknown'    # 有目录但特征不足以判定

STATE_CONFIG = 'config.yaml'
STATE_CORE_YAML = 'core_plugins.yaml'
STATE_PLUGINS = 'plugins'

# 真实证据锚点：只写 CHANGELOG / 源码里能查到的事实，不编造版本细节
LAYOUT_ANCHORS = (
    {
        'range': '>=1.21.0',
        'layout': LAYOUT_DATA,
        'label': 'data/ 状态布局（1.21.0 起）',
        'basis': 'CHANGELOG [1.21.1] 节 + framework/paths.py：配置、清单、用户插件统一落 data/',
    },
    {
        'range': '1.8.9..1.20.2',
        'layout': LAYOUT_ROOT,
        'label': '根目录状态布局（有明确记录的版本区间）',
        'basis': 'CHANGELOG 1.8.9 起 data/ 只放 zcbot.lock / plugins_dat / zcbot.db 等运行产物；'
                 '1.20.2 之前的真实仓库状态文件仍在根目录',
    },
    {
        'range': '<1.8.9（含 v0.x / v1.x 早期）',
        'layout': LAYOUT_ROOT,
        'label': '早期版本（无逐版本目录记录）',
        'basis': 'CHANGELOG 代际主线表记录 v0.0.1-alpha 起即有框架，但没有逐版本目录结构记录',
    },
)

_RUNTIME_MARKERS = ('zcbot.db', 'zcbot.lock', 'plugins_dat', 'logs',
                    'event_buffer.db', 'matrix_media')


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _is_dir(path: str) -> bool:
    return os.path.isdir(path)


def _escapes_home(path: str, home: str) -> bool:
    """符号链接 / 联接点是否把路径指到 home 之外（防误搬外部目录）。"""
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(home)
        return os.path.commonpath([real, base]) != base
    except (ValueError, OSError):
        return True


def probe(home=None) -> dict:
    """探测 ``home`` 目录的布局代际，返回可 JSON 序列化的结构化报告。"""
    home = os.path.abspath(home) if home else paths.home_dir()
    state = paths.state_dir() if not home else os.path.join(home, 'data')
    warnings = []
    exists = _is_dir(home)

    def _feature(name: str, path: str, kind: str) -> bool:
        present = _is_dir(path) if kind == 'dir' else _is_file(path)
        if present and os.path.islink(path) and _escapes_home(path, home):
            warnings.append('%s 是指向 home 之外的符号链接，自动迁移将跳过此项' % name)
        return present

    evidence = {
        'home_exists': exists,
        'root_config': _feature('root_config', os.path.join(home, STATE_CONFIG), 'file'),
        'root_core_plugins_yaml': _feature('root_core_plugins_yaml',
                                           os.path.join(home, STATE_CORE_YAML), 'file'),
        'root_plugins': _feature('root_plugins', os.path.join(home, STATE_PLUGINS), 'dir'),
        'data_config': _feature('data_config', os.path.join(state, STATE_CONFIG), 'file'),
        'data_core_plugins_yaml': _feature('data_core_plugins_yaml',
                                           os.path.join(state, STATE_CORE_YAML), 'file'),
        'data_plugins': _feature('data_plugins', os.path.join(state, STATE_PLUGINS), 'dir'),
        'data_dir': _feature('data_dir', state, 'dir'),
        'migration_ledger': _feature('migration_ledger',
                                     os.path.join(state, paths.MIGRATION_STATE_NAME), 'file'),
    }
    evidence['data_runtime_markers'] = sorted(
        m for m in _RUNTIME_MARKERS if os.path.exists(os.path.join(state, m)))

    # 畸形特征：本该是目录/文件的位置类型不对
    for key, path, want in (
        ('root_plugins', os.path.join(home, STATE_PLUGINS), 'dir'),
        ('data_plugins', os.path.join(state, STATE_PLUGINS), 'dir'),
    ):
        if os.path.exists(path) and not evidence[key]:
            warnings.append('%s 存在但不是目录，已按缺失处理' % key)

    root_state = any(evidence[k] for k in
                     ('root_config', 'root_core_plugins_yaml', 'root_plugins'))
    data_state = any(evidence[k] for k in
                     ('data_config', 'data_core_plugins_yaml', 'data_plugins'))

    if not exists:
        layout = LAYOUT_MISSING
    elif root_state and data_state:
        layout = LAYOUT_MIXED
        warnings.append('新旧两处同时存在状态文件，存在配置分叉风险；自动迁移不会覆盖 data/ 下已有文件')
    elif data_state:
        layout = LAYOUT_DATA
    elif root_state:
        layout = LAYOUT_ROOT
    elif not evidence['data_dir'] and not evidence['data_runtime_markers']:
        layout = LAYOUT_EMPTY
    else:
        layout = LAYOUT_UNKNOWN
        warnings.append('只发现运行产物或空 data/，无法判定是哪一代布局')

    candidates = []
    if layout in (LAYOUT_ROOT, LAYOUT_MIXED):
        candidates = [
            {**LAYOUT_ANCHORS[1], 'confidence': 'medium'},
            {**LAYOUT_ANCHORS[2], 'confidence': 'low'},
        ]
    elif layout == LAYOUT_DATA:
        candidates = [{**LAYOUT_ANCHORS[0], 'confidence': 'high'
                       if evidence['migration_ledger'] else 'medium'}]
    elif layout == LAYOUT_MIXED:
        candidates.insert(0, {**LAYOUT_ANCHORS[0], 'confidence': 'medium'})

    if evidence['migration_ledger']:
        confidence = 'high'
    elif layout in (LAYOUT_ROOT, LAYOUT_DATA, LAYOUT_EMPTY, LAYOUT_MISSING):
        confidence = 'medium'
    else:
        confidence = 'low'

    return {
        'home': home,
        'state_dir': state,
        'layout': layout,
        'confidence': confidence,
        'evidence': evidence,
        'warnings': warnings,
        'detected_candidates': candidates,
        'needs_migration': layout == LAYOUT_ROOT,
        'manual_required': layout == LAYOUT_MIXED,
    }
