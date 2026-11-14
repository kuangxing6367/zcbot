"""单实例文件锁：防止守护进程无脑拉起导致多实例（抢端口 / 抢 DB / 双写）。

跨平台：Linux 用 fcntl.flock（内核级 advisory lock，进程退出自动释放）；
Windows 用 msvcrt.locking。拿到锁则持有 fd 直到进程退出（fd 关闭即释放）。

退出时由 cleanup_instance_lock()（atexit + finally 双保险）关闭 fd 并删除锁文件，
避免残留锁文件干扰运维判断。若进程被强杀导致锁文件残留，下次启动会通过 PID 探活
识别 stale 锁并清理后重新获取，不会误判「已在运行」。
"""
import logging
import os
import sys

logger = logging.getLogger('zcbot')

# 锁字节与 PID 字节错开：锁打在偏移 4096，PID 写在偏移 0，避免 Windows 下
# 持锁句柄锁住第 0 字节导致第二个实例读取 PID 时读到「PID 未知」。
LOCK_BYTE_OFFSET = 4096

# 单实例锁全局句柄（供退出清理）
_INSTANCE_LOCK = {'path': None, 'fd': None}


def instance_lock_path(root):
    # 锁文件属于「运行状态」，必须落在可写状态目录。调用方传的是代码根，
    # pip 安装时那是 site-packages（只读）、容器只读挂载同理 —— 旧实现在
    # 那种环境下必然创建失败，进而被误判成「已有实例在跑」而拒绝启动。
    try:
        from framework.config import get_home
        home = get_home()
        if home and os.access(home, os.W_OK):
            return os.path.join(home, 'data', 'zcbot.lock')
    except Exception as _exc:
        logger.debug('home/data 不可写，回落到系统临时目录: %s', _exc)
    return os.path.join(root, 'data', 'zcbot.lock')


def open_lock_file(lock_path):
    """跨平台打开锁文件。

    Windows 下以「读+写共享」(FILE_SHARE_READ|FILE_SHARE_WRITE) 打开，使第二个实例也能
    打开并读取记录的 PID，从而打印明确的「已有实例」提示，而不是因独占共享导致 os.open
    直接抛共享冲突、静默退出（这正是早期版本在 Windows 上「发现锁住却啥都不打印」的根因）。
    POSIX 下直接 os.open（fcntl.flock 是整文件 advisory lock，且不影响读）。
    """
    if sys.platform == 'win32':
        import ctypes
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_ALWAYS = 4  # 注意：Windows 里 3 是 OPEN_EXISTING（仅当文件已存在），4 才是 OPEN_ALWAYS（不存在则创建）
        # 用显式 c_uint32 类型对象传参，避免 ctypes 默认按有符号 c_int 转换 0xC0000000 这类高位值溢出
        h = kernel32.CreateFileW(
            ctypes.c_wchar_p(lock_path),
            ctypes.c_uint32(GENERIC_READ | GENERIC_WRITE),
            ctypes.c_uint32(FILE_SHARE_READ | FILE_SHARE_WRITE),
            None,
            ctypes.c_uint32(OPEN_ALWAYS),
            ctypes.c_uint32(0),
            None,
        )
        h_val = h.value if hasattr(h, 'value') else h
        INVALID = ctypes.c_void_p(-1).value  # 0xFFFFFFFFFFFFFFFF（INVALID_HANDLE_VALUE）
        if h_val in (None, 0, INVALID):
            raise OSError(f"CreateFileW 打开锁文件失败: {kernel32.GetLastError()}")
        try:
            # 必须带 O_BINARY：Windows CRT 的 open_osfhandle 对仅 O_RDWR 的句柄
            # 会报 [Errno 9] Bad file descriptor（缺少二进制模式标志）。
            return msvcrt.open_osfhandle(int(h_val), os.O_RDWR | os.O_BINARY)
        except Exception:
            kernel32.CloseHandle(ctypes.c_void_p(int(h_val)))
            raise
    return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)


def pid_is_our_instance(pid, root):
    """判断 pid 是否仍是本框架进程（避免 PID 复用误判为「已在运行」）。"""
    if pid is None or pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            PROCESS_QUERY_INFORMATION = 0x0400
            h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
            alive = bool(h)
            if h:
                kernel32.CloseHandle(h)
            return alive
        except Exception as _exc:
            logger.debug('查询进程存活失败，按不存活处理: %s', _exc)
            return False
    # POSIX：先确认进程存在，再比对 /proc/<pid>/cmdline 是否指向本 main.py
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except Exception as _exc:
        # 存在但无权限探活 -> 保守视为存活
        logger.debug('查询进程存活时进程已消失: %s', _exc)
        return True
    try:
        with open('/proc/%d/cmdline' % int(pid), 'rb') as f:
            cmd = f.read().decode('utf-8', 'replace')
        for seg in cmd.split('\x00'):
            if 'main.py' in seg or 'zcbot' in seg.lower():
                return True
        return False
    except (FileNotFoundError, PermissionError):
        return False


def read_lock_pid(fd):
    """从本进程已打开的锁 fd 读取记录的 PID（lseek+read，不涉及跨句柄共享，
    跨平台稳定；读不到或非数字返回 None，不阻断主流程）。
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 64)
        txt = data.decode('utf-8', 'replace').strip()
        return int(txt) if txt.isdigit() else None
    except Exception as _exc:
        logger.debug('读取锁文件 pid 失败: %s', _exc)
        return None


def write_lock_pid(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode('utf-8'))
    except Exception as _exc:
        logger.debug('写入锁文件 pid 失败: %s', _exc)


def try_lock(fd):
    if sys.platform == 'win32':
        import msvcrt
        try:
            os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def remove_stale_lock(lock_path, fd):
    try:
        if fd is not None:
            try:
                os.close(fd)
            except Exception as _exc:
                logger.debug('关闭锁文件描述符失败: %s', _exc)
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception as _exc:
        logger.debug('删除残留锁文件失败: %s', _exc)


def acquire_instance_lock(root):
    """单实例文件锁：防止守护进程无脑拉起导致多实例（抢端口 / 抢 DB / 双写）。

    跨平台：Linux 用 fcntl.flock（内核级 advisory lock，进程退出自动释放）；
    Windows 用 msvcrt.locking。拿到锁则持有 fd 直到进程退出（fd 关闭即释放）。

    退出时由 cleanup_instance_lock()（atexit + finally 双保险）关闭 fd 并删除锁文件，
    避免残留锁文件干扰运维判断。若进程被强杀导致锁文件残留，下次启动会通过 PID 探活
    识别 stale 锁并清理后重新获取，不会误判「已在运行」。

    返回：
      - fd（>0）：持有锁；
      - False：锁文件不可用（目录只读等），已跳过单实例保护，调用方应继续启动；
      - None：确实已有实例在运行 / 锁获取失败，调用方应退出。
    """
    # 【踩坑保佑】v1.10.0 单实例文件锁
    # 坑：退出残留锁文件误杀后续实例，加atexit兜底  →  acquire_instance_lock
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    lock_path = instance_lock_path(root)
    lock_dir = os.path.dirname(lock_path)
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except Exception as _exc:
        logger.debug('创建锁目录失败: %s', _exc)

    try:
        fd = open_lock_file(lock_path)
    except Exception as e:
        # 旧实现这里 return None，而调用方是 `if lock_fd is None: sys.exit(1)`，
        # 于是日志说「跳过单实例保护」，实际却直接退出，报错方向完全相反。
        # 锁文件建不出来只是「没有保护」，不是「有另一个实例」。
        logger.warning(f"实例锁文件不可用（{lock_path}），跳过单实例保护: {e}")
        return False

    if try_lock(fd):
        _INSTANCE_LOCK['path'] = lock_path
        _INSTANCE_LOCK['fd'] = fd
        write_lock_pid(fd)
        return fd

    # 拿不到锁：可能是真有另一个实例，也可能是残留 stale 锁（持有进程已死）
    holder_pid = read_lock_pid(fd)
    if holder_pid is not None and pid_is_our_instance(holder_pid, root):
        # 确属本框架仍在运行的实例 -> 明确提示并拒绝
        try:
            os.close(fd)
        except Exception as _exc:
            logger.debug('关闭锁文件描述符失败: %s', _exc)
        print("\n" + "=" * 60)
        print(f"[实例锁] 检测到另一个 ZCBOT 实例正在运行（PID {holder_pid}），")
        print("          本进程立即退出，避免多实例抢端口 / 抢 DB / 双写。")
        print("          如需强制启动，请先结束该进程或删除锁文件：")
        print(f"          {lock_path}")
        print("=" * 60)
        logger.warning(f"实例锁冲突：检测到另一个 ZCBOT 实例（PID {holder_pid}），本进程退出")
        return None
    if holder_pid is None:
        # 锁被占用但读不到 PID（极少见，如权限/共享拦截）-> 保守拒绝并提示
        try:
            os.close(fd)
        except Exception as _exc:
            logger.debug('关闭锁文件描述符失败: %s', _exc)
        print("\n" + "=" * 60)
        print("[实例锁] 检测到实例锁已被占用（PID 未知），本进程立即退出，")
        print("          避免多实例抢端口 / 抢 DB / 双写。如需强制启动，")
        print("          请先结束占用进程或删除锁文件：")
        print(f"          {lock_path}")
        print("=" * 60)
        logger.warning("实例锁冲突：锁被占用但 PID 未知，本进程退出")
        return None

    # stale 锁：持有者已死或无法确认 -> 清理残留并重新获取
    print(f"\n[实例锁] 发现残留锁文件（记录的 PID {holder_pid} 已不存在或无效），"
          f"判定为 stale，清理后继续启动。")
    logger.info(f"实例锁：清理 stale 锁文件（原 PID {holder_pid}），路径 {lock_path}")
    remove_stale_lock(lock_path, fd)
    try:
        fd = open_lock_file(lock_path)
    except Exception as e:
        logger.warning(f"实例锁文件重建失败，跳过单实例保护: {e}")
        return None
    if try_lock(fd):
        _INSTANCE_LOCK['path'] = lock_path
        _INSTANCE_LOCK['fd'] = fd
        write_lock_pid(fd)
        return fd
    # 重建后仍失败（极少见，可能真有并发启动）-> 保守拒绝并提示
    try:
        os.close(fd)
    except Exception as _exc:
        logger.debug('关闭锁文件描述符失败: %s', _exc)
    print("\n" + "=" * 60)
    print("[实例锁] 清理残留锁后仍无法获取实例锁，疑似并发启动，本进程退出。")
    print("=" * 60)
    logger.warning("实例锁：清理 stale 后仍无法获取，疑似并发启动，本进程退出")
    return None


def cleanup_instance_lock():
    """退出时释放并删除实例锁文件（atexit + finally 双保险）。

    仅清理本进程持有的锁（_INSTANCE_LOCK 记录当前 fd / 路径），不会误删
    其他存活实例的锁；清理前先解锁再关闭再删除，跨平台安全。
    """
    info = _INSTANCE_LOCK
    fd = info.get('fd')
    path = info.get('path')
    if fd is not None:
        try:
            if sys.platform == 'win32':
                try:
                    import msvcrt
                    os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except Exception as _exc:
                    logger.debug('释放文件锁失败（Windows 分支）: %s', _exc)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception as _exc:
            logger.debug('释放文件锁失败（fcntl 分支）: %s', _exc)
        try:
            os.close(fd)
        except Exception as _exc:
            logger.debug('关闭锁文件描述符失败: %s', _exc)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as _exc:
            logger.debug('删除锁文件失败: %s', _exc)
    _INSTANCE_LOCK['fd'] = None
    _INSTANCE_LOCK['path'] = None
