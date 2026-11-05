# -*- coding: utf-8 -*-
"""看门狗必须能在循环卡死时抓到栈

历史 bug：线程 id 靠 loop.call_soon_threadsafe 回调记录，
循环一卡住该回调永不执行 → 每次都只报「栈不可用」，
等于看门狗只能告诉你「卡了」，永远告诉不了你在哪卡。
"""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.core.loop_watchdog import LoopWatchdog


def _run_blocked_loop(wd, started, hold):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def in_loop():
        wd.start(loop=loop)        # 由事件循环线程启动（与框架一致）
        started.set()
        hold.wait(2.0)             # 同步阻塞循环线程，模拟卡顿
        loop.stop()

    loop.call_soon(in_loop)
    loop.run_forever()


def test_capture_stack_while_blocked():
    wd = LoopWatchdog(interval=0.05, threshold=0.1)
    started, hold = threading.Event(), threading.Event()
    t = threading.Thread(target=_run_blocked_loop, args=(wd, started, hold),
                         daemon=True)
    t.start()
    assert started.wait(2.0), '循环线程未启动'
    try:
        time.sleep(0.05)
        stack = wd._capture_stack()
        assert '无法获取事件循环线程 id' not in stack, stack
        assert '事件循环线程已退出' not in stack, stack
        assert 'in_loop' in stack or 'wait' in stack, stack
        return '卡死时仍抓到栈: ' + stack.strip().splitlines()[0][:60]
    finally:
        hold.set()
        t.join(2.0)
        wd.stop()


def test_id_recorded_at_start():
    """start() 当场就记下线程 id，不依赖任何回调"""
    wd = LoopWatchdog(interval=0.05, threshold=0.1)
    assert wd._loop_thread_id is None
    wd._loop_thread_id = threading.get_ident()   # 模拟 start 内的赋值
    stack = wd._capture_stack()
    assert '栈不可用' not in stack
    return '同线程可抓栈'


if __name__ == '__main__':
    ok = fail = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                print(f'PASS {name}: {fn()}')
                ok += 1
            except Exception as e:
                print(f'FAIL {name}: {type(e).__name__}: {e}')
                fail += 1
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
