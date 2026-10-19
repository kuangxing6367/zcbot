# -*- coding: utf-8 -*-
"""中间件管线验收（tools/middleware_check.py）

进程内确定性验收：直接驱动 sdk._on_event 与 ctx._flush，用假客户端捕获出站，
验证多阶段中间件管线（pre / post / send / error）的真实行为。

覆盖：
  1) pre 变换：改写 event，下游 command 看到改写后的值
  2) pre 短路：返回 False 中止分发（command / on / post 均不执行）
  3) post：分发完成后执行（记录最终 event）
  4) send 改写：出站参数被中间件改写后发出
  5) send 拦截：返回 None/False 的条目被丢弃（不发出）
  6) error：handler 抛异常时，error 中间件以 (event, exc, ctx) 收到

退出码 0 = 全部通过。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IIPC = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(IIPC, "py_sdk"))
sys.path.insert(0, HERE)

import sdk  # noqa: E402


class FakeClient:
    """假 IIPC 客户端：捕获 call，fire/close 为空操作。"""

    def __init__(self):
        self.calls = []

    def call(self, method, params, timeout=5):
        self.calls.append((method, params))
        return {}

    def fire(self, method, params):
        self.calls.append(("fire:" + method, params))

    def close(self):
        pass


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, ("-- " + detail) if detail else ""))
    return ok


def fresh_ctx():
    """构造一个干净的 PluginContext + 重置相关注册表。"""
    client = FakeClient()
    ctx = sdk.PluginContext(client, "mw_test")
    sdk._current_ctx = ctx
    # 清空累积的注册项，保证每个场景独立
    with sdk._registry_lock:
        sdk._registry["command"] = []
        sdk._registry["on"] = {}
        sdk._registry["hook"] = {}
        sdk._registry["raw_message"] = []
        for st in ("pre", "post", "send", "error"):
            sdk._registry["middleware"][st] = []
    return ctx, client


def inject_message(message, user_id=10001, group_id=1, self_id="mainpy"):
    sdk._on_event({
        "type": "message",
        "data": {
            "message": message, "user_id": user_id, "group_id": group_id,
            "self_id": self_id, "message_type": "group" if group_id else "private",
        },
    })


def test_pre_transform():
    ctx, _ = fresh_ctx()
    cap = {}

    @ctx.middleware("pre", priority=10)
    def suffix(ev, c):
        # pre 在命令匹配前生效：追加后缀（不破坏 /echo 前缀匹配）
        ev.message = ev.message + "!!"
        cap["pre_ran"] = True

    @ctx.command("/echo")
    def echo(ev, match):
        cap["cmd_msg"] = ev.message

    inject_message("/echo hello")
    ok = cap.get("pre_ran") and cap.get("cmd_msg") == "/echo hello!!"
    check("pre 变换：下游 command 看到改写后消息", ok,
          "pre_ran=%s cmd_msg=%s" % (cap.get("pre_ran"), cap.get("cmd_msg")))


def test_pre_shortcircuit():
    ctx, _ = fresh_ctx()
    cap = {}

    @ctx.middleware("pre", priority=5)
    def block(ev, c):
        cap["block_ran"] = True
        return False  # 短路

    @ctx.middleware("pre", priority=50)
    def never(ev, c):
        cap["never_ran"] = True

    @ctx.command("/x")
    def x(ev, match):
        cap["cmd_ran"] = True

    inject_message("/x")
    ok = cap.get("block_ran") and not cap.get("never_ran") and not cap.get("cmd_ran")
    check("pre 短路：False 中止后续中间件 + 分发", ok,
          str(cap))


def test_post():
    ctx, _ = fresh_ctx()
    cap = {}

    @ctx.command("/p")
    def p(ev, match):
        cap["cmd_ran"] = True

    @ctx.middleware("post", priority=10)
    def after(ev, c):
        cap["post_msg"] = ev.message
        cap["post_after_cmd"] = cap.get("cmd_ran", False)

    inject_message("/p")
    ok = cap.get("cmd_ran") and cap.get("post_after_cmd") is True \
        and cap.get("post_msg") == "/p"
    check("post：分发完成后执行且看到最终 event", ok, str(cap))


def test_send_rewrite():
    ctx, client = fresh_ctx()
    cap = {}

    @ctx.middleware("send", priority=10)
    def tag(params, c):
        p = dict(params)
        p["message"] = (p.get("message") or "") + " [tag]"
        return p

    ctx.send_msg(group_id=2, message="hi")
    ctx._flush()
    ok = len(client.calls) == 1 and client.calls[0][0] == "msg.send" \
        and client.calls[0][1].get("message") == "hi [tag]"
    check("send 改写：出站参数被中间件改写", ok, str(client.calls))


def test_send_block():
    ctx, client = fresh_ctx()

    @ctx.middleware("send", priority=5)
    def block(params, c):
        return False  # 拦截

    @ctx.middleware("send", priority=50)
    def rewrite(params, c):
        p = dict(params)
        p["message"] = "rewritten"
        return p

    ctx.send_msg(group_id=2, message="secret")
    ctx._flush()
    ok = len(client.calls) == 0  # 被拦截，完全不发出
    check("send 拦截：返回 False 的条目被丢弃", ok, str(client.calls))


def test_error():
    ctx, _ = fresh_ctx()
    cap = {}

    @ctx.middleware("error", priority=10)
    def on_err(event, exc, c):
        cap["err_type"] = type(exc).__name__
        cap["err_evmsg"] = getattr(event, "message", None)

    @ctx.command("/boom")
    def boom(ev, match):
        raise RuntimeError("kaboom")

    inject_message("/boom")
    # _invoke 阻塞等线程池完成，故此处 error 中间件已同步执行
    ok = cap.get("err_type") == "RuntimeError" and cap.get("err_evmsg") == "/boom"
    check("error：handler 异常时收到 (event, exc, ctx)", ok, str(cap))


def test_zero_cost_no_mw():
    """未注册任何中间件时，分发行为与原先一致（命令仍正常执行）。"""
    ctx, _ = fresh_ctx()
    cap = {}

    @ctx.command("/plain")
    def plain(ev, match):
        cap["ran"] = ev.message

    inject_message("/plain")
    ok = cap.get("ran") == "/plain"
    check("零成本：无中间件时命令行为不变", ok, str(cap))


def main():
    test_pre_transform()
    test_pre_shortcircuit()
    test_post()
    test_send_rewrite()
    test_send_block()
    test_error()
    test_zero_cost_no_mw()

    ok = sum(1 for _, v, _ in results if v)
    bad = [n for n, v, _ in results if not v]
    print("\n==== 中间件管线验收: %d/%d 通过 ====" % (ok, len(results)))
    if bad:
        print("失败项: %s" % ", ".join(bad))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
