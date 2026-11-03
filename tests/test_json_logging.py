# -*- coding: utf-8 -*-
"""结构化日志（log.format=json）行为测试。

验证的是真实输出行为，不是源码里有字符串：

  · JsonFormatter 输出单行 JSON，字段齐全（ts/level/logger/msg/pid/thread/src）
  · extra=fields(...) 的附加字段进 fields 对象，不污染 msg
  · 异常堆栈进 exc 字段
  · apply_format('json') 替换真实 handler 的 formatter，切回 text 时**还原同一个对象**
  · 桥接到 WebUI 日志中心的 handler 不被替换（否则后台日志页显示成 JSON 字符串）
  · 非法格式值回落 text
  · 真实 Framework 按 log.format=json 初始化后，控制台/文件 handler 确实变成 JSON

运行：python tests/test_json_logging.py   或   pytest tests/test_json_logging.py
"""
import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework import jsonlog

_FAILED = []


def check(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        _FAILED.append(msg)


class _FakeBridgeHandler(logging.Handler):
    """模拟 FrameworkLogHandler：自己格式化，不该被套 JSON formatter"""

    LEVEL_MAP = {logging.INFO: 'INFO'}

    def __init__(self):
        super().__init__()
        self._broker = object()

    def emit(self, record):
        pass


def _fresh_logger(name, stream):
    """独立 logger + 文本 handler，避免污染根 logger"""
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    lg.addHandler(h)
    return lg, h


def test_formatter_fields():
    print("[1] JsonFormatter 字段与单行输出")
    stream = io.StringIO()
    lg, handler = _fresh_logger('zcbot.jsonlog.t1', stream)
    handler.setFormatter(jsonlog.JsonFormatter())
    lg.info("用户登录失败: %s", 1001)

    raw = stream.getvalue()
    check(raw.count('\n') == 1, f"只输出一行（实际 {raw.count(chr(10))} 个换行）")
    obj = json.loads(raw)
    for key in ('ts', 'level', 'logger', 'msg', 'pid', 'thread', 'src', 'service'):
        check(key in obj, f"含字段 {key}")
    check(obj['level'] == 'INFO', "level 正确")
    check(obj['msg'] == '用户登录失败: 1001', f"参数已格式化（{obj['msg']}）")
    check(obj['logger'] == 'zcbot.jsonlog.t1', "logger 名正确")
    check(':' in str(obj['src']), f"src 带行号（{obj['src']}）")
    # 时区断言必须与时区无关：CI 跑在 UTC，本机是 UTC+8，不能写死偏移量
    try:
        _dt = datetime.fromisoformat(obj['ts'])
        tz_ok = _dt.tzinfo is not None
    except ValueError:
        tz_ok = False
    check(tz_ok, f"时间戳可解析且带时区（{obj['ts']}）")


def test_extra_fields():
    print("[2] extra=fields(...) 进 fields 对象")
    stream = io.StringIO()
    lg, handler = _fresh_logger('zcbot.jsonlog.t2', stream)
    handler.setFormatter(jsonlog.JsonFormatter())
    lg.warning("插件加载失败", extra=jsonlog.fields(plugin='foo', retry=2))

    obj = json.loads(stream.getvalue())
    check(obj['msg'] == '插件加载失败', "msg 未被附加字段污染")
    check(obj.get('fields') == {'plugin': 'foo', 'retry': 2},
          f"fields 内容正确（{obj.get('fields')}）")

    # 不带 extra 时不出现 fields 键，避免下游误判
    stream.truncate(0)
    stream.seek(0)
    lg.info("普通消息")
    check('fields' not in json.loads(stream.getvalue()), "无 extra 时无 fields 键")


def test_exception():
    print("[3] 异常堆栈进 exc")
    stream = io.StringIO()
    lg, handler = _fresh_logger('zcbot.jsonlog.t3', stream)
    handler.setFormatter(jsonlog.JsonFormatter())
    try:
        raise ValueError("boom-json-log")
    except ValueError:
        lg.exception("处理失败")
    obj = json.loads(stream.getvalue())
    check('exc' in obj and 'ValueError' in obj['exc'], "exc 含异常类型")
    check('boom-json-log' in obj['exc'], "exc 含异常消息")


def test_apply_and_restore():
    print("[4] apply_format 切换与还原")
    stream = io.StringIO()
    lg, handler = _fresh_logger('zcbot.jsonlog.t4', stream)
    original = handler.formatter

    applied = jsonlog.apply_format('json', root=lg)
    check(applied == 'json', f"返回生效格式（{applied}）")
    check(isinstance(handler.formatter, jsonlog.JsonFormatter), "formatter 已换成 JSON")
    check(jsonlog.current_format(root=lg) == 'json', "current_format 报告 json")

    lg.info("切换后")
    obj = json.loads(stream.getvalue())
    check(obj['msg'] == '切换后', "切换后输出确实是 JSON")

    applied = jsonlog.apply_format('text', root=lg)
    check(applied == 'text', "切回 text")
    check(handler.formatter is original, "还原为同一个 formatter 对象（不是新建）")
    check(jsonlog.current_format(root=lg) == 'text', "current_format 报告 text")

    stream.truncate(0)
    stream.seek(0)
    lg.info("切回后")
    check(stream.getvalue().strip() == '[INFO] 切回后', f"恢复文本格式（{stream.getvalue().strip()}）")


def test_bridge_handler_untouched():
    print("[5] 桥接 handler 不被替换")
    stream = io.StringIO()
    lg, handler = _fresh_logger('zcbot.jsonlog.t5', stream)
    bridge = _FakeBridgeHandler()
    bridge_fmt = logging.Formatter('%(message)s')
    bridge.setFormatter(bridge_fmt)
    lg.addHandler(bridge)

    jsonlog.apply_format('json', root=lg)
    check(isinstance(handler.formatter, jsonlog.JsonFormatter), "普通 handler 已切换")
    check(bridge.formatter is bridge_fmt, "桥接 handler 的 formatter 保持不变")
    check(not getattr(bridge, '_zcbot_json_on', False), "桥接 handler 未被打标记")

    jsonlog.apply_format('text', root=lg)


def test_invalid_falls_back():
    print("[6] 非法格式回落 text")
    stream = io.StringIO()
    lg, handler = _fresh_logger('zcbot.jsonlog.t6', stream)
    jsonlog.apply_format('json', root=lg)
    applied = jsonlog.apply_format('yaml', root=lg)
    check(applied == 'text', f"非法值回落 text（{applied}）")
    check(not isinstance(handler.formatter, jsonlog.JsonFormatter), "确实回到文本 formatter")
    applied = jsonlog.apply_format(None, root=lg)
    check(applied == 'text', "None 也回落 text")


def test_framework_wiring():
    print("[7] 真实 Framework 按 log.format=json 接线")
    from framework.core import Framework

    root = logging.getLogger()
    before = list(root.handlers)
    tmp = tempfile.mkdtemp(prefix='zcbot_jsonlog_')
    fw = None
    try:
        cfg = os.path.join(tmp, 'config.yaml')
        db = os.path.join(tmp, 'j.db').replace('\\', '/')
        log_path = os.path.join(tmp, 'test_json.log').replace('\\', '/')
        with open(cfg, 'w', encoding='utf-8') as f:
            f.write(
                "database:\n  type: sqlite\n  path: {db}\n"
                "log:\n  level: INFO\n  format: json\n"
                "  file: {log}\n"
                "core_plugins:\n"
                "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
                "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
                "  telegram: false\n  discord: false\n  session: false\n"
                "  scheduler: false\n  image_renderer: false\n".format(db=db, log=log_path)
            )
        fw = Framework(config_path=cfg, role='standard')
        added = [h for h in root.handlers if h not in before]
        switched = [h for h in added if getattr(h, '_zcbot_json_on', False)]
        check(len(switched) >= 1, f"新增 handler 中有 JSON 模式（{len(switched)} 个）")

        bridge = [h for h in added if jsonlog._is_bridge_handler(h)]
        check(all(not getattr(h, '_zcbot_json_on', False) for h in bridge),
              f"桥接 handler 未被切换（{len(bridge)} 个）")

        # 真实写一条到文件，验证落盘的就是 JSON 行
        logging.getLogger('zcbot').info("接线验证", extra=jsonlog.fields(stage='wiring'))
        for h in root.handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001
                pass
        log_path = os.path.join(tmp, 'test_json.log')
        check(os.path.isfile(log_path), f"日志文件已创建（{log_path}）")
        if os.path.isfile(log_path):
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            target = [ln for ln in lines if '接线验证' in ln]
            check(bool(target), "文件里有目标日志行")
            if target:
                obj = json.loads(target[-1])
                check(obj.get('fields') == {'stage': 'wiring'},
                      f"落盘 JSON 含结构化字段（{obj.get('fields')}）")
    finally:
        for h in [h for h in root.handlers if h not in before]:
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            root.removeHandler(h)
        if fw is not None:
            try:
                fw._db_executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        jsonlog.apply_format('text', root=root)


def main():
    for fn in (test_formatter_fields, test_extra_fields, test_exception,
               test_apply_and_restore, test_bridge_handler_untouched,
               test_invalid_falls_back, test_framework_wiring):
        fn()
    print()
    if _FAILED:
        print(f"FAILED {len(_FAILED)} 项：")
        for m in _FAILED:
            print("  -", m)
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
