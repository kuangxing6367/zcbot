"""
结构化日志（JSON 行格式）
==========================
默认输出是给人看的文本（`[12:00:00] INFO 消息`），接 Loki / ELK / Vector 这类
采集器时不好解析。`log.format: json` 打开后，控制台与日志文件改为**每行一个
JSON 对象**（JSON Lines），字段固定，采集端可直接按字段检索。

设计约束
--------
- **只换 formatter，不换 handler**：日志文件轮转、级别过滤、合并去重全部不变。
- **桥接到 WebUI 日志中心的 handler 不受影响**：它自己从 LogRecord 取消息，
  若被套上 JSON formatter，后台日志页会显示成一堆 JSON 字符串。
- 支持运行时切换（`framework/hotapply.py` 的 `log` 段 applier 调用 `apply_format`），
  切换前会记住原始 formatter，切回 text 时原样还原。
- 附加结构化字段用 `extra=fields(k=v)`，输出到 `fields` 对象；不污染 `msg`。

用法
----
    logger.info("用户登录失败: %s", uid, extra=fields(user=uid, ip=addr))

输出（json 模式）：

    {"ts":"2026-11-04T00:12:33.123+08:00","level":"INFO","logger":"zcbot",
     "msg":"用户登录失败: 1001","pid":4231,"thread":"MainThread",
     "src":"auth.py:88","fields":{"user":1001,"ip":"1.2.3.4"}}
"""
import json
import logging
from datetime import datetime

FORMAT_TEXT = 'text'
FORMAT_JSON = 'json'
VALID_FORMATS = (FORMAT_TEXT, FORMAT_JSON)

# 挂在 LogRecord 上的附加字段容器名（避开 logging 内置属性）
EXTRA_KEY = 'zcbot_fields'


def fields(**kwargs) -> dict:
    """构造 extra 参数：`logger.info('...', extra=fields(user=1))`"""
    return {EXTRA_KEY: kwargs}


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON（JSON Lines）"""

    def __init__(self, service: str = 'zcbot'):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        # 本地时间带时区偏移，跨时区排障不用猜
        try:
            ts = datetime.fromtimestamp(record.created).astimezone().isoformat(
                timespec='milliseconds')
        except (OSError, OverflowError, ValueError):
            ts = ''

        payload = {
            'ts': ts,
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
            'pid': record.process,
            'thread': record.threadName or '',
            'src': '%s:%s' % (record.filename, record.lineno),
        }
        if self.service:
            payload['service'] = self.service

        # 事件关联 ID（由 _EventIdFilter 注入，无事件上下文时为空串）
        _eid = getattr(record, 'zcbot_eid', '')
        if _eid:
            payload['eid'] = _eid

        extra = getattr(record, EXTRA_KEY, None)
        if isinstance(extra, dict) and extra:
            payload['fields'] = extra

        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload['exc'] = record.exc_text
        if record.stack_info:
            payload['stack'] = self.formatStack(record.stack_info)

        # default=str 兜底不可序列化对象，日志绝不能因为序列化失败而丢
        return json.dumps(payload, ensure_ascii=False, default=str)


JSON_FORMATTER = JsonFormatter()


def _is_bridge_handler(handler) -> bool:
    """判断是否是「自己格式化」的桥接 handler（LogBroker），它不该被替换 formatter"""
    return hasattr(handler, '_broker') and hasattr(handler, 'LEVEL_MAP')


def apply_format(fmt: str, root: logging.Logger = None) -> str:
    """把根 logger 上可切换的 handler 统一成指定格式，返回实际生效的格式名。

    text/json 之外的值一律按 text 处理（配置写错不至于让日志消失）。
    """
    want = str(fmt or FORMAT_TEXT).strip().lower()
    if want not in VALID_FORMATS:
        want = FORMAT_TEXT
    target = root or logging.getLogger()
    enabled = want == FORMAT_JSON

    for handler in list(target.handlers):
        if not hasattr(handler, 'setFormatter') or _is_bridge_handler(handler):
            continue
        if enabled:
            if getattr(handler, '_zcbot_json_on', False):
                continue
            # 首次切换前记住原始 formatter，切回时原样还原
            if not hasattr(handler, '_zcbot_orig_formatter'):
                handler._zcbot_orig_formatter = handler.formatter
            handler.setFormatter(JSON_FORMATTER)
            handler._zcbot_json_on = True
        else:
            if not getattr(handler, '_zcbot_json_on', False):
                continue
            handler.setFormatter(getattr(handler, '_zcbot_orig_formatter', None))
            handler._zcbot_json_on = False
    return want


def current_format(root: logging.Logger = None) -> str:
    """当前是否处于 JSON 模式（以任一可切换 handler 的状态为准）"""
    target = root or logging.getLogger()
    for handler in target.handlers:
        if _is_bridge_handler(handler):
            continue
        if getattr(handler, '_zcbot_json_on', False):
            return FORMAT_JSON
    return FORMAT_TEXT
