# -*- coding: utf-8 -*-
"""全 API 面验收脚本（tools/api_surface_check.py）

plugins/api_surface 把 SDK 全部公开 API 真实调用一遍并落库 (name, ok, detail)。
本脚本按**预先写死的期望值**逐项判定——插件只报真实返回值，判定权在脚本侧，
避免"插件说自己 ok 就算 ok"的自证循环。

覆盖：PluginContext 68 + ActionProxy 24 + Event 属性 37 / 方法 8。

用法：
    python tools/api_surface_check.py [--kernel path] [--plugins-dir path] [--keep]
退出码 0 = 全部通过。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IIPC = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(IIPC, "py_sdk"))
sys.path.insert(0, HERE)

from iipc_client import IIPCClient  # noqa: E402
from portutil import pick_addr  # noqa: E402

# 端口动态分配：不写死，避免与开发机上正在跑的服务抢端口
ADDR = pick_addr("ZCBOT_SURFACE_ADDR")
AUTH = "dev-auth"
ADMIN = 10001
GROUP = 666

results = []
_log_lock = threading.Lock()
klog_ref = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, ("-- " + detail) if detail else ""))
    return ok


def klog_get():
    with _log_lock:
        return list(klog_ref)


class FakeAdapter:
    """假适配器：所有动作回可识别的固定结构，供脚本校验回传链路。"""

    def __init__(self):
        self.client = IIPCClient(ADDR, AUTH)
        self.inbox = []
        self._lock = threading.Lock()
        self.actions = []
        self._seq = 5000
        self.client.register("api.call", self._on_api_call)

    def _on_api_call(self, params):
        action = params.get("action", "")
        with self._lock:
            self.actions.append(action)
            if action == "send_msg":
                self.inbox.append(params.get("params") or {})
            self._seq += 1
        return {"retcode": 0, "status": "ok",
                "data": {"echo_action": action, "nickname": "fakebot", "user_id": 123,
                         "message_id": self._seq},
                "message_id": self._seq}

    def drain(self):
        with self._lock:
            out, self.inbox = list(self.inbox), []
        return out

    def wait_msg(self, predicate, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for p in self.inbox:
                    if predicate(p):
                        return p
            time.sleep(0.05)
        return None

    def inject(self, text, user_id=ADMIN, group_id=None, segments=None):
        data = {"message": segments if segments is not None else text,
                "user_id": user_id, "self_id": "testbot",
                "post_type": "message",
                "message_type": "group" if group_id else "private",
                "raw_message": text, "font": 0}
        if group_id:
            data["group_id"] = group_id
        self.client.call("debug.inject", {"type": "message", "data": data})

    def inject_rich(self, text, segments, user_id=ADMIN, group_id=None):
        """message 保持纯文本（命令匹配用），富媒体段走独立 segments 字段。"""
        data = {"message": text, "segments": segments,
                "user_id": user_id, "self_id": "testbot",
                "post_type": "message",
                "message_type": "group" if group_id else "private",
                "raw_message": text, "font": 0,
                "sender": {"nickname": "探长", "card": "名片", "role": "member"}}
        if group_id:
            data["group_id"] = group_id
        self.client.call("debug.inject", {"type": "message", "data": data})


def db(ad, sql, args=None):
    res = ad.client.call("db.query", {"sql": sql, "args": list(args or [])})
    return res.get("rows", []) if isinstance(res, dict) else []


def load(ad):
    """把结果表读成 {name: (ok, detail)}。"""
    rows = db(ad, "SELECT name, ok, detail FROM api_surface_result")
    return {r["name"]: (bool(r["ok"]), r.get("detail") or "") for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.path.join(IIPC, "go_kernel", "zcbot_kernel.exe"))
    ap.add_argument("--plugins-dir", default=os.path.join(IIPC, "plugins"))
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.kernel):
        print("内核不存在: %s（先 go build）" % a.kernel)
        return 2

    kenv = dict(os.environ)
    kenv["PYTHONIOENCODING"] = "utf-8"
    kenv["PYTHONUTF8"] = "1"
    kenv["ZCBOT_PYTHON"] = sys.executable
    kproc = subprocess.Popen(
        [a.kernel, "--plugins-dir", a.plugins_dir, "--ipc-addr", ADDR,
         "--db", ":memory:", "--data-dir", os.path.join(IIPC, "plugins_dat"),
         "--admin", str(ADMIN), "--log-level", "debug"],
        cwd=os.path.dirname(a.kernel), env=kenv,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=lambda: [klog_ref.append(l) for l in kproc.stdout],
                     daemon=True).start()

    try:
        run_checks(a)
    finally:
        if a.keep:
            print("---- 内核日志（保留）----")
            for l in klog_ref[-150:]:
                print(l.rstrip())
        else:
            kproc.terminate()
            try:
                kproc.wait(5)
            except Exception:
                kproc.kill()

    failed = [r for r in results if not r[1]]
    print("\n==== %d 项，通过 %d，失败 %d ====" % (len(results), len(results) - len(failed), len(failed)))
    if failed:
        print("---- 失败明细 ----")
        for n, _, d in failed:
            print("  %s -> %s" % (n, d[:200]))
    return 1 if failed else 0


def wait_register(ad, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = db(ad, "SELECT DISTINCT plugin_name FROM commands WHERE plugin_name='api_surface'")
            if rows:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def run_checks(a):
    ok = False
    for _ in range(100):
        if any("IIPC 服务端监听" in l for l in klog_get()):
            ok = True
            break
        time.sleep(0.1)
    check("内核监听", ok)
    if not ok:
        return

    ad = FakeAdapter()
    ad.client.call("adapter.register", {"self_id": "testbot"})
    check("api_surface 插件注册", wait_register(ad))

    # ═════════ 1. 同步面 ═════════
    ad.drain()
    ad.inject("/surface")
    m = ad.wait_msg(lambda p: str(p.get("message")) == "surface done", timeout=15)
    check("/surface 执行完成", m is not None)
    if m is None:
        return
    R = load(ad)
    check("结果条目数 >= 45", len(R) >= 45, str(len(R)))

    # —— 注册型 API（register 内调用）——
    for name in ["ctx.command", "ctx.command(actions)", "ctx.command(async)",
                 "ctx.command(event)", "ctx.command(waitfor)", "ctx.command(session)",
                 "ctx.on", "ctx.task", "ctx.hook", "ctx.unhook", "ctx.on_raw_message",
                 "ctx.register_api", "ctx.dashboard_card", "ctx.webui",
                 "ctx.override_webui", "ctx.register_group_extension",
                 "ctx.register_user_extension"]:
        check("注册型 " + name, R.get(name, (False, ""))[0], R.get(name, ("", ""))[1])

    # —— DB 族：断言真实返回值语义 ——
    def ok_of(n):
        return R.get(n, (False, ""))[0]

    def det(n):
        return R.get(n, (False, ""))[1] or ""

    check("db_insert 返回自增 id>0", ok_of("db_insert") and det("db_insert").isdigit()
          and int(det("db_insert")) > 0, det("db_insert"))
    check("db_insert#2 id 递增", det("db_insert#2").isdigit()
          and int(det("db_insert#2") or 0) > int(det("db_insert") or 0),
          "%s -> %s" % (det("db_insert"), det("db_insert#2")))
    check("db_execute 返回受影响行数>=1",
          ok_of("db_execute") and int(det("db_execute") or 0) >= 1, det("db_execute"))
    check("db_execute_many 返回 2", det("db_execute_many") == "2", det("db_execute_many"))
    check("db_query 返回 4 行", det("db_query") == "4", det("db_query"))
    check("db_query_one 命中 alpha", "alpha" in det("db_query_one"), det("db_query_one"))
    check("db_query_one 的 v 已更新为 9",
          '"v": "9"' in det("db_query_one") or "'v': '9'" in det("db_query_one")
          or '"v":"9"' in det("db_query_one"), det("db_query_one"))
    check("db_exec 别名可用", ok_of("db_exec(=db_execute 别名)"))
    check("db_exec_many 别名可用", ok_of("db_exec_many(=db_execute_many 别名)"))
    check("db_pool_status 返回 dict", "mode" in det("db_pool_status"), det("db_pool_status"))
    check("create_table 幂等重建", ok_of("create_table(幂等重建)"))
    # 异常路径：坏 SQL 必须失败（失败才是正确行为）
    check("坏 SQL 确实抛错(异常路径正确)",
          ok_of("db_query(坏 SQL 应抛错)") is False
          and "no_such_table" in det("db_query(坏 SQL 应抛错)"),
          det("db_query(坏 SQL 应抛错)"))

    # —— 配置 ——
    check("set/get 配置往返", det("get_config") == "roundtrip-value", det("get_config"))
    check("get_config 默认值", det("get_config(默认值)") == "DEF", det("get_config(默认值)"))
    check("get_all_config 含该键", "surf_key" in det("get_all_config"), det("get_all_config")[:120])

    # —— 权限与身份 ——
    check("is_superuser(admin)=True", det("is_superuser(admin)") == "true",
          det("is_superuser(admin)"))
    check("is_superuser(plain)=False", det("is_superuser(plain)") == "false",
          det("is_superuser(plain)"))
    check("get_user_role(admin)=super", det("get_user_role(admin)") == "super",
          det("get_user_role(admin)"))
    # has_perm 不传 role 时与原框架同语义：perm.resolve 只做 ROLE_TO_GROUP.get(role or '')，
    # 不会去 users 表反查身份，因此未注入内置角色组 → 未定义节点按拒绝。
    check("has_perm(不传role)=False（对齐原框架 perm.resolve 语义）",
          det("has_perm(不传role)") == "false", det("has_perm(不传role)"))
    check("has_perm(传role=super)=True", det("has_perm(传role=super)") == "true",
          det("has_perm(传role=super)"))
    check("has_perm(plain不传role)=False", det("has_perm(plain不传role)") == "false",
          det("has_perm(plain不传role)"))
    check("has_perm(未定义节点)=False", det("has_perm(未定义节点)") == "false",
          det("has_perm(未定义节点)"))
    check("check_perm(传role=super)", det("check_perm(传role=super)") == "true",
          det("check_perm(传role=super)"))
    check("user_groups 返回列表", det("user_groups(不传role)").startswith("["),
          det("user_groups(不传role)"))
    check("user_groups(传role=super) 非空",
          det("user_groups(传role=super)") not in ("", "[]"), det("user_groups(传role=super)"))
    check("is_blacklisted(plain)=False", det("is_blacklisted(plain)") == "false",
          det("is_blacklisted(plain)"))
    check("is_group_owner 可用", ok_of("is_group_owner"), det("is_group_owner"))
    check("is_group_admin 可用", ok_of("is_group_admin"), det("is_group_admin"))

    # —— 群级插件开关 ——
    d = det("群开关往返(disable→enable)")
    check("群开关往返 off=False/on=True",
          '"off": false' in d and '"on": true' in d, d)
    check("get_plugin_status_list 可用", ok_of("get_plugin_status_list"),
          det("get_plugin_status_list")[:120])

    # —— 日志 / 审计 / 数据目录 ——
    for n in ["log(info)", "log(warning)", "log(error)"]:
        check(n, ok_of(n), det(n))
    check("get_data_dir 非空", len(det("get_data_dir")) > 0, det("get_data_dir"))
    check("数据目录真实可写可读回", det("数据目录可写") == "probe", det("数据目录可写"))
    check("audit_log 可用", ok_of("audit_log"), det("audit_log"))
    rows = db(ad, "SELECT action FROM audit_logs WHERE action LIKE '%surface_test%'")
    check("audit_log 真写入 audit_logs 表", bool(rows), str(rows))

    # —— 广播 ——
    check("emit 可用", ok_of("emit"), det("emit"))
    time.sleep(0.5)
    check("emit 广播被 on 处理器收到",
          any("surface.ping" in l for l in klog_get()))

    # —— 文本 / 发送 ——
    check("get_text 提取纯文本含'你好'", "你好" in det("get_text(dict)"), det("get_text(dict)"))
    for n in ["send_msg", "send_private_msg", "send_group_msg"]:
        check(n, ok_of(n), det(n))
    check("send_msg 真的到达适配器",
          any(p.get("message") == "surface sync" for p in ad.inbox), str(len(ad.inbox)))

    # —— typed 快捷动作（假适配器回值）——
    for n in ["ban", "kick", "mute_all", "set_card", "get_member_list",
              "get_member_info", "delete_msg", "get_login_info",
              "get_stranger_info", "get_group_info", "api(原始动作)"]:
        check("快捷动作 " + n, ok_of(n), det(n)[:120])
    check("get_login_info 拿到假适配器回值",
          "fakebot" in det("get_login_info"), det("get_login_info")[:150])

    # —— 异步执行器 ——
    check("run_async 返回计算结果 42", det("run_async") == "42", det("run_async"))

    # ═════════ 2. ActionProxy 24 动作 ═════════
    ad.drain()
    ad.inject("/surface-actions")
    m = ad.wait_msg(lambda p: str(p.get("message")) == "actions done", timeout=15)
    check("/surface-actions 执行完成", m is not None)
    R2 = load(ad)
    ACTIONS = ["actions.call(原始)", "actions.send_msg", "actions.set_group_ban",
               "actions.set_group_kick", "actions.set_group_whole_ban",
               "actions.set_group_card", "actions.get_group_member_list",
               "actions.get_group_member_info", "actions.get_stranger_info",
               "actions.get_group_info", "actions.get_group_list",
               "actions.get_friend_list", "actions.get_login_info",
               "actions.get_status", "actions.get_version_info",
               "actions.delete_msg", "actions.get_msg",
               "actions.set_friend_add_request", "actions.set_group_add_request",
               "actions.get_image", "actions.can_send_image", "actions.get_record",
               "onebot(=actions 别名)"]
    for n in ACTIONS:
        check(n, R2.get(n, (False, ""))[0], R2.get(n, ("", ""))[1][:120])
    check("ActionProxy 共 23 项全部登记", len([n for n in ACTIONS if n in R2]) == 23,
          str(len([n for n in ACTIONS if n in R2])))
    check("actions 回值带 echo_action",
          "echo_action" in (R2.get("actions.get_login_info", ("", ""))[1] or ""),
          R2.get("actions.get_login_info", ("", ""))[1][:150])

    # ═════════ 3. async 面 ═════════
    ad.drain()
    ad.inject("/surface-async")
    m = ad.wait_msg(lambda p: str(p.get("message")) == "async done", timeout=15)
    check("/surface-async 执行完成", m is not None)
    R3 = load(ad)
    for n in ["aapi", "asend_msg", "aemit", "db_query_async", "db_query_one_async",
              "db_execute_async", "db_execute_many_async", "db_insert_async",
              "create_table_async", "call_async"]:
        check("async " + n, R3.get(n, (False, ""))[0], R3.get(n, ("", ""))[1][:120])
    check("call_async 拿到协程返回值",
          (R3.get("call_async", ("", ""))[1] or "") == "coro-ok",
          R3.get("call_async", ("", ""))[1])
    check("db_insert_async 返回 id>0",
          (R3.get("db_insert_async", ("", ""))[1] or "").isdigit(),
          R3.get("db_insert_async", ("", ""))[1])

    # ═════════ 4. Event 属性面（富媒体群消息）═════════
    ad.drain()
    # message 用纯文本（保证命令能匹配），segments 单独带富媒体
    # —— Event 构造会优先取 raw['segments'] 作为段来源（sdk event.py），
    # 把富媒体塞进 message 会让 _extract_text 拼出 "[回复][@12345]..." 导致命令匹配不上。
    segs = [
        {"type": "reply", "data": {"id": 111}},
        {"type": "at", "data": {"qq": "12345"}},
        {"type": "text", "data": {"text": "看看这个"}},
        {"type": "image", "data": {"file": "http://x/a.jpg"}},
    ]
    ad.inject_rich("/surface-event", segs, user_id=ADMIN, group_id=GROUP)
    m = ad.wait_msg(lambda p: str(p.get("message")) == "event done", timeout=15)
    check("/surface-event 执行完成", m is not None)
    R4 = load(ad)

    def ev(name):
        return R4.get("event." + name, (False, ""))[1] or ""

    def evok(name):
        return R4.get("event." + name, (False, ""))[0]

    check("event.is_group=True(群消息)", ev("is_group") == "true", ev("is_group"))
    check("event.is_private=False", ev("is_private") == "false", ev("is_private"))
    check("event.group_id=666", ev("group_id") == "666", ev("group_id"))
    check("event.message_type=group", ev("message_type") == "group", ev("message_type"))
    check("event.has_image=True", ev("has_image") == "true", ev("has_image"))
    check("event.has_at=True", ev("has_at") == "true", ev("has_at"))
    check("event.has_reply=True", ev("has_reply") == "true", ev("has_reply"))
    check("event.at_list 含 12345", "12345" in ev("at_list"), ev("at_list"))
    check("event.at_all=False", ev("at_all") == "false", ev("at_all"))
    check("event.reply_id=111", "111" in ev("reply_id"), ev("reply_id"))
    check("event.images 非空", ev("images") not in ("", "[]"), ev("images")[:120])
    check("event.first_image 是 dict(非 None)",
          evok("first_image") and ev("first_image").startswith("{"),
          ev("first_image")[:120])
    check("event.first_voice 空时返回 {} ", ev("first_voice") == "{}", ev("first_voice"))
    check("event.first_video 空时返回 {}", ev("first_video") == "{}", ev("first_video"))
    check("event.first_file 空时返回 {}", ev("first_file") == "{}", ev("first_file"))
    check("event.text 含命令文本", "/surface-event" in ev("text"), ev("text")[:120])
    check("event.is_media_only=False", ev("is_media_only") == "false", ev("is_media_only"))
    # 注意 ev()/evok() 内部已自动加 "event." 前缀，这里只传裸名（勿写成 "event.xxx"）
    check("event.sender_nickname=探长", ev("sender_nickname") == "探长", ev("sender_nickname"))
    check("event.sender_card=名片", ev("sender_card") == "名片", ev("sender_card"))
    check("event.role 非空", len(ev("role")) > 0, ev("role"))
    check("event.is_superuser=True(admin)", ev("is_superuser") == "true", ev("is_superuser"))
    check("event.get(user_id) 可取", ev("get(user_id)") == str(ADMIN), ev("get(user_id)"))
    check("event.get(不存在) 返回默认", ev("get(不存在,默认)") == "DV", ev("get(不存在,默认)"))
    check("event['user_id'] 可取", ev("__getitem__") == str(ADMIN), ev("__getitem__"))
    check("event.__contains__", ev("__contains__") == "true", ev("__contains__"))
    check("event.keys 含 user_id", "user_id" in ev("keys"), ev("keys")[:150])
    # Event.has_perm 对齐原框架：resolve(db, user_id, context_from_event(ev), ev.role)
    # —— 会注入事件自身 role，故超管的 zcbot.role.super 应为 True。
    check("event.has_perm(zcbot.role.super)=True（注入自身 role）",
          ev("has_perm(zcbot.role.super)") == "true", ev("has_perm(zcbot.role.super)"))
    check("event.check_perm 可用", ev("check_perm(zcbot.role.super)") == "true",
          ev("check_perm(zcbot.role.super)"))
    check("event.stop_event → is_stopped=True", ev("is_stopped") == "true", ev("is_stopped"))
    check("event.continue_route → is_continue_route=True",
          ev("is_continue_route") == "true", ev("is_continue_route"))

    # ═════════ 5. 多轮会话 ═════════
    ad.drain()
    ad.inject("/surface-waitfor")
    m = ad.wait_msg(lambda p: "请回答你的名字" in str(p.get("message")), timeout=8)
    check("wait_for 发出提示语", m is not None)
    ad.inject("小明")
    m = ad.wait_msg(lambda p: str(p.get("message")) == "waitfor done", timeout=12)
    check("wait_for 拿到用户回话", m is not None)
    R5 = load(ad)
    check("wait_for 记录了回话内容",
          R5.get("wait_for", (False, ""))[0] and "小明" in (R5.get("wait_for", ("", ""))[1] or ""),
          R5.get("wait_for", ("", ""))[1][:150])

    ad.drain()
    ad.inject("/surface-session")
    m = ad.wait_msg(lambda p: str(p.get("message")) == "session done", timeout=12)
    check("/surface-session 执行完成", m is not None)
    R6 = load(ad)
    check("create_session 返回会话", R6.get("create_session", (False, ""))[0],
          R6.get("create_session", ("", ""))[1][:120])

    # ═════════ 6. 收尾：插件全程未异常退出 ═════════
    bad = [l for l in klog_get() if "插件进程退出" in l and "code=0" not in l]
    check("全程无插件异常退出", not bad, str(bad[:3]))


if __name__ == "__main__":
    sys.exit(main())
