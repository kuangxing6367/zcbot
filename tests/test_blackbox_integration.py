# -*- coding: utf-8 -*-
"""黑匣子 · 事件流 + 加载期 ctx 调用 真实集成验证。

真实启动 Framework，验证：
  1. 加载期「插件注册时调了哪些 ctx 函数」（plugin_loaded 摘要 + load_ctx 详情）真实落黑匣子；
  2. 一条命令消息的完整事件流链路（ingest→…→done）可被单 event_id 拉出，且
     链路里能看到：候选插件名单、命中谁、是否等待、准备发给谁、卡在第几步；
  3. 未命中命令的链路能走到 unmatched + broadcast，并列出订阅 message 事件的插件；
  4. stuck_at 在事件正常完成时返回 None（未卡住）。

运行：python tests/test_blackbox_integration.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.core.base import Framework
from framework.blackbox import (get_event_trace, stuck_at, get_frames,
                                clear, configure)

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


async def main():
    print("== 黑匣子事件流集成验证 ==")
    # 放大黑匣子缓冲，避免启动期大量注册帧把最早加载的 echo/help 帧挤出环形缓冲。
    configure(max_bytes=64 * 1024 * 1024)
    fw = Framework()
    await fw.start(wait_ready=True)
    print("  [INFO] 框架已启动")

    # ── 1. 加载期 ctx 调用日志（plugin_loaded / load_ctx）──
    # echo/help 是核心插件，其注册经 _load_core_plugins，已在启动期把加载期 ctx 调用
    # 写入黑匣子；直接查启动期产生的帧即可（无需重新登记）。
    allf = get_frames()
    echo_frames = [f for f in allf if f['plugin'] == 'echo']
    pl_echo = [f for f in echo_frames if f['stage'] == 'plugin_loaded']
    lc_echo = [f for f in echo_frames if f['stage'] == 'load_ctx']
    check("echo 有 plugin_loaded 摘要帧", bool(pl_echo),
          [f['msg'] for f in pl_echo])
    check("echo 有 load_ctx 详情帧（加载期 ctx 调用）", bool(lc_echo),
          [f['msg'] for f in lc_echo[:5]])
    check("echo 的 load_ctx 含 command 注册",
          any('command' == f['fn'] for f in lc_echo),
          [f['msg'] for f in lc_echo[:5]])

    help_frames = [f for f in allf if f['plugin'] == 'help']
    lc_help = [f for f in help_frames if f['stage'] == 'load_ctx']
    check("help 有 load_ctx 详情帧", bool(lc_help),
          [f['msg'] for f in lc_help[:5]])

    # ── 2. 真实分发：/echo 你好 ──
    clear()  # 只看这条消息的链路
    ev = {"type": "message", "message_type": "private", "user_id": 10001,
          "group_id": None, "message": "/echo 你好", "adapter": "onebot",
          "bot_name": "default"}
    await asyncio.wait_for(fw.dispatch_event(ev, wait=True), timeout=15)
    eid = ev.get('_bb_eid')
    trace = get_event_trace(eid)
    print(f"  [INFO] /echo 你好 链路 {len(trace)} 帧:")
    for f in trace:
        print(f"    [{f['step'] if f['step'] is not None else '?':>2}] {f['stage']:<14} "
              f"{f['plugin'] or '-':<10} {f['msg']} "
              f"(to={f['to'] or '-'} subs={f['subs'] or '-'})")
    stages = [f['stage'] for f in trace]
    check("链路含 ingest", 'ingest' in stages)
    check("链路含 enqueue", 'enqueue' in stages)
    check("链路含 dequeue", 'dequeue' in stages)
    check("链路含 route_start", 'route_start' in stages)
    check("链路含 plugin_hit（命中 echo）",
          'plugin_hit' in stages and any(f['plugin'] == 'echo' for f in trace))
    check("链路含 handler_enter/exit",
          'handler_enter' in stages and 'handler_exit' in stages)
    check("链路含 send（准备发给谁）", 'send' in stages)
    check("链路含 done", 'done' in stages)
    check("stuck_at 为 None（已正常完成）", stuck_at(eid) is None)
    rs = [f for f in trace if f['stage'] == 'route_start']
    check("route_start 记录候选插件名单",
          bool(rs) and ',' in (rs[0]['subs'] or ''), rs[0]['subs'] if rs else '')
    enq = [f for f in trace if f['stage'] == 'enqueue']
    check("enqueue 记录是否在等待（waiting 字段）",
          bool(enq) and enq[0]['waiting'] in ('0', '1'),
          enq[0]['waiting'] if enq else '')

    # ── 3. 真实分发：未命中命令 ──
    clear()
    ev2 = {"type": "message", "message_type": "private", "user_id": 10001,
           "group_id": None, "message": "/zzz_no_such_cmd_xyz",
           "adapter": "onebot", "bot_name": "default"}
    await asyncio.wait_for(fw.dispatch_event(ev2, wait=True), timeout=15)
    eid2 = ev2.get('_bb_eid')
    trace2 = get_event_trace(eid2)
    print(f"  [INFO] 未命中命令 链路 {len(trace2)} 帧:")
    for f in trace2:
        print(f"    [{f['step'] if f['step'] is not None else '?':>2}] {f['stage']:<14} "
              f"{f['plugin'] or '-':<10} {f['msg']} "
              f"(subs={f['subs'] or '-'})")
    stages2 = [f['stage'] for f in trace2]
    check("未命中链路含 unmatched", 'unmatched' in stages2)
    check("未命中链路含 broadcast（订阅者名单）", 'broadcast' in stages2)
    bc = [f for f in trace2 if f['stage'] == 'broadcast']
    check("broadcast 记录订阅 message 事件的插件",
          bool(bc), bc[0]['subs'] if bc else '')
    check("未命中链路含 done", 'done' in stages2)
    check("未命中 stuck_at 为 None", stuck_at(eid2) is None)

    await fw.stop()
    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == '__main__':
    code = 0
    try:
        code = asyncio.run(main()) or 0
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
