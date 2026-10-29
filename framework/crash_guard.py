# -*- coding: utf-8 -*-
"""
崩溃重启守卫（Crash-loop Guard）

解决“数据库损坏 / 启动期致命异常 → systemd/pm2 无限重启 → 永远起不来”的死循环：

- 启动期捕获致命异常时，在指定目录写 error.log（自动轮转 error.log.1..N）。
- 若已累计 ≥ max_restarts 份崩溃记录，直接拒绝启动并打印明确处置指引，
  把“无限重启死循环”变成“一次性明确失败”。
- 启动成功后调用 clear_crash_markers() 复位计数（健康运行即清零，避免误杀）。
- 可选：framework.crash.alert_command / alert_webhook 触发外部告警（邮件/Webhook）。

注意：本模块只提供原语，由进程入口（main.py amain）在合适的生命周期调用，
不侵入 Framework 类本身，因此单元测试直接构造 Framework 时不会触发。
"""
import logging
import os
import subprocess
import time
import traceback
import urllib.request

logger = logging.getLogger('zcbot')

_MAX_KEEP = 9  # 崩溃日志最多保留轮转副本数


def _resolve_path(cfg, root):
    path = (cfg or {}).get('path') or os.path.join(root, 'error.log')
    return path if os.path.isabs(path) else os.path.join(root, path)


def count_crash_markers(cfg, root):
    """统计 error.log 及其轮转副本数量（即累计崩溃次数）。"""
    path = _resolve_path(cfg, root)
    count = 1 if os.path.isfile(path) else 0
    i = 1
    while os.path.isfile(f"{path}.{i}"):
        count += 1
        i += 1
    return count


def should_abort_boot(cfg, root):
    """达到重启上限则返回 True（调用方应拒绝启动，打破死循环）。"""
    crash_cfg = (cfg or {}).get('framework', {}).get('crash', {}) or {}
    if not crash_cfg.get('enabled', True):
        return False
    max_restarts = int(crash_cfg.get('max_restarts', 5) or 5)
    return count_crash_markers(cfg, root) >= max_restarts


def write_crash_log(cfg, root, exc=None, extra=None):
    """写入崩溃日志并轮转旧副本（保留最近 _MAX_KEEP 份用于排障）。"""
    path = _resolve_path(cfg, root)
    try:
        # 轮转：error.log.N -> error.log.N+1 … error.log -> error.log.1
        for i in range(_MAX_KEEP - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.isfile(src):
                try:
                    if i + 1 > _MAX_KEEP:
                        os.remove(src)
                    else:
                        os.replace(src, dst)
                except OSError:
                    pass
        if os.path.isfile(path):
            try:
                os.replace(path, f"{path}.1")
            except OSError:
                pass
    except Exception:
        pass
    try:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        lines = [f"=== ZCBOT 启动崩溃 {ts} ==="]
        if extra:
            lines.append(f"context: {extra}")
        lines.append("traceback:\n" + _format_exc(exc))
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception as e:
        logger.error(f"写入崩溃日志失败: {e}")


def _format_exc(exc):
    """优先格式化显式传入的异常对象；无活跃 traceback 场景（如回调）也能落盘。"""
    if exc is not None:
        try:
            return ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception:
            return f"{type(exc).__name__}: {exc}"
    return traceback.format_exc()


def clear_crash_markers(cfg, root):
    """启动成功后清除崩溃计数（健康运行即复位，避免误杀正常重启）。"""
    path = _resolve_path(cfg, root)
    try:
        if os.path.isfile(path):
            os.remove(path)
        i = 1
        while os.path.isfile(f"{path}.{i}"):
            try:
                os.remove(f"{path}.{i}")
            except OSError:
                pass
            i += 1
    except Exception:
        pass


def fire_alert(cfg, exc=None):
    """可选外部告警：alert_command（shell）或 alert_webhook（POST 文本）。"""
    crash_cfg = (cfg or {}).get('framework', {}).get('crash', {}) or {}
    cmd = crash_cfg.get('alert_command')
    if cmd:
        try:
            subprocess.run(cmd, shell=True, timeout=30, capture_output=True,
                           env={**os.environ, 'ZCBOT_CRASH': '1'})
        except Exception as e:
            logger.warning(f"崩溃告警命令执行失败: {e}")
    hook = crash_cfg.get('alert_webhook')
    if hook:
        try:
            data = (traceback.format_exc() if exc else 'ZCBOT crash').encode('utf-8')
            req = urllib.request.Request(hook, data=data, method='POST')
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            logger.warning(f"崩溃告警 Webhook 失败: {e}")
