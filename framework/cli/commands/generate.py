# -*- coding: utf-8 -*-
"""``zb generate`` —— 生成启动脚本。"""
import os

from ..utils import PROG

LAUNCHER = '''# -*- coding: utf-8 -*-
"""启动脚本（由 `{prog} generate` 生成）。"""
import os
import sys

_home = os.environ.get("ZCBOT_HOME")
if _home:
    sys.path.insert(0, _home)

from main import main

if __name__ == "__main__":
    main()
'''


def do_generate(args, working_dir):
    file_path = os.path.join(working_dir, args.file)
    if os.path.exists(file_path):
        print("错误：文件已存在: %s（不覆盖）" % file_path)
        return 1
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(LAUNCHER.format(prog=PROG))
    print("已生成启动脚本: %s" % file_path)
    print("运行：python %s" % args.file)
    return 0
