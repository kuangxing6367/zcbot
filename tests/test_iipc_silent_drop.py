# -*- coding: utf-8 -*-
"""IIPC SDK「吞消息 / 静默降级」修复的回归测试。

覆盖三处历史静默丢弃点：
  1) 出站消息批量 flush 失败 —— 原实现直接丢弃，现改为回队列重试 + 退避 + 有界；
  2) 插件日志批次发送失败 —— 原实现 `pass`（静默丢弃），现计数 + 抑制式告警；
  3) 权限/身份查询失败降级 —— 原实现无声降权（管理员被静默降为 member），
     现保留安全降级返回值但留下可见痕迹。

不依赖真实内核：用内存假 client 注入失败，验证可观测性与不丢消息。
"""
import os
import sys
import time

# SDK 以插件侧目录为根运行；测试里把它和依赖（iipc_client/event/ext）加进路径
SDK_DIR = os.path.join(os.path.dirname(__file__), "..", "framework", "iipc", "py_sdk")
SDK_DIR = os.path.abspath(SDK_DIR)
for p in (SDK_DIR, os.path.join(SDK_DIR, "deps")):
    if p not in sys.path:
        sys.path.insert(0, p)

import sdk  # noqa: E402


# 关掉后台线程（flush 循环 / async 循环），让本测试完全确定性、不依赖 5ms 调度。
sdk.PluginContext._start_flush_loop = lambda self: None
sdk.PluginContext._start_async_loop = lambda self: None


class _FakeClient:
    """内存版 IIPCClient：可注入 call / fire 失败，记录调用。"""

    def __init__(self):
        self.calls = []
        self.fire_calls = []
        self.call_fail = False
        self.fire_fail = False

    def call(self, action, payload=None):
        self.calls.append((action, payload))
        if self.call_fail:
            raise RuntimeError("send failed")

    def fire(self, action, payload=None):
        self.fire_calls.append((action, payload))
        if self.fire_fail:
            raise RuntimeError("fire failed")


def _ctx(client=None):
    """构造 PluginContext（后台线程已禁用），并清空 send 中间件保证确定性。"""
    if client is None:
        client = _FakeClient()
    ctx = sdk.PluginContext(client, "test_plugin")
    # 清掉可能残留的 send 中间件，避免 batch 被静默拦截
    sdk._registry["middleware"]["send"] = []
    return ctx, client


# ───────────────────────── 1) 出站消息 flush 失败不再丢 ─────────────────────────

def test_flush_failure_requeues():
    client = _FakeClient()
    client.call_fail = True
    ctx, client = _ctx(client)
    sdk._MSG_DROP.count = 0
    ctx._queue.append({"message": "hi", "group_id": "1"})
    ctx._flush()
    # 失败批次应回队列，消息未丢失
    assert len(ctx._queue) == 1, "失败批次应回队列，不应丢弃"
    assert sdk._MSG_DROP.count >= 1, "失败应被计数（可见）"


def test_flush_success_clears_queue():
    client = _FakeClient()
    ctx, client = _ctx(client)
    sdk._MSG_DROP.count = 0
    ctx._queue.append({"message": "hi", "group_id": "1"})
    ctx._flush()
    assert len(ctx._queue) == 0, "成功应清空队列"
    assert client.calls, "应真正调用 msg.send"
    assert sdk._MSG_DROP.count == 0, "成功不应计为丢弃"


def test_flush_recovery_resends():
    client = _FakeClient()
    client.call_fail = True
    ctx, client = _ctx(client)
    ctx._queue.append({"message": "hello", "group_id": "1"})
    ctx._flush()              # 失败 → 回队列 + retry_after 退避
    assert len(ctx._queue) == 1
    # 链路恢复
    client.call_fail = False
    client.calls.clear()      # 只看恢复后的发送
    ctx._retry_after = 0.0
    ctx._flush()              # 退避解除后重发
    assert len(ctx._queue) == 0, "恢复后应把积压消息发出"
    assert len(client.calls) == 1, "恢复后应有一次真实发送"


def test_queue_bounded_on_overflow():
    client = _FakeClient()
    client.call_fail = True   # 持续失败，触发有界裁剪
    ctx, client = _ctx(client)
    sdk._MSG_DROP.count = 0
    for i in range(ctx.MAX_QUEUE + 50):
        ctx._queue.append({"message": str(i)})
    ctx._flush()              # 失败回队列，超出上限裁剪最旧
    assert len(ctx._queue) <= ctx.MAX_QUEUE, "队列必须受上限约束"
    assert sdk._MSG_DROP.count >= 1


def test_requeue_preserves_order():
    client = _FakeClient()
    client.call_fail = True
    ctx, client = _ctx(client)
    for m in ("a", "b", "c"):
        ctx._queue.append({"message": m})
    ctx._flush()              # 回队列到队首
    msgs = [it["message"] for it in ctx._queue]
    assert msgs == ["a", "b", "c"], "回队列应保持原始顺序"


# ───────────────────────── 2) 日志批次发送失败可计数 ─────────────────────────

def test_log_drop_counted():
    client = _FakeClient()
    client.fire_fail = True
    ctx, client = _ctx(client)
    sdk._LOG_DROP.count = 0
    ctx.logger._send([{"level": "INFO", "msg": "x", "logger": "p"}])
    assert sdk._LOG_DROP.count >= 1, "日志发送失败应被计数（可见）"


# ───────────────────────── 3) 权限查询失败降级可观测 ─────────────────────────

def _boom(*a, **k):
    raise RuntimeError("DB 不可用")


def test_permission_query_failure_is_visible():
    ctx, client = _ctx()
    sdk._PERM_DROP.count = 0

    def boom(*a, **k):
        raise RuntimeError("DB 不可用")

    ctx.db_query_one = boom
    # 降级方向保持安全（不提权）：
    assert ctx.is_superuser("123") is False
    assert ctx.is_group_admin("1", "123") is False
    assert ctx.is_group_owner("1", "123") is False
    assert ctx.is_blacklisted("123") is False
    assert sdk._PERM_DROP.count >= 4, "四次权限查询失败都应被计数"


def test_fetch_role_degradation_visible():
    ctx, client = _ctx()
    sdk._PERM_DROP.count = 0

    def boom(*a, **k):
        raise RuntimeError("DB 不可用")

    ctx.db_query_one = boom
    role = ctx._fetch_role("123", "1")
    # 安全降级为 member，但必须留下痕迹
    assert role == {"role": "member"}
    assert sdk._PERM_DROP.count >= 1, "角色查询失败降级应被计数"


def test_permission_ok_no_drop():
    ctx, client = _ctx()
    sdk._PERM_DROP.count = 0
    ctx.db_query_one = lambda sql, params=None: {"role": "super"}
    assert ctx.is_superuser("123") is True
    assert sdk._PERM_DROP.count == 0, "正常查询不应计为丢弃"


if __name__ == "__main__":
    # 脚本式运行（pytest 收集不到时可直接 python 跑）
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        sdk._MSG_DROP.count = sdk._LOG_DROP.count = sdk._PERM_DROP.count = 0
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(funcs) - failed, len(funcs)))
    sys.exit(1 if failed else 0)
