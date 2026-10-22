# -*- coding: utf-8 -*-
"""main.py 依赖自检可配置（dependencies.allow_missing）回归测试。

直接运行：PYTHONPATH=. python tests/test_main_selfcheck_config.py
"""
import os
import sys
import tempfile

sys.path.insert(0, '.')

import main as main_mod


def test_resolve_self_check_missing_filters_allowed():
    missing = ['websockets>=12.0', 'pillow>=10.0', 'numpy>=1.24.0']
    to_install, skipped = main_mod._resolve_self_check_missing(
        missing, ['pillow', 'numpy'])
    assert to_install == ['websockets>=12.0']
    assert skipped == ['pillow>=10.0', 'numpy>=1.24.0']


def test_resolve_self_check_missing_no_allow():
    missing = ['websockets>=12.0', 'pyyaml>=6.0']
    to_install, skipped = main_mod._resolve_self_check_missing(missing, [])
    assert to_install == missing
    assert skipped == []


def test_resolve_self_check_missing_underscore_normalized():
    # 配置里写 Pillow（驼峰），requirements 里写 pillow（小写）应等价
    missing = ['Pillow>=10.0']
    to_install, skipped = main_mod._resolve_self_check_missing(
        missing, ['pillow'])
    assert to_install == []
    assert skipped == ['Pillow>=10.0']


def test_read_self_check_allow_missing_from_config():
    cfg = (
        "database:\n  type: sqlite\n"
        "dependencies:\n  allow_missing: [pillow, numpy]\n"
    )
    fd, path = tempfile.mkstemp(suffix='.yaml', prefix='zcbot_cfg_')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(cfg)
    try:
        allow = main_mod._read_self_check_allow_missing(path)
        assert allow == {'pillow', 'numpy'}
    finally:
        os.remove(path)


def test_read_self_check_allow_missing_missing_section():
    cfg = "database:\n  type: sqlite\n"
    fd, path = tempfile.mkstemp(suffix='.yaml', prefix='zcbot_cfg_')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(cfg)
    try:
        # 无 dependencies 段 → 空集（不报错，不破坏既有行为）
        assert main_mod._read_self_check_allow_missing(path) == set()
    finally:
        os.remove(path)


if __name__ == '__main__':
    test_resolve_self_check_missing_filters_allowed()
    test_resolve_self_check_missing_no_allow()
    test_resolve_self_check_missing_underscore_normalized()
    test_read_self_check_allow_missing_from_config()
    test_read_self_check_allow_missing_missing_section()
    print('ALL SELFCHECK CONFIG TESTS PASSED')
