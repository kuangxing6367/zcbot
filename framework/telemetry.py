"""遥测：默认关闭；开启后每半月上报一次系统信息与错误日志。

设计边界：
- 关闭时**不采集、不联网、不落盘**，只有 instance_id 在首次读取时生成；
- 上报目标完全由配置 `telemetry.endpoint` 决定，代码里没有硬编码地址；
- 携带框架生成的随机唯一标识（UUID4），不含管理员账号、令牌、IP 等身份信息；
- 任何网络/序列化异常都只写 debug 日志，绝不影响框架运行。
"""
import asyncio
import json
import logging
import os
import platform
import sys
import time
import urllib.request
import uuid

from framework.paths import state_dir

logger = logging.getLogger('zcbot')

_CHECK_INTERVAL = 3600  # 每小时检查一次是否到上报周期


def _state_path():
    return os.path.join(state_dir(), 'telemetry.json')


def _read_state():
    try:
        with open(_state_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data):
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as e:
        logger.debug('[telemetry] 状态落盘失败: %s', e)


def instance_id():
    """框架随机唯一标识；生成一次后落盘复用。"""
    state = _read_state()
    iid = state.get('instance_id')
    if not iid:
        iid = str(uuid.uuid4())
        state['instance_id'] = iid
        _write_state(state)
    return iid


def config_of(framework):
    cfg = (getattr(framework, 'config', None) or {}).get('telemetry') or {}
    return cfg if isinstance(cfg, dict) else {}


def enabled(framework):
    cfg = config_of(framework)
    return bool(cfg.get('enabled')) and bool(str(cfg.get('endpoint') or '').strip())


def status(framework):
    """供 WebUI 展示：是否开启、上次上报时间、实例标识。"""
    cfg = config_of(framework)
    state = _read_state()
    return {
        'enabled': enabled(framework),
        'configured': bool(cfg.get('enabled')),
        'endpoint_set': bool(str(cfg.get('endpoint') or '').strip()),
        'interval_days': int(cfg.get('interval_days', 15) or 15),
        'instance_id': instance_id(),
        'last_report': state.get('last_report'),
        'notice': ('' if cfg.get('enabled')
                   else '遥测数据已关闭。开启后可帮助定位稳定性问题；可在 config.yaml 的 telemetry 段配置。'),
    }


def _errors():
    try:
        from framework.log_broker import log_broker

        rows = log_broker.get_logs(level='ERROR', limit=20) or []
        return [str(r.get('message', ''))[:500] for r in rows]
    except Exception as e:
        logger.debug('[telemetry] 读取错误日志失败: %s', e)
        return []


def _version():
    """版本号来自文本 VERSION 文件，与面板 /api/version 同口径。"""
    try:
        from framework.paths import home_dir

        with open(os.path.join(home_dir(), 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        logger.debug('[telemetry] 读取 VERSION 失败: %s', e)
        return ''


def _plugin_count(framework):
    try:
        return len(framework.plugin_loader.get_loaded_plugins() or {})
    except Exception as e:
        logger.debug('[telemetry] 读取插件数失败: %s', e)
        return 0


def collect(framework):
    cfg = config_of(framework)
    days = max(1, int(cfg.get('interval_days', 15) or 15))
    return {
        'instance_id': instance_id(),
        'framework_version': _version(),
        'system': {
            'platform': platform.platform(),
            'python': sys.version.split()[0],
            'machine': platform.machine(),
        },
        'stats': {
            'plugins': _plugin_count(framework),
        },
        'errors': _errors(),
        'interval_days': days,
        'sent_at': int(time.time()),
    }


def _due():
    state = _read_state()
    last = state.get('last_report') or 0
    try:
        days = int(config_of(_FRAMEWORK).get('interval_days', 15) or 15)
    except Exception:
        days = 15
    return (time.time() - float(last)) >= max(1, days) * 86400


_FRAMEWORK = None


def start(framework):
    """在 asyncio 事件循环中启动周期检查；关闭时完全不建任务。"""
    global _FRAMEWORK
    _FRAMEWORK = framework
    if not enabled(framework):
        logger.info('[遥测] 已关闭（telemetry.enabled=false 或未配置 endpoint），不采集不上报')
        return None
    try:
        return asyncio.create_task(_loop(), name='telemetry')
    except RuntimeError:
        return None


async def _loop():
    while True:
        await asyncio.sleep(_CHECK_INTERVAL)
        if _FRAMEWORK is None or not enabled(_FRAMEWORK):
            continue
        if not _due():
            continue
        try:
            await _report(_FRAMEWORK)
        except Exception as e:
            logger.debug('[telemetry] 上报失败（不影响框架）: %s', e)


async def _report(framework):
    endpoint = str(config_of(framework).get('endpoint') or '').strip()
    body = json.dumps(collect(framework), ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        endpoint, data=body, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'zcbot-telemetry'})
    result = await asyncio.to_thread(
        lambda: urllib.request.urlopen(req, timeout=15).read())
    state = _read_state()
    state['last_report'] = int(time.time())
    _write_state(state)
    logger.info('[遥测] 已上报 %d 字节，响应 %d 字节', len(body), len(result or b''))


async def report_now(framework):
    """管理面手动上报一次（忽略周期）。"""
    if not enabled(framework):
        raise RuntimeError('遥测未开启或未配置 endpoint')
    await _report(framework)
    return status(framework)
