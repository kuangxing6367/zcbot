# -*- coding: utf-8 -*-
"""IIPC 统一日志出口 —— 对齐原 zcbot 框架日志体系。

原框架契约（config.yaml `log:` 段 + framework/log_broker.py）：
  - 标准 Python logging（root 'zcbot' logger），级别 DEBUG/INFO/WARNING/ERROR；
  - 文件落盘 `log.file`（缺省 data/logs/zcbot.log），留空则只输出控制台；
  - LogBroker 环形缓存 + SSE（WebUI 日志页）；IpcLogHandler 批量 notify('log') 跨进程合并。

IIPC 侧落地（本模块）：
  - launcher 是全栈日志总出口：Go 内核 / Rust 适配器 / Python 插件的日志全部汇到
    本进程，按统一格式 `时间 级别 [组件] 消息` 输出 + 落盘到**同一个** log.file
    （tag 区分组件，不做第二套日志文件）；
  - 插件侧日志经 IIPC `log.batch` → 内核 → stdout → launcher 出口（见 py_sdk/sdk.py
    的 IIPCLogger 与 go_kernel/handlers.go 的 MethodLogBatch）；
  - 级别过滤 / 落盘路径直接复用 config.yaml，不另立配置；
  - WebUI/SSE 深接（LogBroker 桥接）列为待联调项，见 iipc/README.md。

防御：config 读取失败用默认值；日志目录不存在则创建；写文件失败降级仅控制台；
日志系统任何异常不得影响主流程（launcher 的 pump 线程同样有兜底）。
"""
import logging
import os
import sys
import threading

_LOCK = threading.Lock()
_LOGGER = None
_FILE_READY = False


def _load_log_config(project_root):
    """读 config.yaml 的 log: 段，失败返回默认值（与原框架默认行为一致）。"""
    cfg = {}
    try:
        import yaml
        path = os.path.join(project_root, "config.yaml")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    log_cfg = cfg.get("log") or {}
    level = str(log_cfg.get("level", "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        level = "INFO"
    return {
        "level": level,
        # 缺省与原框架一致：data/logs/zcbot.log；空串 = 只输出控制台
        "file": str(log_cfg.get("file", "data/logs/zcbot.log") or ""),
    }


def setup_logging(project_root):
    """初始化 IIPC 统一日志（幂等）。返回 logging.Logger。"""
    global _LOGGER
    with _LOCK:
        if _LOGGER is not None:
            return _LOGGER
        conf = _load_log_config(project_root)
        logger = logging.getLogger("zcbot.iipc")
        logger.setLevel(getattr(logging, conf["level"], logging.INFO))
        logger.propagate = False
        fmt_str = "%(asctime)s %(levelname)s [%(tag)s] %(message)s"
        datefmt = "%Y-%m-%d %H:%M:%S"

        # 控制台（stdout，UTF-8）
        try:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(_TagFormatter(fmt_str, datefmt))
            logger.addHandler(sh)
        except Exception as e:
            # 不能再静默：日志系统静默失败 = 全程瞎子
            print("[iipc_log] 控制台 handler 初始化失败: %r" % e, file=sys.stderr, flush=True)

        # 文件（与 zcbot 框架同一个 log.file；失败降级仅控制台）
        if conf["file"]:
            path = conf["file"]
            if not os.path.isabs(path):
                path = os.path.join(project_root, path)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fh = logging.FileHandler(path, encoding="utf-8")
                fh.setFormatter(_TagFormatter(fmt_str, datefmt))
                logger.addHandler(fh)
                global _FILE_READY
                _FILE_READY = True
            except Exception as e:
                # 文件不可用（只读/权限）时仅控制台，不炸启动流程，但要留痕
                print("[iipc_log] 文件 handler 初始化失败（降级仅控制台）: %r" % e,
                      file=sys.stderr, flush=True)

        _LOGGER = logger
        return logger


class _TagFormatter(logging.Formatter):
    """带默认 tag 的 Formatter：record 缺 tag 字段时补 'iipc'。"""

    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)

    def format(self, record):
        if not hasattr(record, "tag"):
            record.tag = "iipc"
        return super().format(record)


def emit(tag, level, msg):
    """统一日志入口（launcher / 内核泵 / 适配器泵共用）。

    :param tag: 组件名（launcher/kernel/rust/plugin xxx/pm/...）
    :param level: DEBUG/INFO/WARNING/ERROR（容忍大小写与别名）
    :param msg: 已格式化的消息文本
    """
    if _LOGGER is None:
        # 未初始化（极早期）：直接 stdout，格式对齐
        lv = str(level).upper()
        lv = "WARNING" if lv in ("WARN",) else lv
        if lv not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            lv = "INFO"
        print("%s %s [%s] %s" % (_now(), lv, tag, msg), flush=True)
        return
    lv = str(level).upper()
    if lv == "WARN":
        lv = "WARNING"
    if lv not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        lv = "INFO"
    try:
        _LOGGER.log(getattr(logging, lv), msg, extra={"tag": tag})
    except Exception:
        try:
            print("%s %s [%s] %s" % (_now(), lv, tag, msg), flush=True)
        except Exception:
            pass  # 日志系统绝不能影响主流程


def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def file_enabled():
    return _FILE_READY
