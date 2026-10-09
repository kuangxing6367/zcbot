# -*- coding: utf-8 -*-
"""终端编码对齐（Windows 控制台代码页 vs Python UTF-8 模式）。

背景：Python 若跑在 UTF-8 模式（`PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`），
而 Windows 控制台代码页是 936（GBK），则输出按 UTF-8 编码、控制台按 GBK 解码，
中文即乱码；输入（msvcrt.getch 读到的是代码页字节）同理。

策略：优先把控制台代码页切到 65001（UTF-8），与 Python 的 UTF-8 模式对齐；
若控制台不支持，返回 False，由调用方退化为「把 stdio 编码对齐到控制台代码页」。

全部 best-effort，绝不抛异常；非 Windows 直接视为已对齐。
"""
import os

_utf8_applied = False


def console_codepages():
    """返回 (输出代码页, 输入代码页)；非 Windows 返回 (None, None)。"""
    if os.name != 'nt':
        return None, None
    try:
        import ctypes
        k = ctypes.windll.kernel32
        return int(k.GetConsoleOutputCP()), int(k.GetConsoleCP())
    except Exception:
        return None, None


def ensure_utf8_console() -> bool:
    """把 Windows 控制台代码页切到 UTF-8（65001）。

    :return: 是否处于 UTF-8（非 Windows / 已是 65001 / 切换成功均为 True；
        控制台不支持切换则为 False）。
    """
    global _utf8_applied
    if os.name != 'nt':
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        out_cp, in_cp = console_codepages()
        if out_cp == 65001 and in_cp == 65001:
            _utf8_applied = True
            return True
        ok_out = bool(k.SetConsoleOutputCP(65001))
        ok_in = bool(k.SetConsoleCP(65001))
        if ok_out and ok_in:
            _utf8_applied = True
            return True
    except Exception:
        pass
    return False


def align_stdio_to_console():
    """兜底：把 sys.stdout / sys.stdin 的编码对齐到控制台实际代码页。

    仅在控制台无法切到 UTF-8 时使用。返回 {流名: 原编码} 以便还原。
    """
    saved = {}
    out_cp, in_cp = console_codepages()
    pairs = [('stdout', out_cp), ('stdin', in_cp)]
    for name, cp in pairs:
        if not cp:
            continue
        s = getattr(__import__('sys'), name, None)
        if s is None or not hasattr(s, 'reconfigure'):
            continue
        saved[name] = getattr(s, 'encoding', None)
        try:
            s.reconfigure(encoding='cp%d' % cp, errors='replace')
        except Exception:
            saved.pop(name, None)
    return saved


def restore_stdio(saved):
    """还原 align_stdio_to_console() 改过的 stdio 编码。"""
    import sys
    for name, enc in (saved or {}).items():
        s = getattr(sys, name, None)
        if s is not None and enc and hasattr(s, 'reconfigure'):
            try:
                s.reconfigure(encoding=enc)
            except Exception:
                pass
