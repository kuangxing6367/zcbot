#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zb —— ZCBOT 命令行工具（脚手架）。

等价于 ``python -m framework.cli``。在仓库根目录下直接运行：

    python zb.py --help
    python zb.py create plugin my_plugin
    python zb.py run

若以包形式安装（``pip install -e .``），``zb`` 会作为控制台脚本可用。
"""
import sys

from framework.cli import main

if __name__ == "__main__":
    sys.exit(main())
