"""插件目录归属与缓存回收。

框架与插件之间的磁盘契约只有两类目录：

- ``plugins_dat/<name>/``：用户数据 / 配置。卸载、重载、禁用都**保留**，
  只有显式「删除插件并勾选删除数据」才清理（见 api/plugins.py 的 delete_data）。
- ``cache/<name>/``：可丢弃的中间产物（渲染图、临时下载、缩略图…）。
  卸载即清空；框架另有内置定时任务按 TTL 扫全量。

插件名到目录名的映射统一收敛在这里（``safe_dirname``）。官方插件 ctx 名带
``core:`` 前缀，冒号在 Windows 路径非法，替换为下划线——历史上 ctx 和 loader
各自实现过一遍，两处不一致就会出现「插件写进了 A 目录，卸载去清 B 目录」的
隐性泄漏。缓存 TTL 清理的配置规格（默认值 + 语义）也放这里，作为单一事实源。
"""

import os
import shutil
import time

__all__ = [
    'safe_dirname',
    'plugin_cache_dir',
    'purge_plugin_cache',
    'purge_expired_cache',
    'CACHE_CLEANUP_SPEC',
    'resolve_cache_cleanup_config',
]

# 缓存 TTL 定时清理的配置 schema + 默认值（单一事实源）。
#
# 归属 config.yaml 的 ``scheduler`` 段。framework/config.py 的全局
# _CONFIG_SCHEMA / _DEFAULT_CONFIG 不在本次允许改动范围，故本特性相关的
# 配置项把「默认值 / 类型 / 取值范围 / 语义」集中定义在这里，由
# TaskScheduler 启动时经 resolve_cache_cleanup_config() 统一解析，
# 缺失或非法值一律回退默认值（等价于「有默认值」）。
#
# TTL 语义（明确二选一，杜绝「TTL=0 变成无差别全删」）：
#   cache_ttl_hours > 0  → 启用定时清理，按 mtime 删除超过 TTL 的文件；
#   cache_ttl_hours <= 0 → 禁用定时清理，不注册任务、不删任何文件。
CACHE_CLEANUP_SPEC = {
    'cache_ttl_hours': {
        # min=None：允许 0 / 负数，它们保留下来用于表达「禁用」
        'default': 24.0, 'min': None,
        'desc': '缓存文件超过该小时数未修改即清理；<=0 表示禁用定时清理',
    },
    'cache_cleanup_interval_hours': {
        'default': 1.0, 'min': 0.01,
        'desc': 'TTL 全量扫描周期（小时）',
    },
    'cache_min_age_seconds': {
        'default': 60.0, 'min': 0.0,
        'desc': '最小保留时长（秒），保底避免删掉刚写入/正在写的文件',
    },
}


def resolve_cache_cleanup_config(scheduler_cfg) -> dict:
    """按 CACHE_CLEANUP_SPEC 解析 scheduler 段，返回归一化的清理配置。

    :param scheduler_cfg: config.yaml 的 ``scheduler`` 段（dict）
    :return: {'enabled', 'ttl_hours', 'interval_hours', 'min_age_seconds'}
             enabled 为 ``ttl_hours > 0``；禁用时调用方不应注册/执行清理。
    """
    src = scheduler_cfg if isinstance(scheduler_cfg, dict) else {}
    out = {}
    for key, spec in CACHE_CLEANUP_SPEC.items():
        try:
            val = float(src.get(key, spec['default']))
        except (TypeError, ValueError):
            val = float(spec['default'])
        lo = spec.get('min')
        if lo is not None and val < lo:
            val = float(spec['default'])
        out[key] = val
    ttl = out['cache_ttl_hours']
    return {
        'enabled': ttl > 0,
        'ttl_hours': ttl,
        'interval_hours': out['cache_cleanup_interval_hours'],
        'min_age_seconds': out['cache_min_age_seconds'],
    }


def safe_dirname(plugin_name: str) -> str:
    """插件名 → 合法目录名（与 ctx.get_data_dir 的规则保持一致）。

    官方插件 ctx 名带 ``core:`` 前缀，冒号在 Windows 路径非法 → 下划线
    （``core:webui`` → ``core_webui``）。这是既有行为，务必保持，否则
    「写进 A 目录、卸载去清 B 目录」。

    另做目录穿越加固：路径分隔符 / Windows 非法字符一律替换为下划线，
    消除 ``..`` 跳转片段并去掉首尾点/空白，保证
    ``os.path.join(root, safe_dirname(name))`` 永远不会逃出 ``root``。
    对合法插件名（字母/数字/下划线/横杠，可含中文）结果完全不变。
    """
    name = str(plugin_name).replace(':', '_')
    for ch in ('/', '\\', '\x00', '|', '?', '*', '<', '>', '"'):
        name = name.replace(ch, '_')
    while '..' in name:
        name = name.replace('..', '.')
    name = name.strip('. ')
    return name or '_'


def _cache_root(framework) -> str:
    """取缓存根目录；老对象没有 cache_dir 属性时按同一规则兜底。"""
    root = getattr(framework, 'cache_dir', None)
    if root:
        return root
    from framework.config import get_home
    cfg = (getattr(framework, 'config', None) or {}).get('cache', {}) or {}
    cache_dir = cfg.get('dir', '') or 'data/cache'
    if os.path.isabs(cache_dir):
        return cache_dir
    return os.path.join(get_home(), cache_dir)


def plugin_cache_dir(framework, plugin_name: str, create: bool = True) -> str:
    """返回某插件的缓存目录绝对路径（默认自动创建）。"""
    # 【踩坑保佑】v1.20.1 插件缓存清理
    # 坑：cache目录可丢弃中间产物原先没人清  →  plugin_cache_dir
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

    path = os.path.join(_cache_root(framework), safe_dirname(plugin_name))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def purge_plugin_cache(framework, plugin_name: str) -> bool:
    """清空某插件的缓存目录（目录本身保留，避免与并发写入竞态）。

    卸载插件时调用。缓存都是可重建的，删错也不会丢用户数据。
    绝不触碰 ``plugins_dat`` 用户数据目录。
    """
    path = plugin_cache_dir(framework, plugin_name, create=False)
    if not os.path.isdir(path):
        return False
    removed = 0
    for entry in os.listdir(path):
        target = os.path.join(path, entry)
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.remove(target)
            removed += 1
        except OSError:
            continue
    return removed > 0


def purge_expired_cache(framework, ttl_hours: float,
                        min_age_seconds: float = 0) -> dict:
    """按 mtime 清理缓存根目录下的过期文件（只删文件与随之变空的子目录）。

    TTL 语义明确，杜绝「TTL=0 变成无差别全删」：

    - ``ttl_hours <= 0``：**禁用清理**，直接返回空 stats，不删任何文件；
      （接入层也会因此不注册定时任务，双保险）
    - ``ttl_hours > 0``：删除 mtime 早于
      ``max(ttl_hours*3600, min_age_seconds)`` 的文件；``min_age_seconds``
      是保底最小保留时长，避免删掉刚写入/正在写的文件。

    :return: {'files': 删除文件数, 'dirs': 删除空目录数, 'freed': 释放字节}
    """
    root = _cache_root(framework)
    stats = {'files': 0, 'dirs': 0, 'freed': 0}
    if not os.path.isdir(root):
        return stats

    try:
        ttl = float(ttl_hours)
    except (TypeError, ValueError):
        return stats
    if ttl <= 0:
        return stats  # 禁用：绝不无差别全删
    threshold = max(ttl * 3600.0, float(min_age_seconds))
    now = time.time()

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
                if os.path.islink(path) or now - st.st_mtime < threshold:
                    continue
                size = st.st_size
                os.remove(path)
                stats['files'] += 1
                stats['freed'] += size
            except OSError:
                continue
        # 自底向上删空目录（不碰 root 本身）
        if os.path.normpath(dirpath) == os.path.normpath(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                stats['dirs'] += 1
        except OSError:
            continue
    return stats
