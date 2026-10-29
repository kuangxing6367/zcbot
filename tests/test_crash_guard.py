# -*- coding: utf-8 -*-
"""崩溃重启守卫（Crash-loop Guard）单元测试。

覆盖：计数、写入、轮转上限、拒绝启动阈值、关闭态不拦截、复位。
运行：python tests/test_crash_guard.py  或  pytest tests/test_crash_guard.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import framework.crash_guard as cg  # noqa: E402


def _cfg(max_restarts=5, enabled=True, path=None):
    cfg = {'framework': {'crash': {'enabled': enabled, 'max_restarts': max_restarts}}}
    if path is not None:
        cfg['framework']['crash']['path'] = path
    return cfg


def test_count_zero_when_no_log():
    """无崩溃日志时计数为 0。"""
    root = tempfile.mkdtemp()
    assert cg.count_crash_markers(_cfg(path='error.log'), root) == 0


def test_write_then_count():
    """写入一份崩溃日志后计数为 1。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(path='error.log')
    cg.write_crash_log(cfg, root, exc=RuntimeError('boom'))
    assert cg.count_crash_markers(cfg, root) == 1
    assert os.path.isfile(os.path.join(root, 'error.log'))


def test_rotation_bounded():
    """反复崩溃仅保留有限轮转副本（≤ _MAX_KEEP）。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(path='error.log')
    for _ in range(20):
        cg.write_crash_log(cfg, root, exc=RuntimeError('again'))
    count = cg.count_crash_markers(cfg, root)
    assert count <= cg._MAX_KEEP + 1, f"轮转副本过多: {count}"
    # 最新一份必存在
    assert os.path.isfile(os.path.join(root, 'error.log'))


def test_should_abort_below_threshold():
    """未达上限不拒绝启动。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(max_restarts=5)
    cg.write_crash_log(cfg, root, exc=RuntimeError('x'))
    assert cg.should_abort_boot(cfg, root) is False


def test_should_abort_at_threshold():
    """达到上限拒绝启动，打破无限重启死循环。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(max_restarts=5)
    for _ in range(5):
        cg.write_crash_log(cfg, root, exc=RuntimeError('x'))
    assert cg.should_abort_boot(cfg, root) is True


def test_disabled_guard_does_not_abort():
    """crash.enabled=false 时即便有崩溃记录也不拦截启动。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(max_restarts=5, enabled=False)
    for _ in range(5):
        cg.write_crash_log(cfg, root, exc=RuntimeError('x'))
    assert cg.should_abort_boot(cfg, root) is False


def test_clear_crash_markers():
    """启动成功后复位计数。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(path='error.log')
    for _ in range(3):
        cg.write_crash_log(cfg, root, exc=RuntimeError('x'))
    assert cg.count_crash_markers(cfg, root) == 3
    cg.clear_crash_markers(cfg, root)
    assert cg.count_crash_markers(cfg, root) == 0
    assert not os.path.isfile(os.path.join(root, 'error.log'))


def test_rotation_preserves_latest_traceback():
    """轮转后最新日志仍含真实异常信息。"""
    root = tempfile.mkdtemp()
    cfg = _cfg(path='error.log')
    cg.write_crash_log(cfg, root, exc=ValueError('specific-error-xyz'))
    with open(os.path.join(root, 'error.log'), 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'specific-error-xyz' in content, "崩溃日志未记录真实异常"


if __name__ == '__main__':
    cases = [
        test_count_zero_when_no_log,
        test_write_then_count,
        test_rotation_bounded,
        test_should_abort_below_threshold,
        test_should_abort_at_threshold,
        test_disabled_guard_does_not_abort,
        test_clear_crash_markers,
        test_rotation_preserves_latest_traceback,
    ]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    sys.exit(1 if failed else 0)
