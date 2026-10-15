# -*- coding: utf-8 -*-
"""
全 API 面调用插件（plugins/api_surface）

把 SDK 暴露的每一个公开 API 真实调用一遍（PluginContext 68 + ActionProxy 24 +
Event 属性 37/方法 8），逐项记录 (name, ok, detail) 落库到 api_surface_result。

设计原则：**插件只负责调用并记录真实返回值，不做自我判定**。
对不对由 tools/api_surface_check.py 按预先写死的期望值断言（防止自证循环）。

分组命令：
  /surface        同步面：DB/配置/权限/身份/日志/审计/数据目录/广播/群开关/文本/发送
  /surface-actions ActionProxy 24 个 typed 动作（假适配器回值）
  /surface-async  async 面：aapi / aemit / db_*_async / asend_msg / call_async / run_async
  /surface-event  Event 属性面（需注入富媒体群消息）
  /surface-waitfor wait_for 多轮会话
  /surface-session create_session
"""
import json
import os

__plugin_meta__ = {
    "name": "API 全量面",
    "version": "1.0.0",
    "author": "iipc",
    "desc": "穷举调用 SDK 全部公开 API 并记录真实返回值",
    "priority": 40,
}

_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS api_surface_result ("
    "name TEXT PRIMARY KEY, ok INTEGER, detail TEXT)"
)

_ADMIN = "10001"     # 内核 --admin
_PLAIN = "999"       # 普通用户
_GROUP = 666

_recv_ev = {}        # 广播接收验证：emit 后由 on 处理器回填


def _short(v, n=400):
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return s[:n]


def register(ctx):
    ctx.create_table(_TABLE_SQL)
    ctx.db_execute("DELETE FROM api_surface_result")

    # ── 注册型 API（在 register 内调用，记录是否抛异常）──
    def _reg(name, fn):
        try:
            r = fn()
            _save(ctx, name, True, _short(r))
        except Exception as e:
            _save(ctx, name, False, "%s: %s" % (type(e).__name__, e))

    _reg("ctx.command", lambda: bool(ctx.command("/surface", on_surface)))
    _reg("ctx.command(actions)", lambda: bool(ctx.command("/surface-actions", on_actions)))
    _reg("ctx.command(async)", lambda: bool(ctx.command("/surface-async", on_async)))
    _reg("ctx.command(event)", lambda: bool(ctx.command("/surface-event", on_event_probe)))
    _reg("ctx.command(waitfor)", lambda: bool(ctx.command("/surface-waitfor", on_waitfor)))
    _reg("ctx.command(session)", lambda: bool(ctx.command("/surface-session", on_session)))
    _reg("ctx.on", lambda: bool(ctx.on("surface.ping", _on_ping)))
    _reg("ctx.task", lambda: bool(ctx.task("0 3 * * *", _nightly)))
    _reg("ctx.hook", lambda: bool(ctx.hook("command.before", _before)))
    _reg("ctx.unhook", lambda: ctx.unhook("command.before"))
    _reg("ctx.on_raw_message", lambda: bool(ctx.on_raw_message(_raw)))
    _reg("ctx.register_api", lambda: ctx.register_api("/surface-test", _api_handler))
    _reg("ctx.dashboard_card", lambda: bool(ctx.dashboard_card("面测卡片", _card)))
    _reg("ctx.webui", lambda: ctx.webui("面测页", entry="index.html"))
    _reg("ctx.override_webui", lambda: ctx.override_webui())
    _reg("ctx.register_group_extension",
         lambda: bool(ctx.register_group_extension("surf_col", "面测列", _ext)))
    _reg("ctx.register_user_extension",
         lambda: bool(ctx.register_user_extension("surf_ucol", "面测用户列", _ext)))


def _save(ctx, name, ok, detail):
    try:
        ctx.db_execute(
            "INSERT OR REPLACE INTO api_surface_result(name, ok, detail) VALUES (%s,%s,%s)",
            (name, 1 if ok else 0, detail))
    except Exception:
        pass


def _t(ctx, name, fn):
    """执行一项并把真实返回值落库（异常也记录，供脚本判定）。"""
    try:
        r = fn()
        _save(ctx, name, True, _short(r))
        return r
    except Exception as e:
        _save(ctx, name, False, "%s: %s" % (type(e).__name__, e))
        return None


# ══════════════════ 同步面 ══════════════════

def on_surface(event, match):
    # 注意：这里**不能**清空 api_surface_result——register 阶段记录的注册型 API
    # 结果也在同一张表里，清掉就再也读不到（首次跑曾因此 17 项全空）。
    # 每轮跑都是新内核 + :memory:，本来就是空表，无需清理。

    # ---- DB 全族 ----
    ctx.create_table(
        "CREATE TABLE IF NOT EXISTS surface_kv (id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT, v TEXT)")
    ctx.db_execute("DELETE FROM surface_kv")
    _t(ctx, "db_insert", lambda: ctx.db_insert(
        "INSERT INTO surface_kv(k,v) VALUES(%s,%s)", ("alpha", "1")))
    _t(ctx, "db_insert#2", lambda: ctx.db_insert(
        "INSERT INTO surface_kv(k,v) VALUES(%s,%s)", ("beta", "2")))
    _t(ctx, "db_execute", lambda: ctx.db_execute(
        "UPDATE surface_kv SET v=%s WHERE k=%s", ("9", "alpha")))
    _t(ctx, "db_execute_many", lambda: ctx.db_execute_many(
        "INSERT INTO surface_kv(k,v) VALUES(%s,%s)", [("g1", "1"), ("g2", "2")]))
    _t(ctx, "db_query", lambda: len(ctx.db_query("SELECT * FROM surface_kv")))
    _t(ctx, "db_query_one", lambda: ctx.db_query_one(
        "SELECT * FROM surface_kv WHERE k=%s", ("alpha",)))
    _t(ctx, "db_exec(=db_execute 别名)", lambda: ctx.db_exec(
        "DELETE FROM surface_kv WHERE k=%s", ("g1",)))
    _t(ctx, "db_exec_many(=db_execute_many 别名)", lambda: ctx.db_exec_many(
        "DELETE FROM surface_kv WHERE k=%s", [("g2",)]))
    _t(ctx, "db_pool_status", lambda: ctx.db_pool_status())
    _t(ctx, "create_table(幂等重建)", lambda: ctx.create_table(_TABLE_SQL))
    _t(ctx, "db_query(坏 SQL 应抛错)", lambda: ctx.db_query("SELECT * FROM no_such_table_xyz"))

    # ---- 配置 ----
    _t(ctx, "set_config", lambda: ctx.set_config("surf_key", "roundtrip-value"))
    _t(ctx, "get_config", lambda: ctx.get_config("surf_key"))
    _t(ctx, "get_config(默认值)", lambda: ctx.get_config("surf_missing", "DEF"))
    _t(ctx, "get_all_config", lambda: ctx.get_all_config())

    # ---- 权限与身份 ----
    _t(ctx, "is_superuser(admin)", lambda: ctx.is_superuser(_ADMIN))
    _t(ctx, "is_superuser(plain)", lambda: ctx.is_superuser(_PLAIN))
    _t(ctx, "get_user_role(admin)", lambda: ctx.get_user_role(_GROUP, _ADMIN))
    # 不传 role：与原框架 perm.resolve 同语义（ROLE_TO_GROUP.get(role or '')，
    # 不查 users 表），未注入内置角色组 → 未定义节点按拒绝。
    _t(ctx, "has_perm(不传role)", lambda: ctx.has_perm(_ADMIN, "zcbot.role.super"))
    _t(ctx, "has_perm(传role=super)", lambda: ctx.has_perm(_ADMIN, "zcbot.role.super", role="super"))
    _t(ctx, "has_perm(plain不传role)", lambda: ctx.has_perm(_PLAIN, "zcbot.role.super"))
    _t(ctx, "has_perm(未定义节点)", lambda: ctx.has_perm(_ADMIN, "no.such.node", role="super"))
    _t(ctx, "check_perm(传role=super)", lambda: ctx.check_perm(_ADMIN, "zcbot.role.super", role="super"))
    _t(ctx, "user_groups(不传role)", lambda: ctx.user_groups(_ADMIN))
    _t(ctx, "user_groups(传role=super)", lambda: ctx.user_groups(_ADMIN, role="super"))
    _t(ctx, "is_blacklisted(plain)", lambda: ctx.is_blacklisted(_PLAIN))
    _t(ctx, "is_group_owner", lambda: ctx.is_group_owner(_GROUP, _ADMIN))
    _t(ctx, "is_group_admin", lambda: ctx.is_group_admin(_GROUP, _ADMIN))

    # ---- 群级插件开关 ----
    def _switch_roundtrip():
        ctx.disable_plugin_in_group("api_surface", _GROUP)
        off = ctx.is_plugin_enabled_in_group("api_surface", _GROUP)
        ctx.enable_plugin_in_group("api_surface", _GROUP)
        on = ctx.is_plugin_enabled_in_group("api_surface", _GROUP)
        return {"off": off, "on": on}
    _t(ctx, "群开关往返(disable→enable)", _switch_roundtrip)
    _t(ctx, "get_plugin_status_list", lambda: ctx.get_plugin_status_list(_GROUP))

    # ---- 日志 / 审计 / 数据目录 ----
    _t(ctx, "log(info)", lambda: ctx.log("surface info log") or "ok")
    _t(ctx, "log(warning)", lambda: ctx.log("surface warn log", level="warning") or "ok")
    _t(ctx, "log(error)", lambda: ctx.log("surface error log", level="error") or "ok")
    _t(ctx, "get_data_dir", lambda: ctx.get_data_dir())
    _t(ctx, "数据目录可写", lambda: _write_data_dir(ctx))
    _t(ctx, "audit_log", lambda: ctx.audit_log("surface_test", target_type="plugin",
                                               target_name="api_surface", detail={"a": 1}) or "ok")

    # ---- 广播（emit → 自身 on 处理器回填）──
    _recv_ev.clear()
    _t(ctx, "emit", lambda: ctx.emit("surface.ping", {"n": 42}) or "ok")

    # ---- 文本提取 / 发送 ----
    _t(ctx, "get_text(dict)", lambda: ctx.get_text({"message": "[CQ:at,qq=1] 你好"}))
    _t(ctx, "send_msg", lambda: ctx.send_msg(user_id=int(_ADMIN), message="surface sync") or "ok")
    _t(ctx, "send_private_msg", lambda: ctx.send_private_msg(int(_ADMIN), "surface priv") or "ok")
    _t(ctx, "send_group_msg", lambda: ctx.send_group_msg(_GROUP, "surface grp") or "ok")

    # ---- 快捷动作（typed，走阻塞 api.call）──
    _t(ctx, "ban", lambda: ctx.ban(_GROUP, int(_PLAIN), 60))
    _t(ctx, "kick", lambda: ctx.kick(_GROUP, int(_PLAIN)))
    _t(ctx, "mute_all", lambda: ctx.mute_all(_GROUP, True))
    _t(ctx, "set_card", lambda: ctx.set_card(_GROUP, int(_PLAIN), "卡"))
    _t(ctx, "get_member_list", lambda: ctx.get_member_list(_GROUP))
    _t(ctx, "get_member_info", lambda: ctx.get_member_info(_GROUP, int(_PLAIN)))
    _t(ctx, "delete_msg", lambda: ctx.delete_msg(12345))
    _t(ctx, "get_login_info", lambda: ctx.get_login_info())
    _t(ctx, "get_stranger_info", lambda: ctx.get_stranger_info(int(_PLAIN)))
    _t(ctx, "get_group_info", lambda: ctx.get_group_info(_GROUP))
    _t(ctx, "api(原始动作)", lambda: ctx.api("send_msg", user_id=int(_ADMIN), message="raw api"))

    # ---- 异步执行器 ----
    _t(ctx, "run_async", lambda: ctx.run_async(lambda: 7 * 6).result(timeout=5))
    # call_async 必须放在**同步** handler 里测：它把协程调度到插件事件循环线程，
    # 若在 async handler 内 fut.result() 会阻塞该循环本身 → 自锁超时（不是 SDK 问题）。
    try:
        _save(ctx, "call_async", True,
              _short(ctx.call_async(_coro_probe()).result(timeout=5)))
    except Exception as e:
        _save(ctx, "call_async", False, "%s: %s" % (type(e).__name__, e))

    ctx.send_msg(user_id=int(_ADMIN), message="surface done")
    return None


def _write_data_dir(ctx):
    d = ctx.get_data_dir()
    if not d:
        return "空路径"
    p = os.path.join(d, "_surface_probe.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("probe")
    with open(p, encoding="utf-8") as f:
        return f.read()


# ══════════════════ ActionProxy 24 个动作 ══════════════════

def on_actions(event, match):
    a = ctx.actions
    _t(ctx, "actions.call(原始)", lambda: a.call("send_msg", user_id=int(_ADMIN), message="ap raw"))
    _t(ctx, "actions.send_msg", lambda: a.send_msg(user_id=int(_ADMIN), message="ap send"))
    _t(ctx, "actions.set_group_ban", lambda: a.set_group_ban(group_id=_GROUP, user_id=int(_PLAIN), duration=60))
    _t(ctx, "actions.set_group_kick", lambda: a.set_group_kick(group_id=_GROUP, user_id=int(_PLAIN)))
    _t(ctx, "actions.set_group_whole_ban", lambda: a.set_group_whole_ban(group_id=_GROUP, enable=True))
    _t(ctx, "actions.set_group_card", lambda: a.set_group_card(group_id=_GROUP, user_id=int(_PLAIN), card="c"))
    _t(ctx, "actions.get_group_member_list", lambda: a.get_group_member_list(group_id=_GROUP))
    _t(ctx, "actions.get_group_member_info", lambda: a.get_group_member_info(group_id=_GROUP, user_id=int(_PLAIN)))
    _t(ctx, "actions.get_stranger_info", lambda: a.get_stranger_info(user_id=int(_PLAIN)))
    _t(ctx, "actions.get_group_info", lambda: a.get_group_info(group_id=_GROUP))
    _t(ctx, "actions.get_group_list", lambda: a.get_group_list())
    _t(ctx, "actions.get_friend_list", lambda: a.get_friend_list())
    _t(ctx, "actions.get_login_info", lambda: a.get_login_info())
    _t(ctx, "actions.get_status", lambda: a.get_status())
    _t(ctx, "actions.get_version_info", lambda: a.get_version_info())
    _t(ctx, "actions.delete_msg", lambda: a.delete_msg(message_id=1))
    _t(ctx, "actions.get_msg", lambda: a.get_msg(message_id=1))
    _t(ctx, "actions.set_friend_add_request", lambda: a.set_friend_add_request(flag="f", approve=True))
    _t(ctx, "actions.set_group_add_request", lambda: a.set_group_add_request(flag="f", sub_type="add", approve=True))
    _t(ctx, "actions.get_image", lambda: a.get_image(file="x.jpg"))
    _t(ctx, "actions.can_send_image", lambda: a.can_send_image())
    _t(ctx, "actions.get_record", lambda: a.get_record(file="x.wav", out_format="mp3"))
    _t(ctx, "onebot(=actions 别名)", lambda: ctx.onebot.get_login_info())
    ctx.send_msg(user_id=int(_ADMIN), message="actions done")
    return None


# ══════════════════ async 面 ══════════════════

async def on_async(event, match):
    _t(ctx, "aapi", lambda: None)  # 占位，真正的协程调用在下方顺序执行
    r = await ctx.aapi("send_msg", user_id=int(_ADMIN), message="aapi")
    _save(ctx, "aapi", True, _short(r))
    await ctx.asend_msg(user_id=int(_ADMIN), message="asend")
    _save(ctx, "asend_msg", True, "ok")
    await ctx.aemit("surface.ping", {"n": 7})
    _save(ctx, "aemit", True, "ok")
    rows = await ctx.db_query_async("SELECT 1 AS one")
    _save(ctx, "db_query_async", True, _short(rows))
    row = await ctx.db_query_one_async("SELECT 2 AS two")
    _save(ctx, "db_query_one_async", True, _short(row))
    n = await ctx.db_execute_async("UPDATE surface_kv SET v=%s WHERE k=%s", ("async", "alpha"))
    _save(ctx, "db_execute_async", True, _short(n))
    n2 = await ctx.db_execute_many_async("DELETE FROM surface_kv WHERE k=%s", [("zzz",)])
    _save(ctx, "db_execute_many_async", True, _short(n2))
    i = await ctx.db_insert_async("INSERT INTO surface_kv(k,v) VALUES(%s,%s)", ("asyncrow", "1"))
    _save(ctx, "db_insert_async", True, _short(i))
    await ctx.create_table_async(
        "CREATE TABLE IF NOT EXISTS surface_async (id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT)")
    _save(ctx, "create_table_async", True, "ok")
    await ctx.asend_msg(user_id=int(_ADMIN), message="async done")
    return None


async def _coro_probe():
    return "coro-ok"


# ══════════════════ Event 属性面 ══════════════════

def on_event_probe(event, match):
    """注入富媒体群消息后调用本命令，逐项记录 Event 属性真实值。"""
    def _p(name, fn):
        try:
            _save(ctx, "event." + name, True, _short(fn()))
        except Exception as e:
            _save(ctx, "event." + name, False, "%s: %s" % (type(e).__name__, e))

    _p("user_id", lambda: event.user_id)
    _p("group_id", lambda: event.group_id)
    _p("message", lambda: event.message)
    _p("raw_message", lambda: event.raw_message)
    _p("message_type", lambda: event.message_type)
    _p("post_type", lambda: event.post_type)
    _p("self_id", lambda: event.self_id)
    _p("message_id", lambda: event.message_id)
    _p("sender", lambda: event.sender)
    _p("is_group", lambda: event.is_group)
    _p("is_private", lambda: event.is_private)
    _p("sender_nickname", lambda: event.sender_nickname)
    _p("sender_card", lambda: event.sender_card)
    _p("sender_role", lambda: event.sender_role)
    _p("role", lambda: event.role)
    _p("is_admin", lambda: event.is_admin)
    _p("is_superuser", lambda: event.is_superuser)
    _p("is_blacklisted", lambda: event.is_blacklisted)
    _p("is_group_owner", lambda: event.is_group_owner)
    _p("is_group_admin", lambda: event.is_group_admin)
    _p("text", lambda: event.text)
    _p("media", lambda: event.media)
    _p("is_media_only", lambda: event.is_media_only)
    _p("has_image", lambda: event.has_image)
    _p("has_reply", lambda: event.has_reply)
    _p("has_voice", lambda: event.has_voice)
    _p("has_video", lambda: event.has_video)
    _p("has_file", lambda: event.has_file)
    _p("has_sticker", lambda: event.has_sticker)
    _p("has_face", lambda: event.has_face)
    _p("has_share", lambda: event.has_share)
    _p("share", lambda: event.share)
    _p("has_at", lambda: event.has_at)
    _p("has_at_bot", lambda: event.has_at_bot)
    _p("at_all", lambda: event.at_all)
    _p("at_list", lambda: event.at_list)
    _p("reply_id", lambda: event.reply_id)
    _p("images", lambda: event.images)
    _p("voices", lambda: event.voices)
    _p("videos", lambda: event.videos)
    _p("files", lambda: event.files)
    _p("stickers", lambda: event.stickers)
    _p("first_image", lambda: event.first_image)
    _p("first_voice", lambda: event.first_voice)
    _p("first_video", lambda: event.first_video)
    _p("first_file", lambda: event.first_file)
    _p("get(user_id)", lambda: event.get("user_id"))
    _p("get(不存在,默认)", lambda: event.get("nope", "DV"))
    _p("__getitem__", lambda: event["user_id"])
    _p("__contains__", lambda: "user_id" in event)
    _p("keys", lambda: sorted(event.keys()))
    _p("has_perm(zcbot.role.super)", lambda: event.has_perm("zcbot.role.super"))
    _p("check_perm(zcbot.role.super)", lambda: event.check_perm("zcbot.role.super"))
    _p("stop_event", lambda: event.stop_event() or "ok")
    _p("is_stopped", lambda: event.is_stopped())
    _p("continue_route", lambda: event.continue_route() or "ok")
    _p("is_continue_route", lambda: event.is_continue_route())
    ctx.send_msg(user_id=int(_ADMIN), message="event done")
    return None


# ══════════════════ 多轮会话 ══════════════════

def on_waitfor(event, match):
    r = ctx.wait_for(event, prompt="请回答你的名字", timeout=8)
    _save(ctx, "wait_for", r is not None, _short(r))
    ctx.send_msg(user_id=int(_ADMIN), message="waitfor done")
    return None


def on_session(event, match):
    try:
        s = ctx.create_session(event, timeout=5)
        _save(ctx, "create_session", True, _short(getattr(s, "session_id", "")))
        try:
            s.close()
        except Exception:
            pass
    except Exception as e:
        _save(ctx, "create_session", False, "%s: %s" % (type(e).__name__, e))
    ctx.send_msg(user_id=int(_ADMIN), message="session done")
    return None


# ══════════════════ 回调 ══════════════════

def _on_ping(payload):
    _recv_ev["ping"] = payload
    return None


def _before(payload):
    return None


def _raw(event, bot_name):
    return None


def _api_handler(*a, **k):
    return {"ok": True}


def _card(*a, **k):
    return {"title": "面测卡片"}


def _ext(*a, **k):
    return ""


def _nightly():
    return None
