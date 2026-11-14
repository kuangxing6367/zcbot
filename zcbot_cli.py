"""zcbot 命令行入口（`pip install zcbot` 后执行 `zcbot`）。

把「可写状态」与「代码位置」解耦：
- 代码（framework / core_plugins / startup.py / main.py）随包安装在 site-packages（只读）；
- 状态（config.yaml / core_plugins.yaml / plugins / logs）统一落在工作目录
  ZCBOT_HOME/data（默认 ~/.zcbot/data），旧的根目录布局仍兼容。

首次运行时：创建 home、播种内置插件、生成默认 config.yaml，然后 chdir 到 home
执行唯一的启动入口 startup.py。用户可用环境变量 ZCBOT_HOME 指定其它工作目录。
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
    """首次运行：把随包分发的内置插件播种到 <home>/data/plugins（已存在则不覆盖）。"""
    from framework.paths import user_plugins_dir
    src = os.path.join(code_root, 'plugins')
    dst = user_plugins_dir()
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
    # pip 安装态一律使用 data 新布局（源码 checkout 不设此变量，保持旧布局兼容）
    os.environ.setdefault('ZCBOT_STATE_LAYOUT', 'data')
    from framework.paths import state_dir
    os.makedirs(os.path.join(state_dir(), 'plugins'), exist_ok=True)

    _seed_plugins(code_root, home)
    # 不存在则自动生成默认 config.yaml（路径由统一解析层决定，优先 data/）
    from framework.paths import resolve_config_path
    load_config(resolve_config_path())

    os.chdir(home)
    if home not in sys.path:
        sys.path.insert(0, home)

    import runpy
    runpy.run_path(os.path.join(code_root, 'startup.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
