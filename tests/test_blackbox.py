# -*- coding: utf-8 -*-
"""内存黑匣子（Memory Black Box）单元测试。

覆盖：环形缓冲、字节上限裁剪、禁用态不落帧、clear、limit 参数、模块级转发。
运行：python tests/test_blackbox.py  或  pytest tests/test_blackbox.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from framework.blackbox import BlackBox, blackbox, record, configure, get_frames, stats, clear  # noqa: E402


def test_ring_buffer_basic():
    """记录后可读出，且 seq 递增。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    bb.record('recv', plugin='router', fn='route', event_id='e1', msg='hello')
    bb.record('plugin_hit', plugin='echo', fn='command', event_id='e1', msg='pong')
    frames = bb.get_frames()
    assert len(frames) == 2, f"期望 2 帧，实际 {len(frames)}"
    assert frames[0]['stage'] == 'recv'
    assert frames[1]['stage'] == 'plugin_hit'
    assert frames[1]['seq'] > frames[0]['seq']


def test_byte_limit_trim():
    """字节上限被突破时自动丢弃最旧帧，dropped 计数增长。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=4096)  # 最小兜底 4096
    bb.clear()
    for i in range(200):
        bb.record('recv', plugin='router', fn='route',
                  event_id=f'e{i}', msg='x' * 50)
    st = bb.stats()
    assert st['frames'] < 200, f"未裁剪: frames={st['frames']}"
    assert st['dropped'] > 0, f"dropped 未增长: {st['dropped']}"
    assert st['used_bytes'] <= st['max_bytes'], \
        f"超出字节上限: {st['used_bytes']} > {st['max_bytes']}"


def test_disabled_no_record():
    """禁用态下 record 直接返回，不落帧。"""
    bb = BlackBox()
    bb.configure(enabled=False, max_bytes=102400)
    bb.clear()
    bb.record('recv', plugin='router', fn='route', event_id='e1', msg='hi')
    assert bb.get_frames() == [], "禁用态仍落帧"
    assert bb.stats()['frames'] == 0


def test_clear():
    """clear 后帧数与字节归零。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    for i in range(10):
        bb.record('recv', event_id=f'e{i}')
    assert bb.stats()['frames'] == 10
    bb.clear()
    assert bb.get_frames() == []
    assert bb.stats()['frames'] == 0
    assert bb.stats()['used_bytes'] == 0


def test_limit_param():
    """get_frames(limit=N) 只返回最近 N 帧。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    for i in range(10):
        bb.record('recv', event_id=f'e{i}')
    last2 = bb.get_frames(limit=2)
    assert len(last2) == 2, f"limit 失效: {len(last2)}"
    assert last2[-1]['eid'] == 'e9'


def test_since_param():
    """get_frames(since=t) 只返回 t 之后的帧。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    t0 = time.time()
    time.sleep(0.01)
    bb.record('recv', event_id='before')
    time.sleep(0.01)
    t1 = time.time()
    bb.record('recv', event_id='after')
    frames = bb.get_frames(since=t1 - 0.001)
    assert [f['eid'] for f in frames] == ['after'], f"since 过滤异常: {frames}"


def test_module_level_forwards():
    """模块级转发函数与全局单例行为一致。"""
    configure(enabled=True, max_bytes=102400)
    clear()
    record('recv', plugin='x', event_id='m1', msg='mod')
    frames = get_frames()
    assert len(frames) == 1 and frames[0]['msg'] == 'mod'
    assert stats()['frames'] == 1


def test_msg_truncate():
    """超长 msg 自动截断到 200 字符。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    big = 'A' * 500
    bb.record('recv', event_id='big', msg=big)
    frames = bb.get_frames()
    assert len(frames[0]['msg']) <= 201, f"msg 未截断: {len(frames[0]['msg'])}"


if __name__ == '__main__':
    cases = [
        test_ring_buffer_basic,
        test_byte_limit_trim,
        test_disabled_no_record,
        test_clear,
        test_limit_param,
        test_since_param,
        test_module_level_forwards,
        test_msg_truncate,
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
