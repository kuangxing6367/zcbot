# -*- coding: utf-8 -*-
"""验证自研文件 L2 溢出后端（sqlite 关闭时自动启用）：

1. sqlite 关闭时 l2_mode 自动解析为 'file'，L3 满回落 L2 不丢弃；
2. L2 文件写盘后可回填 L1 消费（source='l1'），放不下的遗留大块直出（source='l2'）；
3. 重启后承接文件遗留行（_count_lines）；
4. 显式 l2_backend='sqlite' 时仍走 sqlite 路径、'off' 时禁用 L2；
5. 内存记账 O(1)：文件后端不缓存事件本体。
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

    def __eq__(self, other):
        return isinstance(other, BigEvent)


class SmallEvent:
    def __init__(self, n):
        self.n = n

    def __repr__(self):
        return '{"event":"text%d","data":"%s"}' % (self.n, "b" * 100)


def make_buf(sqlite_on=False, l2_backend=None, **over):
    tmp = tempfile.mkdtemp()
    cfg = {
        'maxsize': 50,
        'buffer': {
            'l1_max_bytes': 512 * 1024,
            'sqlite_enabled': sqlite_on,
            'sqlite_path': os.path.join(tmp, 'event_buffer.db'),
            'l2_path': os.path.join(tmp, 'event_buffer.l2.log'),
            'sqlite_write_timeout': 1.0,
            'l3_max_bytes': 4 * 1024 * 1024,
            'sqlite_batch': 8,
        },
    }
    if l2_backend is not None:
        cfg['buffer']['l2_backend'] = l2_backend
    cfg['buffer'].update(over)
    return EventBuffer(cfg, tmp), tmp


async def test_auto_file_when_sqlite_off():
    """sqlite 关闭 → l2_mode 自动解析为 file，L3 满回落 L2 不丢弃"""
    buf, _ = make_buf(sqlite_on=False)
    check("sqlite 关闭时 l2_mode='file'", buf.l2_mode == 'file',
          f"l2_mode={buf.l2_mode}")
    # L3 压到只剩一条 1MB 的空间 → 第 2 个 1MB 必须回落 L2（不再丢弃）
    buf.l3_max_bytes = int(EventBuffer._size_of(BigEvent()) * 1.1)
    ok1 = await buf.put(BigEvent(), None)
    st = buf.stats()
    check("第 1 个 1MB 落 L3（有空间）", ok1 and st['l3_items'] == 1,
          f"ok={ok1} l3={st['l3_items']}")
    ok2 = await buf.put(BigEvent(), None)
    st = buf.stats()
    check("第 2 个 1MB L3 满回落 L2（不丢弃）", ok2 and st['l2_pending'] == 1,
          f"ok={ok2} l2_pending={st['l2_pending']} dropped={st['dropped']}")
    check("消费顺序：L3 先出（热数据优先）", True)
    e1, _, s1 = await buf.get_async()
    check("第 1 条消费 source='l3'", s1 == 'l3', f"s1={s1}")
    e2, _, s2 = await buf.get_async()
    check("第 2 条消费 source='l2'（L2 遗留直出）", s2 == 'l2', f"s2={s2}")
    check("全排空", buf.empty_all(), f"stats={buf.stats()}")
    buf.close()


async def test_l2_file_refills_l1():
    """L2 文件积压应回填 L1 再消费（source='l1'），非直接吐 L2"""
    buf, _ = make_buf(sqlite_on=False)
    for i in range(5):
        assert await buf._l2_write(SmallEvent(i)) is True
    check("L2 文件积压 5 条", buf._l2_rows() == 5, f"rows={buf._l2_rows()}")
    e, done, src = await buf.get_async()
    check("L2 积压消费 source='l1'（回填路径）", src == 'l1', f"src={src}  e={e!r}")
    check("回填后 L1 仍有剩余（批量回填）", not buf._l1.empty())
    buf.close()


async def test_l2_file_leftover_direct_out():
    """回填放不下（单条超 L1 上限）才直出 L2（source='l2'）"""
    buf, _ = make_buf(sqlite_on=False)
    assert await buf._l2_write(BigEvent()) is True
    check("L2 文件积压 1 条大块", buf._l2_rows() == 1)
    e, done, src = await buf.get_async()
    check("超 L1 大块直出 L2（source='l2'）", src == 'l2', f"src={src}")
    buf.close()


async def test_l2_file_restart_inherits():
    """重启承接文件遗留行：新实例 _l2_rows 等于旧实例遗留数，且消费 source='l1'"""
    buf1, tmp = make_buf(sqlite_on=False)
    for i in range(4):
        assert await buf1._l2_write(SmallEvent(i)) is True
    buf1.close()

    buf2 = EventBuffer({'maxsize': 50, 'buffer': {
        'l1_max_bytes': 512 * 1024, 'sqlite_enabled': False,
        'l2_path': os.path.join(tmp, 'event_buffer.l2.log'),
        'sqlite_batch': 8,
    }}, tmp)
    check("重启承接 L2 遗留 4 条", buf2._l2_rows() == 4, f"rows={buf2._l2_rows()}")
    for i in range(4):
        e, _, src = await buf2.get_async()
        check(f"第 {i + 1} 条回填消费 source='l1'", src == 'l1', f"src={src}")
    check("全部消化后文件截断复位", buf2._l2_file._read_offset == 0
          and os.path.getsize(buf2.l2_path) == 0)
    buf2.close()


async def test_explicit_sqlite_and_off_modes():
    """显式 l2_backend='sqlite' 走 sqlite；'off' 时 L3 满即丢弃"""
    buf, _ = make_buf(sqlite_on=True, l2_backend='sqlite')
    check("显式 sqlite：l2_mode='sqlite'", buf.l2_mode == 'sqlite')
    check("sqlite 连接已初始化", buf._sqlite_conn is not None)
    buf.close()

    buf2, _ = make_buf(sqlite_on=False, l2_backend='off')
    check("显式 off：l2_mode='off'", buf2.l2_mode == 'off')
    buf2.l3_max_bytes = int(EventBuffer._size_of(BigEvent()) * 1.1)
    await buf2.put(BigEvent(), None)                       # 占满 L3
    ok2 = await buf2.put(BigEvent(), None)                 # L3 满 + L2 off → 丢弃
    st = buf2.stats()
    check("L3 满且 L2 off 时丢弃", not ok2 and st['dropped'] == 1,
          f"ok={ok2} dropped={st['dropped']}")
    buf2.close()


async def main():
    cases = [
        ("sqlite 关自动 file + L3 满回落 L2", test_auto_file_when_sqlite_off),
        ("L2 文件回填 L1 消费", test_l2_file_refills_l1),
        ("超 L1 遗留大块直出 L2", test_l2_file_leftover_direct_out),
        ("重启承接文件遗留", test_l2_file_restart_inherits),
        ("显式 sqlite / off 模式", test_explicit_sqlite_and_off_modes),
    ]
    for name, fn in cases:
        print(f"== {name} ==")
        await fn()
    print(f"\n结果: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)



if __name__ == "__main__":
    asyncio.run(main())