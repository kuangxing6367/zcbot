# -*- coding: utf-8 -*-
"""全仓统一的路径解析层（只用标准库，可在依赖自检之前安全 import）。

历史上仓库里有两套互相矛盾的口径：
  · 可写状态（config.yaml / core_plugins.yaml / plugins / data）走 ``get_home()``；
  · 官方插件代码目录走 ``__file__`` 上溯，甚至同一个模块里两种混用。
本模块把两者收敛为一组函数，并让「状态」优先落在 ``<home>/data/``：

    <home>/data/config.yaml           主配置（新布局；旧布局在 <home>/ 根）
    <home>/data/core_plugins.yaml     官方插件启停清单
    <home>/data/plugins/              用户插件代码
    <home>/data/plugins_dat/          插件用户数据（早已在此）
    <home>/data/logs/                 日志（早已在此）
    <code_root>/core_plugins/         内核自带插件（代码，随包只读）
    <code_root>/framework/core/plugins/  可选的内核内嵌插件位置

兼容规则（不破坏既有部署与回归测试）：
  1. 显式传入的路径 / ``ZCBOT_CONFIG_PATH`` 永远优先；
  2. ``data/`` 下的文件存在即优先采用，否则回退旧的根目录位置；
  3. ``ZCBOT_STATE_LAYOUT=data`` 可在文件尚未迁移时强制指向 data 布局；
  4. ``ZCBOT_HOME`` 决定「可写状态根目录」，其余全部由它推导。

``framework.config.get_home()`` 已委托到 ``home_dir()``，避免两处实现漂移。
迁移子系统在内核 ``framework/migration/``：启动期自动执行（默认开启，
``config.yaml`` 的 ``framework.auto_migrate: false`` 可关闭），命令行入口
``python -m framework.migration --inspect``。本模块只保留路径解析，不承载迁移逻辑。
"""
import os
import sysconfig

__all__ = [
    'code_root', 'home_dir', 'state_dir', 'resolve_config_path',
    'core_plugins_yaml_path', 'core_plugins_dir', 'user_plugins_dir',
    'plugins_dat_dir', 'webui_web_dir', 'version_path',
    'migration_state_path',
]

CONFIG_NAME = 'config.yaml'
CORE_YAML_NAME = 'core_plugins.yaml'
# 迁移账本 / 并发锁（由 framework/migration/ 维护，常量放此处避免两处漂移）
MIGRATION_STATE_NAME = '.zcbot-state-migration.json'
MIGRATION_LOCK_NAME = '.zcbot-state-migration.lock'


def code_root() -> str:
    """代码根目录（framework/ 的父目录）：源码运行=仓库根，pip 安装=site-packages。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _in_site_packages(path: str) -> bool:
    try:
        p = os.path.abspath(path)
        for key in ('purelib', 'platlib'):
            d = sysconfig.get_paths().get(key)
            if d and p.startswith(os.path.abspath(d)):
                return True
    except Exception:
        pass
    return False


def home_dir() -> str:
    """可写状态根目录：ZCBOT_HOME > ~/.zcbot（pip 安装态）> 代码根（源码运行）。"""
    env = os.environ.get('ZCBOT_HOME')
    if env:
        return os.path.abspath(env)
    root = code_root()
    if _in_site_packages(root):
        return os.path.join(os.path.expanduser('~'), '.zcbot')
    return root


def state_dir() -> str:
    """状态目录：所有配置 / 插件 / 日志 / 锁文件的统一落点。"""
    return os.path.join(home_dir(), 'data')


def _prefer_data(name: str, explicit: str = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get('ZCBOT_CONFIG_PATH') if name == CONFIG_NAME else None
    if env:
        return os.path.abspath(env)
    in_data = os.path.join(state_dir(), name)
    legacy = os.path.join(home_dir(), name)
    if os.path.isfile(in_data):
        return in_data
    if os.environ.get('ZCBOT_STATE_LAYOUT', '').strip().lower() == 'data':
        return in_data
    return legacy


def resolve_config_path(explicit: str = None) -> str:
    """解析主配置路径。显式相对路径按 cwd → data/ → home/ 依次探测。"""
    if explicit and not os.path.isabs(explicit):
        candidates = (os.path.abspath(explicit),
                      os.path.join(state_dir(), explicit),
                      os.path.join(home_dir(), explicit))
        for c in candidates:
            if os.path.isfile(c):
                return c
        return candidates[0]
    return _prefer_data(CONFIG_NAME, explicit)


def core_plugins_yaml_path() -> str:
    """官方插件启停清单（唯一权威）路径。"""
    env = os.environ.get('ZCBOT_CORE_PLUGINS_YAML')
    if env:
        return os.path.abspath(env)
    return _prefer_data(CORE_YAML_NAME)


def core_plugins_dir() -> str:
    """内核自带插件代码目录：优先 framework/core/plugins，回退根目录 core_plugins。

    对应「core 下插件放于内核下」的重构目标：把官方插件视为内核能力，允许
    未来直接内嵌到 framework/core/plugins/；未迁移时保持原位置，零破坏。
    """
    inner = os.path.join(code_root(), 'framework', 'core', 'plugins')
    if os.path.isdir(inner):
        return inner
    return os.path.join(code_root(), 'core_plugins')


def user_plugins_dir() -> str:
    """用户插件代码目录：data/plugins 存在即用，否则旧的 <home>/plugins。"""
    in_data = os.path.join(state_dir(), 'plugins')
    if os.path.isdir(in_data):
        return in_data
    return os.path.join(home_dir(), 'plugins')


def plugins_dat_dir() -> str:
    """插件用户数据目录（始终在 data 下）。"""
    return os.path.join(state_dir(), 'plugins_dat')


def webui_web_dir() -> str:
    """WebUI 构建产物目录（代码资产，随内核插件分发）。"""
    return os.path.join(core_plugins_dir(), 'webui', 'web')


def version_path() -> str:
    return os.path.join(code_root(), 'VERSION')


def migration_state_path() -> str:
    """状态目录迁移账本路径（由 framework.migration 读写）。"""
    return os.path.join(state_dir(), MIGRATION_STATE_NAME)
