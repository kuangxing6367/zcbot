# -*- coding: utf-8 -*-
"""验证事件缓冲分层语义（v1.7.3+，含 v1.8 L4 攒批层）：
1. L1 装不下的中等事件（> L1 上限、< L4 上限）优先进 L4 攒批层，由后台 flush 批量落 L2；
2. 超大事件（> L4 上限）或 L2 写失败才回落 L3 内存兜底，其次落 L2；
3. L1 为空时从 L2 回填进 L1 再按 L1 路径消费（source='l1'），而非直接吐 sqlite；
4. 回填放不下（超 L1 上限的历史大块）才直接吐 sqlite。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from framework.core.event_buffer import EventBuffer

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


class BigEvent:
    def __repr__(self):
        return '{"event":"img","data":"' + 'A' * (1024 * 1024) + '"}'


class SmallEvent:
    def __init__(self, n):
        self.n = n

    def __repr__(self):
        return '{"event":"text%d","data":"%s"}' % (self.n, "b" * 100)


def make_buf(sqlite_on=True):
    tmp = tempfile.mkdtemp()
    cfg = {
        'maxsize': 50,
        'buffer': {
            'l1_max_bytes': 512 * 1024,      # 512KB
            'sqlite_enabled': sqlite_on,
            'sqlite_path': os.path.join(tmp, 'event_buffer.db'),
            'sqlite_write_timeout': 1.0,
            'l3_max_bytes': 4 * 1024 * 1024,  # 4MB
            'sqlite_batch': 8,
        },
    }
    return EventBuffer(cfg, tmp)


async def test_big_event_prefers_l4():
    """中等事件（1MB，> L1 上限且 < L4 上限）溢出进 L4 攒批层，不落 L2"""
    buf = make_buf(sqlite_on=True)
    ev = BigEvent()
    ok = await buf.put(ev, None)
    st = buf.stats()
    check("1MB 事件入队成功", ok)
    check("1MB 事件落 L4 攒批层（l4_items=1）", st['l4_items'] == 1, f"l4_items={st['l4_items']}")
    check("1MB 事件未落 L2（overflow_to_sqlite=0）", st['overflow_to_sqlite'] == 0,
          f"overflow={st['overflow_to_sqlite']}")
    check("sqlite 行数为 0", buf.sqlite_count() == 0, f"rows={buf.sqlite_count()}")
    # 消费优先从 L4 取走（防事件滞留 L4 丢失）
    e, done, src = await buf.get_async()
    check("消费 source='l4'", src == 'l4', f"src={src}")
    buf.close()


async def test_l2_refills_l1_when_l1_empty():
    """L1 空且 L2 有积压时：L2 回填 L1，消费走 L1 路径而非直接 sqlite"""
    buf = make_buf(sqlite_on=True)
    # 直接向 sqlite 灌入小事件（模拟上一进程遗留/纯落 L2 的历史积压）
    for i in range(5):
        buf._write_sqlite(SmallEvent(i))
    check("sqlite 积压 5 条", buf._sqlite_rows == 5, f"rows={buf._sqlite_rows}")
    # L1 为空 → get_async 应触发回填，source='l1'
    e, done, src = await buf.get_async()
    check("L2 积压消费 source='l1'（回填路径）", src == 'l1', f"src={src}  e={e!r}")
    check("回填后 L1 仍有剩余（批量回填）", not buf._l1.empty())
    buf.close()


async def test_l2_big_leftover_direct_out():
    """回填放不下（单条超 L1 上限）才直接吐 sqlite"""
    buf = make_buf(sqlite_on=True)
    buf._write_sqlite(BigEvent())          # 1MB 历史大块落 L2
    check("sqlite 积压 1 条大块", buf._sqlite_rows == 1)
    e, done, src = await buf.get_async()
    check("超 L1 大块直接吐 sqlite（source='sqlite'）", src == 'sqlite', f"src={src}")
    buf.close()


async def test_sqlite_off_big_goes_l4():
    """sqlite 关闭时 1MB 事件仍溢出进 L4（L2 关闭也不丢，后台 flush 落 file）"""
    buf = make_buf(sqlite_on=False)
    ok = await buf.put(BigEvent(), None)
    st = buf.stats()
    check("sqlite 关闭时 1MB 入队成功", ok)
    check("落 L4 攒批层", st['l4_items'] == 1, f"l4_items={st['l4_items']}")
    e, done, src = await buf.get_async()
    check("消费 source='l4'", src == 'l4', f"src={src}")
    buf.close()


async def test_mixed_fifo_order():
    """混合流：小事件填 L1 直出；中等事件走 L4——消费顺序应保持 L1 先、L4 后"""
    buf = make_buf(sqlite_on=False)
    await buf.put(SmallEvent(1), None)     # 小 → L1
    await buf.put(BigEvent(), None)        # 中(1MB) → L4
    e1, _, s1 = await buf.get_async()
    e2, _, s2 = await buf.get_async()
    check("第 1 条来自 L1（小事件）", s1 == 'l1' and getattr(e1, 'n', None) == 1, f"{s1} {e1!r}")
    check("第 2 条来自 L4（中等事件）", s2 == 'l4', f"{s2}")
    buf.close()


async def main():
    cases = [
        ("中等事件溢出进 L4 攒批层", test_big_event_prefers_l4),
        ("L2 回填 L1 消费", test_l2_refills_l1_when_l1_empty),
        ("超 L1 遗留大块直出", test_l2_big_leftover_direct_out),
        ("sqlite 关中等事件走 L4", test_sqlite_off_big_goes_l4),
        ("混合流 FIFO", test_mixed_fifo_order),
    ]
    for name, fn in cases:
        print(f"== {name} ==")
        await fn()
    print(f"\n结果: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)



if __name__ == "__main__":
    asyncio.run(main())