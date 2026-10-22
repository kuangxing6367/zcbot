# -*- coding: utf-8 -*-
"""IIPC 客户端事件分发「不再静默吞异常」回归测试。

此前 _dispatch_loop 用 `except Exception: pass` 把事件处理器异常（含整个
事件分发入口 _on_event 在中间件/事件构造阶段炸掉）无声吞掉，插件「收不到
消息」却无任何线索。现改为 stderr 留痕（_dispatch_one）。本测试验证：
  1) 处理器抛异常时，错误信息出现在 stderr（可见）；
  2) 正常处理器不触发任何 stderr 噪声；
  3) 损坏 JSON 行跳过时打印告警（_read_loop_inner）。
"""
import contextlib
import io
import os
import sys

SDK_DIR = os.path.join(os.path.dirname(__file__), "..", "framework", "iipc", "py_sdk")
SDK_DIR = os.path.abspath(SDK_DIR)
if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

import iipc_client  # noqa: E402


def _make_client():
    """绕过 socket 构造（不连内核），只取方法面。"""
    c = iipc_client.IIPCClient.__new__(iipc_client.IIPCClient)
    c._events = {}
    c._pending = {}
    c._methods = {}
    return c


def test_dispatch_one_logs_handler_exception():
    c = _make_client()
    c._events["event"] = [lambda p: (_ for _ in ()).throw(RuntimeError("handler 炸了"))]

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        c._dispatch_one("event", {"x": 1})

    out = buf.getvalue()
    assert "handler 炸了" in out, "处理器异常必须可见"
    assert "事件处理器异常" in out, "应打印事件处理器异常标记"


def test_dispatch_one_normal_no_noise():
    c = _make_client()
    seen = []
    c._events["event"] = [lambda p: seen.append(p)]

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        c._dispatch_one("event", {"x": 1})

    assert seen == [{"x": 1}], "正常处理器应被执行"
    assert buf.getvalue() == "", "正常分发不应产生 stderr 噪声"


def test_dispatch_one_no_subscribers_safe():
    c = _make_client()  # 无 "missing" 频道订阅
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        c._dispatch_one("missing", {"x": 1})
    assert buf.getvalue() == "", "无订阅者不应报错"


def test_read_loop_inner_reports_corrupt_line():
    c = _make_client()
    import io as _io

    # 构造一个含损坏行的「文件」：第一行合法 req，第二行非法 JSON
    lines = _io.StringIO('{"t":"res","id":1,"result":{}}\n{bad json\n')
    c._f = lines
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        c._read_loop_inner()
    assert "跳过损坏行" in buf.getvalue(), "损坏 JSON 行必须告警"


if __name__ == "__main__":
    import traceback
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(funcs) - failed, len(funcs)))
    sys.exit(1 if failed else 0)
