# -*- coding: utf-8 -*-
"""zcbot 命令行入口（`pip install zcbot` 后执行 `zcbot`）。

把「可写状态」与「代码位置」解耦：
- 代码（framework / core_plugins / plugins / main.py）随包安装在 site-packages（只读）；
- 状态（config.yaml / core_plugins.yaml / data / logs / plugins）落在工作目录
  ZCBOT_HOME（默认 ~/.zcbot）。

首次运行时：创建 home、播种内置插件、生成默认 config.yaml，然后 chdir 到 home
执行 main.py。用户可用环境变量 ZCBOT_HOME 指定其它工作目录。
"""
import os
import shutil
import sys


def _resolve_home() -> str:
    env = os.environ.get('ZCBOT_HOME')
    if env:
        return os.path.abspath(env)
    from framework.config import get_home
    return get_home()


def _seed_plugins(code_root: str, home: str) -> None:
    """首次运行：把随包分发的内置插件复制到 home/plugins（已存在则不覆盖）。"""
    src = os.path.join(code_root, 'plugins')
    dst = os.path.join(home, 'plugins')
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s) and not os.path.exists(d):
            try:
                shutil.copytree(s, d)
            except Exception:
                pass


def main() -> int:
    from framework.config import get_code_root, load_config
    code_root = get_code_root()
    home = _resolve_home()
    os.makedirs(home, exist_ok=True)
    # 显式写入环境变量：后续 import 的 framework 模块读到同一个 home
    os.environ['ZCBOT_HOME'] = home

    _seed_plugins(code_root, home)
    # 不存在则自动生成默认 config.yaml 到 home
    load_config(os.path.join(home, 'config.yaml'))

    os.chdir(home)
    if home not in sys.path:
        sys.path.insert(0, home)

    import runpy
    runpy.run_path(os.path.join(code_root, 'main.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
