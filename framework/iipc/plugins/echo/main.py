# -*- coding: utf-8 -*-
"""示例插件（1:1 框架写法）：回声 + ping 命令 + DB 往返 + API 通道 + 权限命令。

证明：事件总线（message 事件）→ Python 插件处理 → 批量动作回传（send_msg）→
DB 在 Go 侧执行（不占 Python GIL）→ 通用 api.call 通道（get_login_info 等）。

本文件刻意使用与原 zcbot 插件完全相同的写法（ctx.command/ctx.on 实例方法形态），
拖进真框架也能跑——IIPC 的 1:1 目标即此。
"""
__plugin_meta__ = {
    "name": "回声插件",
    "version": "1.1.0",
    "author": "zc",
    "desc": "演示 IIPC 事件总线 + 批量动作回传 + Go 侧 DB + 通用 API 通道",
}


def on_error(event, error):
    """handler 异常钩子（与原框架一致：模块级 on_error(event, error)）。"""
    print("[echo] on_error: %s" % error)


# 定时任务 handler 为模块级零参函数（框架 scheduler._run_job 直接 handler()），
# 需要的 ctx 由 register 时持有（与真插件一致写法）。
_ctx = None


def register(ctx):
    global _ctx
    _ctx = ctx
    # 日志接口演示：ctx.logger（logging 子集）→ IIPC log.batch → 内核 → launcher 统一落盘
    ctx.logger.info("echo 插件已加载 (plugin_id=%s)", ctx.plugin_id)

    @ctx.on("message")
    def echo(event):
        # 1:1 事件面：属性访问（event.message / event.user_id / event.group_id）
        text = event.message
        ctx.logger.debug("收到消息: %s", text)
        ctx.send_msg(
            message="echo: " + text,
            user_id=event.user_id,
            group_id=event.group_id,
        )

    @ctx.command("/ping")
    def ping(event, match):
        ctx.send_msg(message="pong", user_id=event.user_id, group_id=event.group_id)
        # DB 往返：CREATE → INSERT → SELECT，全部在 Go 进程执行（不占 Python GIL）
        # 注意 SQL 按 MySQL 习惯写 %s 占位——内核做方言翻译（对齐框架写法）
        try:
            ctx.db_exec("CREATE TABLE IF NOT EXISTS kv(k TEXT, v TEXT)")
            ctx.db_exec("INSERT INTO kv(k,v) VALUES(%s,%s)", ["ping_count", "1"])
            rows = ctx.db_query("SELECT v FROM kv WHERE k=%s", ["ping_count"])
            ctx.send_msg(message="db=" + str(rows),
                         user_id=event.user_id, group_id=event.group_id)
        except Exception as e:
            ctx.send_msg(message="db_err=" + str(e),
                         user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/info")
    def info(event, match):
        # 通用 API 通道：查询登录信息（走阻塞 call 拿返回值）
        li = ctx.get_login_info()
        ctx.send_msg(message="info=" + str(li),
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/cfg")
    def cfg(event, match):
        # 配置持久化：set 后 get，验证落盘/内存引擎往返
        ctx.set_config("demo_key", "demo_val")
        v = ctx.get_config("demo_key", "default")
        ctx.send_msg(message="cfg=" + str(v),
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/secret", require_admin=True)
    def secret(event, match):
        # 仅管理员可命中（内核 admins + ev.role 判定，1:1 对齐 require_admin）
        ctx.send_msg(message="secret ok",
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.command("/whoami")
    def whoami(event, match):
        # 身份查询：users / group_members 表经 DB 桥接
        ctx.send_msg(message="role=%s is_admin=%s" % (event.role, event.is_admin),
                     user_id=event.user_id, group_id=event.group_id)

    @ctx.task("0 * * * *")  # 每小时整点（cron 5 段，对齐框架 @task）
    def hourly_heartbeat():
        # 演示定时任务：写一条心跳配置（handler 零参，ctx 由 register 持有）
        _ctx.set_config("last_heartbeat", str(__import__("time").time()))
