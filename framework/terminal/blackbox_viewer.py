"""内存黑匣子 TTY 查看器

终端命令 `debug` 进入：在运行时 TTY 浏览内存黑匣子现场（事件流转 / 卡步诊断）。
黑匣子为纯内存环形缓冲，进程退出即失；本查看器读取当前进程内的实时现场。

子命令：
  help                显示帮助
  stats               黑匣子统计（容量 / 帧数 / 丢弃 / 事件数）
  recent [n=30]       最近事件列表 + 各自「卡在第几步」摘要
  stuck [n=30]        仅列出未走完（卡住）的事件
  last [n=50]         最近 n 帧原始记录
  trace <event_id>    某事件的完整事件流链路（按时间排序，含每步 step 编号）
  clear               清空内存黑匣子
  exit | back | q     退出查看器，回到终端主命令循环

设计约束（沿用 framework/terminal 约定）：
- 由终端命令 `debug` 经 input.py 的 `to_thread` 调用，阻塞至退出；
  期间主输入循环停在 `run_coroutine_threadsafe(...).result()`，不与之抢 stdin。
- 非 TTY / terminal.enabled:false 时终端输入线程根本不启动，本查看器无从进入
  （黑匣子仍是 WebUI 调试接口兜底），符合「不向非交互环境强加 TTY 依赖」。
"""
import builtins
import sys
import time

from framework.blackbox import (
    STAGE_STEP,
    clear,
    get_event_trace,
    get_frames,
    recent_event_ids,
    stats,
    stuck_at,
)

_PROMPT = "blackbox> "
_HELP = """黑匣子查看器命令:
  stats               统计（容量 / 帧数 / 事件数 / 丢弃）
  recent [n=30]       最近事件 + 各自卡步摘要
  stuck [n=30]        仅列出未走完（卡住）的事件
  last [n=50]         最近 n 帧原始记录
  trace <event_id>    某事件完整事件流链路 + 卡步诊断
  clear               清空内存黑匣子
  exit | back | q     退出查看器
说明: 黑匣子为纯内存环形缓冲，进程退出即失；trace 报「未找到」多为缓冲淘汰。"""


def _ts(t):
    try:
        return time.strftime('%H:%M:%S', time.localtime(t))
    except Exception:
        return '??:??:??'


def _step_name(step):
    inv = {v: k for k, v in STAGE_STEP.items()}
    return inv.get(step, str(step))


def _print_frame(f, idx=None):
    ts = _ts(f.get('t', 0))
    step = f.get('step')
    stage = f.get('stage', '')
    plugin = f.get('plugin', '')
    eid = f.get('eid', '')
    msg = f.get('msg', '')
    to = f.get('to', '')
    subs = f.get('subs', '')
    prefix = f"  [{idx}] " if idx is not None else "  "
    s = f"{prefix}[{ts}] #{step} {stage}"
    if plugin:
        s += f" <{plugin}>"
    if eid:
        s += f" ({eid})"
    if to:
        s += f" ->{to}"
    if subs:
        s += f" subs={subs}"
    if msg:
        s += f" :: {msg}"
    print(s)


def _cmd_stats():
    st = stats()
    print("=== 黑匣子统计 ===")
    print(f"  启用: {st.get('enabled')}")
    print(f"  容量: {st.get('used_bytes')} / {st.get('max_bytes')} 字节")
    print(f"  帧数: {st.get('frames')}  事件数: {st.get('events')}")
    print(f"  丢弃帧: {st.get('dropped')}  自增序号: {st.get('seq')}")


def _cmd_recent(n, only_stuck=False):
    ids = recent_event_ids(limit=n)
    if not ids:
        print("（无事件记录）")
        return
    print(f"=== 最近事件（最多 {n}，{'仅卡住' if only_stuck else '全部'}）===")
    shown = 0
    for eid in ids:
        s = stuck_at(eid)
        if only_stuck and s is None:
            continue
        if s is None:
            status = "已完成"
        else:
            status = f"卡在 #{s.get('step')} {s.get('stage')}"
            if s.get('plugin'):
                status += f" <{s.get('plugin')}>"
        steps = len(get_event_trace(eid))
        print(f"  {eid}: {status}  （链路 {steps} 帧）")
        shown += 1
    if shown == 0:
        print("  （无卡住的事件）")


def _cmd_last(n):
    frames = get_frames(limit=n)
    if not frames:
        print("（无记录）")
        return
    print(f"=== 最近 {len(frames)} 帧 ===")
    for i, f in enumerate(frames):
        _print_frame(f, idx=i + 1)


def _cmd_trace(eid):
    if not eid:
        print("用法: trace <event_id>，如: trace e42")
        return
    trace = get_event_trace(eid)
    if not trace:
        print(f"未找到事件 {eid}（可能已被环形缓冲淘汰）")
        return
    print(f"=== 事件 {eid} 完整链路（{len(trace)} 帧）===")
    for f in trace:
        _print_frame(f)
    s = stuck_at(eid)
    print("---")
    if s is None:
        print("状态: 已完成（已抵达 DONE）")
    else:
        print(f"状态: 卡在第 {s.get('step')} 步 [{s.get('stage')}]"
              + (f" <{s.get('plugin')}>" if s.get('plugin') else ""))


def _parse_int(s, default):
    try:
        return max(1, int(s.strip()))
    except Exception:
        return default


def run_blackbox_viewer(framework=None):
    """进入黑匣子交互查看器（阻塞至 exit）。由终端命令 `debug` 调用。

    framework 参数保留以兼容调用约定（当前直接读全局黑匣子单例）。
    """
    print("=== 内存黑匣子查看器（输入 help 查看命令，exit 退出）===")
    try:
        while True:
            try:
                line = builtins.input(_PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            raw = line.strip()
            if not raw:
                continue
            parts = raw.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            if cmd in ('exit', 'back', 'q'):
                break
            elif cmd in ('help', '?'):
                print(_HELP)
            elif cmd == 'stats':
                _cmd_stats()
            elif cmd == 'recent':
                _cmd_recent(_parse_int(arg, 30), only_stuck=False)
            elif cmd == 'stuck':
                _cmd_recent(_parse_int(arg, 30), only_stuck=True)
            elif cmd in ('last', 'frames'):
                _cmd_last(_parse_int(arg, 50))
            elif cmd == 'trace':
                _cmd_trace(arg.strip())
            elif cmd == 'clear':
                clear()
                print("已清空内存黑匣子。")
            else:
                print(f"未知子命令: {cmd}（输入 help 查看）")
    except (ValueError, OSError):
        # stdin 被拔（外部杀进程瞬间），安静退出，避免刷屏
        pass
    print("已退出黑匣子查看器，回到终端主命令循环。")


if __name__ == '__main__':
    run_blackbox_viewer()
