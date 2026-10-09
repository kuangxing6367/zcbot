# -*- coding: utf-8 -*-
"""终端命令的「执行上下文」标记。

有些命令需要真正的交互式 TTY（全屏界面：`code`、内置 `tui` 面板）。当命令是
经**远程通道**触发的——本地调试控制台（`python main.py attach`）或 HTTP
`/api/terminal/exec`——时，进程侧根本没有可用的终端，去启动全屏界面只会得到
Windows 的 `0xC0000142 (STATUS_DLL_INIT_FAILED)` 之类的失败。

这里用一个线程局部标记让命令处理器能识别「我是被远程调用的」，从而给出明确
指引而不是盲目 spawn 子进程。

用法：
    from framework.terminal.context import remote_session, is_remote_session

    with remote_session():
        handler(args)            # 期间 is_remote_session() 为 True
"""
import threading

_local = threading.local()


class remote_session:
    """上下文管理器：标记「当前线程正在为远程通道执行终端命令」。"""

    def __enter__(self):
        self._prev = getattr(_local, 'remote', False)
        _local.remote = True
        return self

    def __exit__(self, *exc):
        _local.remote = self._prev
        return False


def is_remote_session() -> bool:
    """当前线程是否正在为远程通道执行命令。"""
    return bool(getattr(_local, 'remote', False))
