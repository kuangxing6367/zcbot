# -*- coding: utf-8 -*-
"""
黑匣子 · 极端运行时 & 异常/恶意输入 集成验证

覆盖用户点的两类「刁钻/变态环境」：
  A. 极端运行时：插件崩溃不连锁崩、命令 handler 超时熔断、缓冲溢出/背压水位可见、
     并发事件链路隔离、适配器宕机时发送失败可诊断。
  B. 异常/恶意输入：畸形事件、超大消息、缺字段事件、吞异常插件 —— 框架不崩、黑匣子可追。

全部断言黑匣子（get_event_trace / stuck_at）能给出「卡在第几步 / 谁炸了 / 发给谁失败」。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

from framework.core.base import Framework
from framework.blackbox import (get_event_trace, stuck_at, get_frames, clear,
                                stats, configure)
from framework.core.event_buffer import EventBuffer
from framework.config import get_home

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


async def inject_command(fw, plugin_name, pattern, handler_name, handler_fn, priority=10):
    """往已加载插件的模块上挂一个命令处理函数，并写入 commands 表 + 重建路由。
    返回该函数名，便于清理。"""
    mod = fw.plugin_loader.get_plugin_module(plugin_name)
    setattr(mod, handler_name, handler_fn)
    fw.db.execute(
        "INSERT INTO commands (plugin_name, pattern, handler, is_active, "
        "is_dynamic, require_level, priority) VALUES (%s, %s, %s, 1, 0, '', %s)",
        (plugin_name, pattern, handler_name, priority))
    fw.router._force_refresh = True
    await asyncio.to_thread(fw.router._rebuild_routes)
    return handler_name


async def cleanup_commands(fw, like_pattern):
    fw.db.execute("DELETE FROM commands WHERE pattern LIKE %s", (like_pattern,))
    fw.router._force_refresh = True
    await asyncio.to_thread(fw.router._rebuild_routes)


def frame_blob(f):
    return f"{f['msg']}|{f['to']}|{f['subs']}|{f['detail']}"


async def main():
    print("== 黑匣子 · 极端运行时 & 异常输入 集成验证 ==")
    fw = Framework()
    # 放大黑匣子预算，避免启动期帧被裁剪干扰断言
    configure(max_bytes=64 * 1024 * 1024)
    await fw.start(wait_ready=True)
    print("  [INFO] 框架已启动")

    # ─────────────────────────────────────────────────────────
    # 场景 1：插件崩溃 —— 单插件炸了不能拖垮管线（其它事件仍正常完成）
    # ─────────────────────────────────────────────────────────
    async def _h_crash(ev, m):
        raise RuntimeError("插件自己炸了 boom")

    await inject_command(fw, 'echo', '/__extreme_crash', '__extreme_crash', _h_crash)
    clear()
    ev_crash = {"type": "message", "message_type": "private", "user_id": 10001,
                "group_id": None, "message": "/__extreme_crash", "bot_name": "default"}
    ev_ok = {"type": "message", "message_type": "private", "user_id": 10002,
             "group_id": None, "message": "/echo 正常", "bot_name": "default"}
    await asyncio.gather(
        asyncio.wait_for(fw.dispatch_event(ev_crash, wait=True), timeout=15),
        asyncio.wait_for(fw.dispatch_event(ev_ok, wait=True), timeout=15),
    )
    ec = ev_crash.get('_bb_eid')
    eo = ev_ok.get('_bb_eid')
    tc = get_event_trace(ec)
    to = get_event_trace(eo)
    stages_c = {f['stage'] for f in tc}
    stages_o = {f['stage'] for f in to}
    print(f"  [INFO] 崩溃事件链路 {len(tc)} 帧，正常事件链路 {len(to)} 帧")
    check("崩溃事件仍有 handler_enter", 'handler_enter' in stages_c)
    check("崩溃事件 handler_exit 标注异常已交 on_error",
          any(f['stage'] == 'handler_exit' and '异常' in f['msg'] for f in tc))
    check("崩溃事件仍抵达 done（未卡死）", 'done' in stages_c)
    check("崩溃事件 stuck_at 为 None", stuck_at(ec) is None)
    check("正常事件在崩溃旁仍完成（无连锁崩）",
          'done' in stages_o and 'plugin_hit' in stages_o)
    check("正常事件 stuck_at 为 None", stuck_at(eo) is None)
    await cleanup_commands(fw, '/__extreme_crash')

    # ─────────────────────────────────────────────────────────
    # 场景 2：命令 handler 超时熔断 —— 恶意/死循环插件不能冻结整条管线
    # ─────────────────────────────────────────────────────────
    fw.config.setdefault('framework', {})['command_handler_timeout'] = 1.0

    async def _h_hang(ev, m):
        # 典型「刁钻」插件：await 死循环，无超时则冻结事件循环
        while True:
            await asyncio.sleep(0.05)

    await inject_command(fw, 'echo', '/__extreme_hang', '__extreme_hang', _h_hang)
    clear()
    ev_hang = {"type": "message", "message_type": "private", "user_id": 10003,
               "group_id": None, "message": "/__extreme_hang", "bot_name": "default"}
    try:
        await asyncio.wait_for(fw.dispatch_event(ev_hang, wait=True), timeout=8)
        hung = False
    except asyncio.TimeoutError:
        hung = True
    eh = ev_hang.get('_bb_eid')
    th = get_event_trace(eh)
    stages_h = {f['stage'] for f in th}
    check("死循环 handler 未被无限卡住（超时熔断生效，测试未超时）", not hung)
    check("超时事件 handler_exit 标注『超时已熔断』",
          any(f['stage'] == 'handler_exit' and '超时' in f['msg'] for f in th))
    check("超时事件仍抵达 done", 'done' in stages_h)
    check("超时事件 stuck_at 为 None（已熔断收尾）", stuck_at(eh) is None)
    # 熔断后，紧接着一条正常消息必须仍能处理（证明管线没被冻住）
    clear()
    ev_after = {"type": "message", "message_type": "private", "user_id": 10004,
                "group_id": None, "message": "/echo 熔断后", "bot_name": "default"}
    await asyncio.wait_for(fw.dispatch_event(ev_after, wait=True), timeout=15)
    ea = ev_after.get('_bb_eid')
    ta = get_event_trace(ea)
    check("超时熔断后管线仍可处理新消息", 'done' in {f['stage'] for f in ta})
    await cleanup_commands(fw, '/__extreme_hang')

    # ─────────────────────────────────────────────────────────
    # 场景 3：适配器宕机 / 无可用接入端 —— 发送失败黑匣子可诊断
    # ─────────────────────────────────────────────────────────
    def _h_sendfail(ev, m):
        # 无适配器环境下调用 ctx.send_msg 必失败，验证 send_fail 记录
        _SEND_CTX.send_msg(user_id=ev.user_id, message="本应发出但没适配器")
        return True

    # 用闭包把 echo 的 ctx 带进 handler（避免依赖 ev 上的属性）
    _SEND_CTX = fw.plugin_loader.get_plugin_module('echo').ctx

    await inject_command(fw, 'echo', '/__extreme_sendfail', '__extreme_sendfail', _h_sendfail)
    clear()
    ev_sf = {"type": "message", "message_type": "private", "user_id": 10005,
             "group_id": None, "message": "/__extreme_sendfail", "bot_name": "default"}
    await asyncio.wait_for(fw.dispatch_event(ev_sf, wait=True), timeout=15)
    es = ev_sf.get('_bb_eid')
    ts = get_event_trace(es)
    stages_s = {f['stage'] for f in ts}
    check("发送失败事件记录 send（准备发）", 'send' in stages_s)
    check("发送失败事件记录 send_fail（发给谁但失败）", 'send_fail' in stages_s)
    sf = [f for f in ts if f['stage'] == 'send_fail']
    check("send_fail 标注失败目标", bool(sf) and sf[0]['to'].startswith('私聊'),
          sf[0]['to'] if sf else '')
    check("发送失败事件仍抵达 done（未因发送异常卡死）", 'done' in stages_s)
    await cleanup_commands(fw, '/__extreme_sendfail')

    # ─────────────────────────────────────────────────────────
    # 场景 4：吞异常的插件 —— 内部 try/except 吃掉错误，黑匣子看到干净收尾
    # ─────────────────────────────────────────────────────────
    def _h_swallow(ev, m):
        try:
            1 / 0
        except Exception:
            pass  # 恶意/劣质插件把错误吞掉
        return None

    await inject_command(fw, 'echo', '/__extreme_swallow', '__extreme_swallow', _h_swallow)
    clear()
    ev_sw = {"type": "message", "message_type": "private", "user_id": 10006,
             "group_id": None, "message": "/__extreme_swallow", "bot_name": "default"}
    await asyncio.wait_for(fw.dispatch_event(ev_sw, wait=True), timeout=15)
    ew = ev_sw.get('_bb_eid')
    tw = get_event_trace(ew)
    check("吞异常插件 handler_exit 标注『执行完毕』（未被误判为崩溃）",
          any(f['stage'] == 'handler_exit' and '执行完毕' in f['msg'] for f in tw))
    check("吞异常事件仍抵达 done", 'done' in {f['stage'] for f in tw})
    await cleanup_commands(fw, '/__extreme_swallow')

    # ─────────────────────────────────────────────────────────
    # 场景 5：畸形事件（缺 type / 怪字段）—— 归一化容错，不崩、链路有记录
    # ─────────────────────────────────────────────────────────
    clear()
    ev_bad = {"foo": "bar", "weird": [1, 2, 3]}
    await asyncio.wait_for(fw.dispatch_event(ev_bad, wait=True), timeout=15)
    eb = ev_bad.get('_bb_eid')
    tb = get_event_trace(eb)
    check("畸形事件被 ingest 记录", any(f['stage'] == 'ingest' for f in tb))
    check("畸形事件被 classify 记录", any(f['stage'] == 'classify' for f in tb))
    check("畸形事件未引发未处理异常（dispatch 正常返回，管线继续）",
          bool(tb) and any(f['stage'] == 'ingest' for f in tb)
          and any(f['stage'] == 'classify' for f in tb))

    # ─────────────────────────────────────────────────────────
    # 场景 6：缺字段事件（无 user/group/message）—— 不崩，默认填充
    # ─────────────────────────────────────────────────────────
    clear()
    ev_missing = {"type": "message"}
    await asyncio.wait_for(fw.dispatch_event(ev_missing, wait=True), timeout=15)
    em = ev_missing.get('_bb_eid')
    tm = get_event_trace(em)
    check("缺字段事件被 ingest + classify",
          any(f['stage'] == 'ingest' for f in tm)
          and any(f['stage'] == 'classify' for f in tm))
    check("缺字段事件未崩（dispatch 正常返回，归一化补齐默认字段）",
          bool(tm) and any(f['stage'] == 'ingest' for f in tm))

    # ─────────────────────────────────────────────────────────
    # 场景 7：超大消息（2MB）—— 黑匣子只存截断预览，不爆内存预算
    # ─────────────────────────────────────────────────────────
    clear()
    big = "x" * (2 * 1024 * 1024)
    ev_big = {"type": "message", "message_type": "private", "user_id": 10007,
              "group_id": None, "message": "/echo " + big, "bot_name": "default"}
    await asyncio.wait_for(fw.dispatch_event(ev_big, wait=True), timeout=15)
    ebig = ev_big.get('_bb_eid')
    tbig = get_event_trace(ebig)
    check("超大消息被 ingest + classify",
          any(f['stage'] == 'ingest' for f in tbig)
          and any(f['stage'] == 'classify' for f in tbig))
    # 任何一帧都不应包含 100 个连续的 'x'（证明内容未整段入库）
    leaked = any('x' * 100 in frame_blob(f) for f in tbig)
    check("超大消息内容未整段落入黑匣子帧", not leaked)
    check("黑匣子内存预算未被超大消息撑爆（<5MB）",
          stats()['used_bytes'] < 5 * 1024 * 1024,
          stats()['used_bytes'])

    # ─────────────────────────────────────────────────────────
    # 场景 8：并发事件链路隔离 —— 每条事件独立 eid，互不串台
    # ─────────────────────────────────────────────────────────
    clear()
    payloads = [f"/echo 并发{i}" for i in range(5)]
    evs = [{"type": "message", "message_type": "private", "user_id": 20000 + i,
            "group_id": None, "message": p, "bot_name": "default"} for i, p in enumerate(payloads)]
    await asyncio.gather(*[asyncio.wait_for(fw.dispatch_event(e, wait=True), timeout=20)
                          for e in evs])
    all_ok = True
    for e, p in zip(evs, payloads):
        eid = e.get('_bb_eid')
        tr = get_event_trace(eid)
        # 链路内不应出现别的事件帧
        cross = any(f['eid'] != eid for f in tr)
        hit = [f for f in tr if f['stage'] == 'plugin_hit']
        hit_ok = any(p in f['msg'] for f in hit)
        if cross or not hit_ok or stuck_at(eid) is not None:
            all_ok = False
        print(f"    {p}: 帧={len(tr)} 命中={hit_ok} 串台={cross} stuck={stuck_at(eid) is None}")
    check("5 条并发事件各自链路独立、命中正确、无卡死", all_ok)

    await fw.stop()

    # ─────────────────────────────────────────────────────────
    # 场景 9：缓冲溢出 / 背压水位可见（独立 Framework，缓冲调小）
    # ─────────────────────────────────────────────────────────
    print("  [INFO] 启动溢出测试用框架（缓冲调小）")
    fw2 = Framework()
    configure(max_bytes=64 * 1024 * 1024)
    # L1 限 1 条（maxsize=1）、L3/L4 留足不丢 → 第 2 条起 L1 满、enqueue 必记 waiting=1
    fw2._event_buffer = EventBuffer(
        {'maxsize': 1, 'buffer': {'l1_max_bytes': 512 * 1024,
                                  'l3_max_bytes': 4 * 1024 * 1024,
                                  'l4_max_bytes': 4 * 1024 * 1024,
                                  'l2_mode': 'off'}},
        os.path.join(get_home(), 'data'))
    await fw2.start(wait_ready=True)
    clear()
    n = 30
    for i in range(n):
        e = {"type": "message", "message_type": "private", "user_id": 30000 + i,
             "group_id": None, "message": f"/echo 洪{i}", "bot_name": "default"}
        await fw2.dispatch_event(e, wait=False)  # 不等，制造入队压力
    await asyncio.sleep(1.0)  # 给 worker 时间消化 + 落黑匣子
    enq_frames = [f for f in get_frames() if f['stage'] == 'enqueue']
    waiting_seen = any(f['waiting'] == '1' for f in enq_frames)
    ingest_cnt = sum(1 for f in get_frames() if f['stage'] == 'ingest')
    print(f"  [INFO] 入队帧={len(enq_frames)} waiting_seen={waiting_seen} "
          f"ingest帧={ingest_cnt}")
    check("溢出/背压：黑匣子 enqueue 记录『是否在等待(waiting=1)』", waiting_seen)
    check("溢出/背压：入队时 ingest 帧未丢失（全部记录）", ingest_cnt == n, ingest_cnt)

    # 9b：缓冲层真实丢弃（保老弃新）—— 直接验证，与黑匣子 enqueue 的 drop 计数同源
    tiny = EventBuffer(
        {'buffer': {'l1_max_bytes': 1, 'l3_max_bytes': 1,
                    'l4_max_bytes': 1, 'l2_backend': 'off'}},
        os.path.join(get_home(), 'data'))
    for i in range(5):
        await tiny.put({'seq': i}, None)  # 每条均 >1B，L1/L3/L4 全装不下 → 丢弃
    check("缓冲溢出真实丢弃（drop>0，保老弃新）", tiny.stats()['dropped'] >= 1,
          tiny.stats()['dropped'])
    await fw2.stop()

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
