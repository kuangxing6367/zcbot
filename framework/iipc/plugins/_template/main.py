# -*- coding: utf-8 -*-
"""插件模板（脚手架）：复制本目录即可开始写一个 zcbot IIPC 插件。

给一份带完整类型标注的起点，
让 IDE（pyright / pylance）对 `ctx.` 与 `event.` 都有补全。

要点：
  - `register(ctx: PluginContext)`：ctx 已标注类型，写 ctx. 直接出补全。
  - 模块级 `ctx: "PluginContext"`：handler 里用裸 `ctx` 也有补全（SDK 运行时会注入真值）。
  - handler 的 `event` 标注为 `Event`，`event.message / event.user_id / ...` 均有补全。
  - 扩展面方法（kv / config / http / call_plugin / transform / ...）由 SDK 运行时挂载，
    类型已在 PluginContext 上声明，补全可用。
"""
from sdk import PluginContext
from event import Event

__plugin_meta__ = {
    "name": "我的插件",
    "version": "0.1.0",
    "author": "你的名字",
    "desc": "一句话说明这个插件做什么",
    "priority": 50,
}

# 模块级 ctx 注解：让下方 handler 里裸写的 `ctx` 也能被 IDE 识别类型。
# SDK 会在加载时把真实 PluginContext 实例注入此变量（无需你手动赋值）。
ctx: "PluginContext"


def register(ctx: PluginContext):
    """插件入口：所有注册都写在这里（或用模块级装饰器，见下方注释）。"""
    ctx.logger.info("我的插件已加载 (plugin_id=%s)", ctx.plugin_id)

    # ── 命令：/hello ──
    @ctx.command("/hello")
    def hello(event: Event, match):
        ctx.send_msg(
            message="hello, %s" % (event.sender.get("nickname") or event.user_id),
            user_id=event.user_id,
            group_id=event.group_id,
        )

    # ── 订阅事件 ──
    @ctx.on("message")
    def on_message(event: Event):
        # 用共享 KV 统计收到消息数（跨事件/跨调用持久）
        n = (ctx.kv.get("msg_count") or 0) + 1
        ctx.kv.set("msg_count", n)

    # ── 中间件：入站前脱敏（多阶段管线，可选）──
    @ctx.middleware("pre", priority=10)
    def redact(event: Event, c: PluginContext):
        # 返回 False 可短路整个分发；否则就地改写 event 后继续
        return True

    # ── 配置读写 ──
    @ctx.command("/set")
    def set_cfg(event: Event, match):
        ctx.set_config("greeting", "hi")
        v = ctx.get_config("greeting", "default")
        ctx.send_msg(message="cfg=%s" % v, user_id=event.user_id)

    # ── 懒加载 HTTP 调用 ──
    @ctx.command("/fetch")
    def fetch(event: Event, match):
        r = ctx.http.get("https://example.com/health")
        ctx.send_msg(message="status=%s" % r.status_code, user_id=event.user_id)

    # ── 跨插件 RPC（调用别处 expose 的动作）──
    @ctx.command("/rpc")
    def rpc(event: Event, match):
        try:
            res = ctx.call_plugin("other_plugin", "some_action", {"x": 1})
            ctx.send_msg(message="rpc=%s" % res, user_id=event.user_id)
        except Exception as e:
            ctx.send_msg(message="rpc_err=%s" % e, user_id=event.user_id)

    # ── 内核级全局变换节点（事件扇出前改写，可选）──
    @ctx.transform(priority=10, action="normalize")
    def normalize(params):
        # params 即 event.data（dict）；返回改写后的 dict，或 None 丢弃
        params = dict(params or {})
        params["message"] = (params.get("message") or "").strip()
        return params

    # ── 定时任务：每小时整点 ──
    @ctx.task("0 * * * *")
    def hourly():
        ctx.set_config("last_beat", str(__import__("time").time()))


# 模块级装饰器写法（等价）：
#    from framework.plugin import command, on_message
#    @command("/hi")
#    def hi(event, match):
#        ctx.send_msg(message="hi", user_id=event.user_id)
