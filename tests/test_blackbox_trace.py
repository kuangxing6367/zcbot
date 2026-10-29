# -*- coding: utf-8 -*-
"""内存黑匣子 · 事件流追踪单元测试。

覆盖：阶段编号体系、富字段（step/adapter/src/to/subscribers/waiting/queue）、
单事件完整链路 get_event_trace、卡在第几步 stuck_at、event_id 过滤、recent_event_ids。
运行：python tests/test_blackbox_trace.py  或  pytest tests/test_blackbox_trace.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from framework.blackbox import BlackBox, STAGE_STEP  # noqa: E402


def test_stage_step_complete():
    """标准事件流阶段均有步骤编号，且按正确顺序。"""
    expect = ['ingest', 'enqueue', 'dequeue', 'classify', 'raw_handlers',
              'route_start', 'plugin_eval', 'plugin_hit', 'handler_enter',
              'handler_ctx', 'handler_exit', 'broadcast', 'keyword', 'unmatched',
              'send', 'done']
    for s in expect:
        assert s in STAGE_STEP, f"阶段缺失步骤编号: {s}"
    nums = [STAGE_STEP[s] for s in expect]
    assert nums == sorted(nums), "阶段步骤编号未按顺序"


def test_record_rich_fields():
    """record 新字段（step/adapter/src/to/subscribers/waiting/queue）正确落帧。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    bb.record('route_start', plugin='router', event_id='E1',
              subscribers='a,b,c', msg='候选 3 个')
    bb.record('send', plugin='echo', event_id='E1', step=15,
              to='群123', adapter='default', msg='发送文本')
    frames = bb.get_frames(event_id='E1')
    by_stage = {f['stage']: f for f in frames}
    assert by_stage['route_start']['subs'] == 'a,b,c'
    assert by_stage['send']['to'] == '群123'
    assert by_stage['send']['adapter'] == 'default'
    assert by_stage['send']['step'] == 15


def test_get_event_trace_ordered():
    """get_event_trace 返回某事件按时间排序的完整链路。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    eid = 'EV-TRACE'
    bb.record('ingest', event_id=eid, msg='进入内核')
    time.sleep(0.01)
    bb.record('classify', event_id=eid, msg='分类')
    time.sleep(0.01)
    bb.record('route_start', event_id=eid, msg='路由开始')
    trace = bb.get_event_trace(eid)
    assert [f['stage'] for f in trace] == ['ingest', 'classify', 'route_start']
    # 步骤编号推断：未显式传 step 时按 STAGE_STEP 推断
    assert trace[0]['step'] == STAGE_STEP['ingest']


def test_stuck_at_returns_last_step():
    """未走完的事件：stuck_at 返回最后一步（卡在第几步）。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    eid = 'EV-STUCK'
    bb.record('ingest', event_id=eid)
    bb.record('classify', event_id=eid)
    bb.record('route_start', event_id=eid)   # 卡在路由开始，没有后续
    s = bb.stuck_at(eid)
    assert s is not None
    assert s['stage'] == 'route_start'
    assert s['step'] == STAGE_STEP['route_start']


def test_stuck_at_none_when_done():
    """事件已抵达 DONE：stuck_at 返回 None（未卡住）。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    eid = 'EV-DONE'
    bb.record('ingest', event_id=eid)
    bb.record('send', event_id=eid, step=15)
    bb.record('done', event_id=eid, step=16)
    assert bb.stuck_at(eid) is None


def test_event_id_filter_isolated():
    """不同 event_id 的帧互不污染。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    bb.record('ingest', event_id='A', msg='A1')
    bb.record('ingest', event_id='B', msg='B1')
    bb.record('send', event_id='A', step=15, to='群1')
    a_frames = bb.get_frames(event_id='A')
    assert len(a_frames) == 2
    assert all(f['eid'] == 'A' for f in a_frames)
    assert any(f['to'] == '群1' for f in a_frames)


def test_recent_event_ids():
    """recent_event_ids 返回最近出现过的事件 id（倒序、去重）。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    for eid in ['x1', 'x2', 'x1', 'x3']:
        bb.record('ingest', event_id=eid)
    recents = bb.recent_event_ids(limit=10)
    assert 'x1' in recents and 'x2' in recents and 'x3' in recents
    assert len(recents) == 3
    assert recents[0] == 'x3'   # 最近出现的排最前


def test_waiting_and_queue_fields():
    """enqueue 阶段的 waiting/queue 字段正确记录（回答「是否在等待」）。"""
    bb = BlackBox()
    bb.configure(enabled=True, max_bytes=102400)
    bb.clear()
    bb.record('enqueue', event_id='Q1', waiting=True, queue='L1=2000/2000 L3=0 L2=3 drop=0')
    f = bb.get_frames(event_id='Q1')[0]
    assert f['waiting'] == '1'
    assert 'L1=' in f['queue']


if __name__ == '__main__':
    cases = [
        test_stage_step_complete,
        test_record_rich_fields,
        test_get_event_trace_ordered,
        test_stuck_at_returns_last_step,
        test_stuck_at_none_when_done,
        test_event_id_filter_isolated,
        test_recent_event_ids,
        test_waiting_and_queue_fields,
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
