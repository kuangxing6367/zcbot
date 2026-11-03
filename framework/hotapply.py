# -*- coding: utf-8 -*-
"""
配置热应用（Hot Apply）
========================
问题：`PUT /api/config/yaml/<section>` 一直只做两件事——写 config.yaml、把新值合并进
`framework.config`。但很多配置项在启动时已经被**读取成对象/闭包**（日志级别、限速器、
黑匣子上限等），改文件 + 改字典并不会让它们重新生效，用户看到「已保存」却要重启才有效，
和「插件保存即热重载」的体验不一致。

本模块把「改完文件之后要不要重启」这件事显式化：

    apply_section(framework, section, data)
        -> {'section', 'live', 'restart_required', 'notes'}

- `notes` 非空 → 这些字段已经即时生效；
- `restart_required=True` → 该段仍有必须重启才生效的字段（如端口、数据库类型）。

每段是否可以热应用由两部分决定：
1. 内置 applier（日志级别 / 黑匣子 / 限速参数 / 插件热重载开关）；
2. `register(section, fn)` 供 Web 层注册闭包型 applier（例如限速器实例）。

未注册 applier 的配置段一律保守判定为「需重启」——宁可让用户重启，也不假装生效。
"""
import logging

logger = logging.getLogger('zcbot')

# 段 -> applier(framework, section_cfg) -> list[str]（返回已即时生效的说明）
_APPLIERS = {}

# 已知必须重启的段（用于给面板/文档一个确定答案，而不是「可能」）
RESTART_ONLY_SECTIONS = {
    'database': '数据库类型/连接参数在启动时建立连接池',
    'onebot': '监听端口与访问令牌在启动时绑定',
    'web': '监听地址/端口/UI 开关在启动时绑定',
    'ssl': '证书在启动时加载并构建 SSLContext',
    'event_queue': '队列容量与 worker 数在启动时创建',
    'dual_process': '双进程拓扑在启动时决定',
    'iipc': 'IPC 服务端在启动时创建',
    'console': '调试控制台监听在启动时创建',
    'observability': '探针/指标路由在 Web 应用创建时注册',
    'system': '系统级参数在启动时读取',
}


def register(section: str, fn):
    """注册某配置段的即时应用函数（fn(framework, section_cfg) -> list[str]）"""
    if callable(fn):
        _APPLIERS[str(section)] = fn


def unregister(section: str):
    _APPLIERS.pop(str(section), None)


# ── 内置 applier ────────────────────────────────────────────
def _apply_log(framework, cfg: dict) -> list:
    notes = []
    level_name = str((cfg or {}).get('level', 'INFO')).upper()
    level = getattr(logging, level_name, None)
    if isinstance(level, int):
        logging.getLogger().setLevel(level)
        notes.append(f'日志级别即时生效: {level_name}')
    else:
        notes.append(f'未知日志级别 {level_name}，未应用')

    # 输出格式（text / json）：换 formatter 即可，handler 与轮转不变
    fmt_cfg = (cfg or {}).get('format')
    if fmt_cfg is not None:
        try:
            from framework import jsonlog
            raw = str(fmt_cfg).strip().lower()
            applied = jsonlog.apply_format(raw)
            if raw != applied:
                notes.append(f'未知日志格式 {raw}，已回落到 {applied}')
            else:
                notes.append(f'日志格式即时生效: {applied}')
        except Exception as e:  # noqa: BLE001
            notes.append(f'日志格式应用失败: {e}')
    return notes


def _apply_framework(framework, cfg: dict) -> list:
    notes = []
    bb = (cfg or {}).get('blackbox')
    if isinstance(bb, dict) and bb:
        try:
            from framework.blackbox import configure
            kwargs = {}
            if 'enabled' in bb:
                kwargs['enabled'] = bool(bb['enabled'])
            if 'max_bytes' in bb:
                kwargs['max_bytes'] = int(bb['max_bytes'])
            if kwargs:
                configure(**kwargs)
                notes.append('内存黑匣子配置即时生效')
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[热应用] 黑匣子配置应用失败: {e}")
    return notes


def _apply_plugin(framework, cfg: dict) -> list:
    notes = []
    cfg = cfg or {}
    if 'hot_reload' in cfg:
        notes.append('插件热重载开关即时生效（心跳读取最新值）')
    if 'heartbeat_interval' in cfg:
        notes.append('heartbeat_interval 已保存，重启后按新间隔心跳')
    return notes


_APPLIERS.update({'log': _apply_log, 'framework': _apply_framework, 'plugin': _apply_plugin})


# 部分生效的段：能热应用的只是其中一部分键，其余仍要重启
PARTIAL_SECTIONS = {
    'security': lambda data: any(k != 'rate_limit' for k in (data or {})),
    'plugin': lambda data: 'heartbeat_interval' in (data or {}),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    """浅层合并 + 一层嵌套字典合并（配置段最多两层，够用且不引入深拷贝开销）"""
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def apply_section(framework, section: str, data: dict) -> dict:
    """把新配置应用到运行中框架，返回生效说明。

    调用方应先把新值写入 config.yaml（本函数只负责内存与运行时副作用）。
    """
    section = str(section)
    data = data if isinstance(data, dict) else {}

    current = framework.config.get(section)
    merged = _deep_merge(current if isinstance(current, dict) else {}, data)
    framework.config[section] = merged

    notes = []
    fn = _APPLIERS.get(section)
    if fn is not None:
        try:
            notes = list(fn(framework, merged) or [])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[热应用] 配置段 [{section}] 即时应用失败: {e}")
            notes = [f'即时应用失败: {e}']

    restart_reason = RESTART_ONLY_SECTIONS.get(section, '')
    restart_required = bool(restart_reason)
    if not restart_required:
        predicate = PARTIAL_SECTIONS.get(section)
        if predicate is not None and predicate(data):
            restart_required = True
            restart_reason = '该段只有部分字段支持即时生效，其余字段重启后生效'
        elif fn is None:
            # 未知段（插件自行新增的配置段等）：保守判定为需重启
            restart_required = True
            restart_reason = '该配置段未提供即时应用能力，重启后生效'

    return {
        'section': section,
        'live': bool(notes),
        'restart_required': restart_required,
        'restart_reason': restart_reason,
        'notes': notes,
    }
