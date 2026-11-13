#!/usr/bin/env python3

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

"""ZCBOT 兼容启动 shim —— 实际启动逻辑已统一到 ``startup.py``。

保留本文件的原因：`python main.py ...` 是历史文档、systemd 单元、NSSM 配置、
``zb generate`` 旧脚本与回归测试都在用的命令；删掉会破坏存量部署。
这里只做两件事：把 ``startup`` 的符号原样再导出，以及把 ``__main__`` 转派过去。

新代码请直接用 ``python startup.py`` 或 ``from startup import main``。
"""
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from startup import (                                          # noqa: E402,F401
    amain, main,
    _read_self_check_allow_missing,
    _resolve_self_check_missing,
    _check_and_install_deps,
    _ensure_optional_driver,
    _print_usage,
    _read_version,
    _pop_language_arg,
    _run_attach,
)

if __name__ == '__main__':
    # 分配器策略必须在 import 重型 framework 之前应用；startup 以被 import
    # 身份进入时不会自行应用（避免 pytest 收集期间 execv 产生「假绿」）。
    from framework.bootstrap.allocator import apply_allocator_policy
    apply_allocator_policy()
    multiprocessing.freeze_support()
    main()
