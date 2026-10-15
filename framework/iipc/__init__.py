# -*- coding: utf-8 -*-
"""IIPC 内核链路（Go 内核 + Rust 协议端 + 每插件独立进程）。

**以源码方式随框架分发**：本目录只含源码，不含任何编译产物。
首次运行会自动编译（也可预先 `build()`），产物落在
`go_kernel/zcbot_kernel.exe` 与 `rust_adapter/target/release/zcbot_adapter.exe`。

两种启动方式：
    python main.py --iipc [选项]        # 框架主入口（推荐）
    python -m framework.iipc [选项]     # 独立模块入口

与原 asyncio 框架的关系：**并列的两条启动路径**，不是插件、也不互相加载。
不带 --iipc 时框架行为完全不变。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

__all__ = ["run", "build", "HERE"]


def _load_launcher():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import launcher  # noqa: E402  同目录下，非框架包内模块
    return launcher


def run(argv=None):
    """启动 IIPC 全栈并进入终端 REPL（阻塞，直到 quit / Ctrl+C）。

    argv 为参数列表；不管传不传，内部都会补上 --iipc。
    """
    launcher = _load_launcher()
    argv = list(argv or [])
    if "--iipc" not in argv and "-iipc" not in argv:
        argv = ["--iipc"] + argv
    return launcher.main(argv)


def build():
    """只编译，不启动：Go 内核 + Rust 协议端。返回 (kernel_ok, rust_ok)。"""
    launcher = _load_launcher()
    return launcher.build_kernel(), launcher.build_rust()


def main(argv=None):
    """`python -m framework.iipc` 入口。"""
    return run(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main() or 0)
