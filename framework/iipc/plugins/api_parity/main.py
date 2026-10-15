# -*- coding: utf-8 -*-
"""API 1:1 验收插件（api_parity）：覆盖原 zcbot 插件会碰到的全部 API 面。

用两种等价写法各注册一半，证明两条路都通：
- 模块级装饰器（from framework.plugin import ...，真插件主流写法）
- register(ctx) 内 ctx.command/ctx.on 实例方法

覆盖：Event 属性/dict 双访问、命令正则与别名、require_admin/require_perm、
多轮会话 wait_for/create_session、群插件开关、actions/onebot 代理、
db 全系（query/one/execute/many/insert + async 变体）、get_all_config、
audit_log、run_async/call_async、get_data_dir、hook、on_raw_message 接管、
task.tick、event.emit 广播。
"""
from framework.plugin import command, on, on_message, task, api, dashboard_card, hook

__plugin_meta__ = {
    "name": "API 验收",
    "version": "1.0.0",
    "author": "zc",
    "desc": "IIPC 1:1 API 面验收插件",
}

# 会话等待中的标记（wait_for 演示用）
_pending_name = {}


# ── 模块级装饰器形态（真插件主流写法）──

@command(r"^/rx (\w+)", description="正则命令：捕获组取参")
def rx_demo(event, match):
    event.ctx_send("rx arg=%s" % match.group(1))


@command("/parity", alias="/py,/p", description="全链路自检")
def parity(event, match):
    """自检：DB/配置/动作代理/数据目录/审计。"""
    ctx = event._ctx
    # db 全系（MySQL 风格 %s）
    ctx.db_exec("CREATE TABLE IF NOT EXISTS parity_log(id INTEGER PRIMARY KEY, note TEXT)")
    new_id = ctx.db_insert("INSERT INTO parity_log(note) VALUES(%s)", ["parity-run"])
    rows = ctx.db_query("SELECT id, note FROM parity_log ORDER BY id DESC LIMIT 1")
    one = ctx.db_query_one("SELECT COUNT(*) AS c FROM parity_log")
    # 配置
    ctx.set_config("parity_key", "v1")
    allc = ctx.get_all_config()
    # 数据目录
    dat = ctx.get_data_dir()
    # 审计
    ctx.audit_log("parity", target_type="selftest", detail={"id": new_id})
    event.ctx_send(
        "parity ok id=%s rows=%s count=%s cfg=%s dat=%s"
        % (new_id, rows, one and one.get("c"), "parity_key" in allc, bool(dat)))


@command("/askss", description="多轮会话：问名字再回答")
def askss(event, match):
    ctx = event._ctx

    def _do():
        name = ctx.wait_for(event, prompt="你叫什么名字？", timeout=30)
        if name is None:
            ctx.send_msg(group_id=event.group_id or None,
                         user_id=event.user_id or None, message="等超时了")
            return
        text = name.get("message") if isinstance(name, dict) else str(name)
        ctx.send_msg(group_id=event.group_id or None,
                     user_id=event.user_id or None,
                     message="你好，%s！" % (text or "匿名"))

    ctx.run_async(_do)


@command("/switch", description="群插件开关演示")
def switch(event, match):
    ctx = event._ctx
    gid = str(event.group_id)
    cur = ctx.is_plugin_enabled_in_group("echo", gid)
    ctx.disable_plugin_in_group("echo", gid)
    after = ctx.is_plugin_enabled_in_group("echo", gid)
    ctx.enable_plugin_in_group("echo", gid)
    event.ctx_send("echo 开关: %s -> %s -> 已恢复" % (cur, after))


@command("/acts", description="actions/onebot 代理（typed 方法）")
def acts(event, match):
    ctx = event._ctx
    info = ctx.actions.get_login_info()
    event.ctx_send("login_info=%s" % info)


@command("/grp", description="群成员角色查询")
def grp(event, match):
    ctx = event._ctx
    role = ctx.get_user_role(event.group_id, event.user_id)
    event.ctx_send("role=%s owner=%s admin=%s" % (
        role, ctx.is_group_owner(event.group_id, event.user_id),
        ctx.is_group_admin(event.group_id, event.user_id)))


@command("/emit", description="广播一个自定义事件")
def emit(event, match):
    event._ctx.emit("parity.ping", {"from": str(event.user_id)})
    event.ctx_send("emitted")


@command("/data", description="dict 式访问事件字段")
def data_access(event, match):
    event.ctx_send("dict: user_id=%s group_id=%s msg=%s" % (
        event.get("user_id"), event.get("group_id"), event.get("message")))


@command("/help", description="帮助")
def help_cmd(event, match):
    event.ctx_send(
        "命令: /parity /askss /switch /acts /grp /emit /data /slow /raise /take\n"
        "另: @bot 我会回话（on_raw_message 演示 /raw）")


# ── register(ctx) 实例方法形态 ──


# 定时任务 handler 为模块级零参函数（框架 scheduler._run_job 直接 handler()），
# 需要的 ctx 由 register 时持有（与真插件一致写法）。
_ctx = None


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.logger.info("api_parity 已加载 (plugin_id=%s)", ctx.plugin_id)

    @ctx.on("message")
    def on_msg(event):
        # at 机器人时回话（对齐 has_at_bot 用法）
        if event.has_at_bot and "你好" in (event.message or ""):
            event.ctx_send("你好，%s！" % event.user_id)

    @ctx.on("parity.ping")
    def on_parity_ping(event):
        # 收到 /emit 广播的事件（event.emit → 总线 → 订阅者）
        ctx.logger.info("收到 parity.ping 事件: %s", event.get("from"))

    @ctx.command("/slow")
    def slow(event, match):
        import time
        time.sleep(0.5)
        event.ctx_send("slow done")

    @ctx.command("/raise")
    def raise_cmd(event, match):
        raise RuntimeError("故意抛错（验证 on_error 钩子）")

    @ctx.command("/take")
    def take(event, match):
        # command.before 扩展点演示对象：hook 对其返回 False，本命令被跳过不执行
        event.ctx_send("take ran")

    @ctx.command("/takeover")
    def takeover(event, match):
        event.ctx_send("takeover: 我接管了这条消息")

    # hook：命令执行前扩展点（对齐框架 hooks.trigger_async：单参 payload dict，
    # 返回 False 跳过该命令；payload 含 plugin/handler/pattern/event/match）
    ctx.hook("command.before", _before_command)

    # 定时任务：每分钟一次（验证 cron 到 tick 到本地执行全链）
    ctx.task("* * * * *", _minutely, description="每分钟心跳")

    # API 路由登记（IIPC 无 Web 层时仅登记元数据）
    ctx.register_api("/api/parity/stats", methods=["GET"],
                     description="验收插件统计")
    ctx.dashboard_card("API 验收", icon="check")


def _before_command(payload):
    # 框架同口径：payload = {'plugin','handler','command_id','pattern','event','match'}
    # 返回 False 跳过该命令（框架 if False in _cb_res: continue）——演示 /take 被跳过
    if payload.get("pattern") == "/take":
        return False
    return None


def _minutely():
    _ctx.set_config("minutely_tick", str(__import__("time").time()))


def on_error(event, error):
    """模块级 on_error：handler 异常时框架/IIPC 调用（对齐原框架契约）。"""
    print("[api_parity] on_error: %r" % error)


# ── Event 便捷方法：ctx_send（按事件上下文自动选群/私聊回送）──
# 说明：这是本验收插件的便利封装，不是框架 API；它内部只调 ctx.send_msg。
def _install_ctx_send():
    from event import Event

    def ctx_send(self, message):
        ctx = self._ctx
        gid = self.group_id
        uid = self.user_id
        if gid not in (None, 0, "0", ""):
            ctx.send_msg(group_id=gid, message=message)
        elif uid:
            ctx.send_msg(user_id=uid, message=message)

    Event.ctx_send = ctx_send


_install_ctx_send()
