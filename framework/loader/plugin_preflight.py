"""启动期插件预检：插件加载前把插件目录整体扫一遍，命中写日志、不阻断启动。

与加载期逐插件扫描的分工：

- 预检（本模块）：启动早期**整目录**体检，一次把「哪些插件里有同步阻塞 / 同步
  等待 / 同步退出」全部打出来，用户开一次日志就能看全，不必等某个插件加载到；
- 加载期（``scan_plugin``）：只管当前这个插件，并按 ``plugins.source_scan.mode``
  决定放行（warn，默认）还是拒绝加载（block）。

预检的任何失败（目录不存在、读不到配置、AST 解析异常）都只记 debug 日志，
绝不向上抛——它的职责是让问题可见，不是拦住框架。
"""
import logging
import os

logger = logging.getLogger('zcbot')

_TAG = '[插件预检]'

# 单次启动最多逐条打印多少命中；其余汇总成一行，避免几百条把启动日志刷穿。
_DEFAULT_MAX_REPORT = 50


def _section(config):
    """取 ``plugins.source_scan`` 配置段（缺失/类型不对时返回空 dict）。"""
    if not isinstance(config, dict):
        return {}
    plugins = config.get('plugins')
    if not isinstance(plugins, dict):
        return {}
    section = plugins.get('source_scan')
    return section if isinstance(section, dict) else {}


def _as_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ('1', 'true', 'yes', 'on', 'y'):
            return True
        if low in ('0', 'false', 'no', 'off', 'n'):
            return False
    if value is None:
        return default
    return default


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _display(path):
    """把路径收敛成相对代码根的写法（统一用 /），启动日志更短也更好 grep。"""
    abs_path = str(path).replace(os.sep, '/')
    try:
        from framework.paths import code_root

        rel = os.path.relpath(str(path), code_root())
    except Exception:
        return abs_path
    if rel.startswith('..'):
        return abs_path
    return rel.replace(os.sep, '/')


def plugin_dirs(config=None):
    """返回本进程会扫描的 ``(标签, 目录)`` 列表。

    官方插件目录是否参与由 ``plugins.source_scan.scan_core_plugins`` 决定
    （默认 true）：插件跑在核心进程内，官方插件阻塞同样是全链路卡住。
    """
    from framework.paths import core_plugins_dir, resolve_user_plugins_dir

    cfg = _section(config)
    plugin_cfg = (config or {}).get('plugin') if isinstance(config, dict) else None
    plugin_cfg = plugin_cfg if isinstance(plugin_cfg, dict) else {}
    out = [('插件目录', resolve_user_plugins_dir(str(plugin_cfg.get('dir') or '')))]
    if _as_bool(cfg.get('scan_core_plugins', True), True):
        out.append(('官方插件目录', core_plugins_dir()))
    return out


def run(config=None):
    """扫描插件目录并打印结果，返回汇总 dict（永不上抛异常）。

    汇总结构::

        {
          'enabled': bool,
          'dirs': [{'label': str, 'path': str, 'plugins': int}],
          'plugins': int,        # 扫到的插件入口总数
          'issues': [SourceIssue.as_dict() + {'file': 相对路径}],
          'truncated': int,      # 未逐条打印的条数
        }
    """
    summary = {'enabled': False, 'dirs': [], 'plugins': 0, 'issues': [], 'truncated': 0}
    try:
        from framework.loader import source_scan

        cfg = _section(config)
        if not _as_bool(cfg.get('enabled', True), True):
            logger.debug('%s 已关闭（plugins.source_scan.enabled=false），跳过', _TAG)
            return summary
        summary['enabled'] = True

        issues = []
        for label, path in plugin_dirs(config):
            count, hits = source_scan.scan_tree(path, config=config)
            summary['dirs'].append({'label': label, 'path': _display(path), 'plugins': count})
            summary['plugins'] += count
            issues.extend(hits)

        allowlist = cfg.get('allowlist') or []
        if isinstance(allowlist, str):
            allowlist = [allowlist]
        allowlist = {str(item) for item in allowlist}

        entries = []
        for issue in issues:
            plugin = _plugin_name_of(issue.path)
            if plugin and plugin in allowlist:
                continue
            item = issue.as_dict()
            item['file'] = _display(issue.path)
            item['plugin'] = plugin
            entries.append(item)
        summary['issues'] = entries
        _emit(summary, cfg)
    except Exception as exc:  # 预检失败绝不能挡住启动
        logger.debug('%s 执行失败（已忽略）: %s', _TAG, exc)
    return summary


def _plugin_name_of(path):
    """从 ``.../<plugin>/main.py`` 里取插件名，取不到返回空串。"""
    parent = os.path.basename(os.path.dirname(str(path)))
    return parent or ''


def _emit(summary, cfg):
    """把汇总写进日志：一条总览 + 逐条命中（最多 max_report 条）。"""
    dirs_text = '、'.join(
        f"{d['label']} {d['path']}（{d['plugins']} 个插件）" for d in summary['dirs']
    ) or '（无可用插件目录）'
    issues = summary['issues']

    if not summary['plugins']:
        logger.info('%s 未找到可扫描的插件入口：%s', _TAG, dirs_text)
        return
    if not issues:
        logger.info('%s 扫描 %s，未发现同步阻塞 / 同步等待 / 同步退出写法', _TAG, dirs_text)
        return

    mode = str(cfg.get('mode') or 'warn').strip().lower()
    tail = ('命中后仅告警，插件照常加载（plugins.source_scan.mode=warn）'
            if mode != 'block' else
            '命中即拒绝加载该插件（plugins.source_scan.mode=block）')
    logger.warning('%s 扫描 %s，命中 %d 处可疑写法；%s',
                   _TAG, dirs_text, len(issues), tail)

    limit = _as_int(cfg.get('max_report'), _DEFAULT_MAX_REPORT)
    if limit < 0:
        limit = _DEFAULT_MAX_REPORT
    shown = issues[:limit]
    for item in shown:
        logger.warning('%s   %s:%d:%d [%s] %s',
                       _TAG, item.get('file', ''), item.get('line', 0),
                       item.get('column', 0), item.get('kind', ''), item.get('detail', ''))
    summary['truncated'] = max(0, len(issues) - len(shown))
    if summary['truncated']:
        logger.warning('%s   另有 %d 处未逐条打印（调大 plugins.source_scan.max_report 可看全）',
                       _TAG, summary['truncated'])
    logger.info('%s 可用 plugins.source_scan.enabled=false 关闭本次扫描', _TAG)
