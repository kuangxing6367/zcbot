"""debug 命令组：连接运行实例的本地控制通道查看黑匣子。"""
import json
import sys
import time

from framework.blackbox_format import (
    format_event,
    format_frame,
    format_stats,
    format_trace,
)
from framework.control._common import control_path
from framework.control.client import (
    NO_INSTANCE_MESSAGE,
    Client,
    ControlError,
    NoRunningInstance,
)


def _print_recent(events, limit, only_stuck):
    print(f"=== 最近事件（最多 {limit}，{'仅卡住' if only_stuck else '全部'}）===")
    if not events:
        print("（无事件记录）" if not only_stuck else "  （无卡住的事件）")
        return
    for e in events:
        print(format_event(e['event_id'], e.get('stuck_at'), e.get('steps', 0)))


def _print_frames(frames):
    if not frames:
        print("（无记录）")
        return
    for i, f in enumerate(frames):
        print(format_frame(f, idx=i + 1))


def _follow(client, args):
    since = time.time()
    last_seq = 0
    initial = client.frames(limit=200)['frames']
    if initial:
        last_seq = max(f.get('seq', 0) for f in initial)
        since = max(f.get('t', since) for f in initial)
    try:
        while True:
            frames = client.frames(since=since, limit=200)['frames']
            fresh = [f for f in frames if f.get('seq', 0) > last_seq]
            for f in fresh:
                if args.json:
                    print(json.dumps({'stats': client.stats(), 'frame': f},
                                     ensure_ascii=False))
                else:
                    print(format_frame(f))
                last_seq = max(last_seq, f.get('seq', 0))
                since = max(since, f.get('t', since))
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止跟随。")
    return 0


def do_debug(args, working_dir):
    """debug 组入口；返回进程退出码。"""
    if getattr(args, 'action', None) != 'blackbox':
        print("用法: debug blackbox [选项]")
        return 2
    try:
        client = Client.connect()
    except NoRunningInstance:
        print(NO_INSTANCE_MESSAGE)
        print(f'查找位置: {control_path()}')
        return 3
    except ControlError as exc:
        print(str(exc))
        return 3

    if args.follow:
        return _follow(client, args)

    try:
        if args.clear:
            if not args.yes:
                print("清空需要 --yes 确认")
                return 2
            client.clear(confirm=True)
            print("已清空内存黑匣子。")
            return 0

        stats = client.stats()
        if args.trace:
            trace = client.trace(args.trace)
            if args.json:
                print(json.dumps({'stats': stats, 'trace': trace},
                                 ensure_ascii=False, indent=2))
            elif not trace['frames']:
                print(f"未找到事件 {args.trace}（可能已被环形缓冲淘汰）")
            else:
                print(format_trace(args.trace, trace['frames'], trace['stuck_at']))
            return 0

        if args.last is not None:
            frames = client.frames(limit=max(1, args.last))['frames']
            if args.json:
                print(json.dumps({'stats': stats, 'frames': frames},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"=== 最近 {len(frames)} 帧 ===")
                _print_frames(frames)
            return 0

        recent = client.recent(limit=30)
        if args.stuck:
            recent['events'] = [e for e in recent['events'] if e.get('stuck_at')]
        if args.json:
            print(json.dumps({'stats': stats, 'recent': recent},
                             ensure_ascii=False, indent=2))
        else:
            print(format_stats(stats))
            print()
            _print_recent(recent['events'], 30, args.stuck)
        return 0
    except ControlError as exc:
        print(str(exc))
        return 1
