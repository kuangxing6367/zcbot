# -*- coding: utf-8 -*-
"""示例插件：定时播报（演示 IIPC 定时任务 @task + 配置持久化）。

每分钟向「配置的目标」发送一次当前时间，验证 cron 任务在 IIPC 进程内执行，
并经批量动作回传通道发送消息。

配置目标（命令写入，落盘持久化）：
  /clock_set group:12345   向群 12345 播报
  /clock_set user:67890    向私聊 67890 播报
  /clock_stop              停止播报
"""
import time

from sdk import PluginContext

__plugin_meta__ = {
    "name": "定时播报",
    "version": "0.1.0",
    "author": "zc",
    "desc": "演示 @task 定时任务 + 配置持久化：每分钟向目标播报时间",
}

# @task 处理函数为零参函数，所需 ctx 在 register 时持有（与 IIPC echo 同写法）
_ctx = None


def on_error(event, error):
    print("[clock] on_error: %s" % error)


def register(ctx: PluginContext):
    global _ctx
    _ctx = ctx
    ctx.logger.info("定时播报插件已加载 (plugin_id=%s)", ctx.plugin_id)

    @ctx.command("/clock_set")
    def clock_set(event, match):
        target = (match.group(1) or "").strip()
        if ":" not in target:
            ctx.send_msg(message="用法：/clock_set group:群号 或 user:QQ号",
                         user_id=event.user_id, group_id=event.group_id)
            return
        ctx.set_config("clock_target", target)
        ctx.send_msg(message="已设置播报目标: %s（每分钟播报）" % target,
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/clock_stop")
    def clock_stop(event, match):
        ctx.set_config("clock_target", "")
        ctx.send_msg(message="已停止定时播报",
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.task("* * * * *")  # 每分钟整点触发
    def tick():
        cfg = _ctx.get_config("clock_target", "")
        if not cfg or ":" not in cfg:
            return
        kind, _, tid = cfg.partition(":")
        now = time.strftime("%H:%M:%S")
        if kind == "group" and tid.isdigit():
            _ctx.send_msg(group_id=int(tid), message="滴答：现在 %s" % now)
        elif kind == "user" and tid.isdigit():
            _ctx.send_msg(user_id=int(tid), message="滴答：现在 %s" % now)
