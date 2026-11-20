"""黑匣子帧与链路的文本格式化。

终端查看器与 CLI 共用这里的函数，避免两处各写一套输出口径。
本模块只格式化传入的数据，不读黑匣子、不打印。
"""
import time

from framework.blackbox import STAGE_STEP

__all__ = [
    'format_event',
    'format_frame',
    'format_stats',
    'format_step_name',
    'format_trace',
    'format_ts',
]


def format_ts(t):
    try:
        return time.strftime('%H:%M:%S', time.localtime(t))
    except Exception:
        return '??:??:??'


def format_step_name(step):
    inv = {v: k for k, v in STAGE_STEP.items()}
    return inv.get(step, str(step))


def format_frame(f, idx=None):
    ts = format_ts(f.get('t', 0))
    step = f.get('step')
    stage = f.get('stage', '')
    plugin = f.get('plugin', '')
    eid = f.get('eid', '')
    msg = f.get('msg', '')
    to = f.get('to', '')
    subs = f.get('subs', '')
    prefix = f"  [{idx}] " if idx is not None else "  "
    # 框架未标注 step 的帧（例如插件自己写的 dispatch）用 "-" 占位，避免输出 #None。
    step_text = f"#{step}" if step is not None else "#-"
    s = f"{prefix}[{ts}] {step_text} {stage}"
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
    return s


def format_stats(st):
    return '\n'.join([
        "=== 黑匣子统计 ===",
        f"  启用: {st.get('enabled')}",
        f"  容量: {st.get('used_bytes')} / {st.get('max_bytes')} 字节",
        f"  帧数: {st.get('frames')}  事件数: {st.get('events')}",
        f"  丢弃帧: {st.get('dropped')}  自增序号: {st.get('seq')}",
    ])


def format_event(event_id, stuck=None, steps=0):
    if stuck is None:
        status = "已完成"
    else:
        status = f"卡在 #{stuck.get('step')} {stuck.get('stage')}"
        if stuck.get('plugin'):
            status += f" <{stuck.get('plugin')}>"
    return f"  {event_id}: {status}  （链路 {steps} 帧）"


def format_trace(event_id, frames, stuck=None):
    lines = [f"=== 事件 {event_id} 完整链路（{len(frames)} 帧）==="]
    lines.extend(format_frame(f) for f in frames)
    lines.append('---')
    if stuck is None:
        lines.append("状态: 已完成（已抵达 DONE）")
    else:
        line = f"状态: 卡在第 {stuck.get('step')} 步 [{stuck.get('stage')}]"
        if stuck.get('plugin'):
            line += f" <{stuck.get('plugin')}>"
        lines.append(line)
    return '\n'.join(lines)
