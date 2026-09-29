# -*- coding: utf-8 -*-
"""进程内存归还辅助（best-effort）。

为什么需要它：
CPython 默认用 pymalloc 管理小对象（<512B），pymalloc 向系统要来的 arena 一旦分配就
不再主动还给 OS——这是解释器固有限制，Python 层无法彻底清零。因此经历大峰值后
（例如解闸 50w 把 RSS 顶到 1.7GB），即使对象已全部释放、队列已排空，RSS 也会长期
残留一个地板（实测约 290MB）。本模块做的是能压多少压多少：

- Linux  ：malloc_trim(0) 回收 glibc 堆顶已 free 的空闲块（对大对象区有效）；
- Windows：SetProcessWorkingSetSize(-1,-1) 建议内核回收可分页的工作集页；
- 另     ：PYTHONMALLOC=malloc 可让 Python 放弃 pymalloc、直用系统分配器，
           使 malloc_trim 能覆盖全部内存（小对象分配略慢，按需在部署时启用）。
"""
import gc
import sys
import threading

_lock = threading.Lock()


def trim_memory() -> bool:
    """尽力把已释放内存归还 OS，返回是否成功执行归还动作（不含"回收了多少"）。

    内部先做 gc.collect() 释放循环引用。低频调用（清空 / 峰值回落时），
    不要在热路径每批调用——gc.collect 与系统调用均有代价。
    """
    gc.collect()
    if sys.platform == 'linux':
        try:
            import ctypes
            libc = ctypes.CDLL('libc.so.6')
            libc.malloc_trim.restype = ctypes.c_int
            libc.malloc_trim.argtypes = [ctypes.c_size_t]
            libc.malloc_trim(0)
            return True
        except Exception:
            return False
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSize.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
            kernel32.SetProcessWorkingSetSize.restype = ctypes.c_int
            h = kernel32.GetCurrentProcess()
            # -1, -1 交给系统决定最小/最大工作集（即尽力收缩到当前必需）
            kernel32.SetProcessWorkingSetSize(
                h, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
            return True
        except Exception:
            return False
    return False


def malloc_mode() -> str:
    """返回当前 Python 内存分配器模式（用于启动提示）。"""
    try:
        return sys.getallocatorname()  # 3.11+
    except AttributeError:
        return 'unknown'


def log_malloc_hint(logger):
    """启动期记录分配器模式与可选优化提示（best-effort，不改动任何行为）。"""
    try:
        mode = malloc_mode()
        if mode == 'pymalloc':
            logger.info(
                "[内存] 当前分配器=pymalloc（默认）。经历大峰值后 RSS 可能长期残留"
                "（pymalloc arena 不还 OS）。若部署机追求更低内存地板，可用环境变量 "
                "PYTHONMALLOC=malloc 启动（小对象分配略慢，但能让 malloc_trim 覆盖全部"
                "内存，配合内存看门狗更有效）。"
            )
        else:
            logger.info(
                f"[内存] 当前分配器={mode}。内存看门狗将在峰值回落后主动归还空闲内存。"
            )
    except Exception:
        pass
