"""
崩溃重启守卫（Crash-loop Guard）

解决“数据库损坏 / 启动期致命异常 → systemd/pm2 无限重启 → 永远起不来”的死循环：

- 启动期捕获致命异常时，在指定目录写 error.log（自动轮转 error.log.1..N）。
- 若已累计 ≥ max_restarts 份崩溃记录，直接拒绝启动并打印明确处置指引，
  把“无限重启死循环”变成“一次性明确失败”。
- 启动成功后调用 clear_crash_markers() 复位计数（健康运行即清零，避免误杀）。
- 可选：framework.crash.alert_command / alert_webhook 触发外部告警（邮件/Webhook）。

注意：本模块只提供原语，由进程入口（startup.py → framework/bootstrap/app.py 的 amain）在合适的生命周期调用，
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


def _crash_section(cfg):
    """从整份配置里取出 framework.crash，兼容直接传 crash 段的旧调用。"""
    if not isinstance(cfg, dict):
        return {}
    framework = cfg.get('framework')
    if isinstance(framework, dict):
        crash = framework.get('crash')
        if isinstance(crash, dict):
            return crash
    direct = cfg.get('crash')
    if isinstance(direct, dict):
        return direct
    if any(k in cfg for k in ('path', 'max_restarts', 'enabled')):
        return cfg
    return {}


def _resolve_path(cfg, root):
    path = _crash_section(cfg).get('path') or os.path.join(root, 'error.log')
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
    # 【踩坑保佑】v1.10.0 崩溃重启守卫
    # 坑：无限重启死循环改为一次性明确失败  →  should_abort_boot
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

    crash_cfg = _crash_section(cfg)
    if not crash_cfg.get('enabled', True):
        return False
    max_restarts = int(crash_cfg.get('max_restarts', 5) or 5)
    return count_crash_markers(cfg, root) >= max_restarts


def write_crash_log(cfg, root, exc=None, extra=None):
    """写入崩溃日志并轮转旧副本，返回落盘路径（失败返回空串）。"""
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
    except Exception as _exc:
        logger.debug('[write_crash_log] 捕获异常后按降级路径继续: %s', _exc)
    try:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        lines = [f"=== ZCBOT 启动崩溃 {ts} ==="]
        if extra:
            if isinstance(extra, dict):
                # 结构化上下文逐行展开。第一项若是处置指引，直接顶到最前面——
                # 用户看 error.log 是来找"出了什么事、我该怎么办"，不是读堆栈。
                for key, val in extra.items():
                    lines.append(f"[{key}]")
                    lines.append(str(val) if val not in (None, '') else '(空)')
            else:
                lines.append(f"context: {extra}")
        trace = _format_exc(exc)
        # 主动退出（如数据库损坏）没有异常栈，不必留一行 NoneType: None 让人困惑
        if trace.strip() and trace.strip() != 'NoneType: None':
            lines.append("traceback:\n" + trace)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')
        # 页脚追加失败不能影响上面的崩溃日志：求助时第一眼就能看到机器环境，
        # 但目录不可写时也必须保住 traceback 本身。
        try:
            from framework import environment

            with open(path, 'a', encoding='utf-8', newline='') as f:
                f.write(environment.format_block(
                    environment.snapshot(), title='[系统环境]') + '\n')
        except Exception as e:
            logger.debug(f'崩溃日志环境页脚写入失败（忽略）: {e}')
        return path
    except Exception as e:
        logger.error(f"写入崩溃日志失败: {e}")
        return ''


def _format_exc(exc):
    """优先格式化显式传入的异常对象；无活跃 traceback 场景（如回调）也能落盘。"""
    if exc is not None:
        try:
            return ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception as _exc:
            logger.debug('[_format_exc] 捕获异常后按降级路径继续: %s', _exc)
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
    except Exception as _exc:
        logger.debug('[clear_crash_markers] 捕获异常后按降级路径继续: %s', _exc)


def fire_alert(cfg, exc=None):
    """可选外部告警：alert_command（shell）或 alert_webhook（POST 文本）。"""
    crash_cfg = _crash_section(cfg)
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
