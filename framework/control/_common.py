"""控制通道两端共用的路径、进程与回环判定（只用标准库）。"""
import os

from framework.paths import state_dir as default_state_dir

CONTROL_FILE = 'control.json'
HOST = '127.0.0.1'
LOOPBACK_HOSTS = frozenset(('127.0.0.1', '::1'))
CONTROL_VERSION = 1


def control_path(state_dir=None):
    return os.path.join(state_dir or default_state_dir(), CONTROL_FILE)


def is_loopback(host):
    return host in LOOPBACK_HOSTS


def _windows_pid_alive(pid):
    # os.kill(pid, 0) 在 Windows 上会调用 TerminateProcess 结束目标进程，
    # 因此改用 OpenProcess 查询退出码。259 = STILL_ACTIVE。
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == 'nt':
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def load_info(path):
    """读取 control.json；格式错误返回 None，不校验 pid 与 host。"""
    import json

    try:
        with open(path, 'r', encoding='utf-8') as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None
