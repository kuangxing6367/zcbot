# -*- coding: utf-8 -*-
"""IIPC 端到端验收脚本（tools/e2e_check.py）

自备"假适配器"连入内核，充当 OneBot 协议端：收 send_msg 类 api.call、回 res。
全程不依赖真实 OneBot 服务器，验证 1:1 API 面：

  内核装配 → 插件注册（commands/tasks 落库）→ 事件注入 → 命令/会话/DB/配置/
  群开关/广播/权限 → 适配器收到回发消息 → 断言。

用法：
    python tools/e2e_check.py [--kernel path] [--plugins-dir path] [--keep]
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

# 端口动态分配：不写死，避免与开发机上正在跑的服务（WebUI/OneBot/适配器）抢端口
ADDR = pick_addr("ZCBOT_E2E_ADDR")
AUTH = "dev-auth"

results = []  # (name, ok, detail)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, ("-- " + detail) if detail else ""))
    return ok


class FakeAdapter:
    """假 OneBot 适配器：记录 send_msg 入站，回应全部 api.call。"""

    def __init__(self):
        self.client = IIPCClient(ADDR, AUTH)
        self.inbox = []          # 收到的 send_msg params
        self.calls = []          # 全部 api.call 记录
        self._lock = threading.Lock()
        self._msg_seq = 1000
        self.client.register("api.call", self._on_api_call)

    def _on_api_call(self, params):
        action = params.get("action", "")
        p = params.get("params") or {}
        with self._lock:
            self.calls.append((action, p))
            if action == "send_msg":
                self.inbox.append(p)
            self._msg_seq += 1
        return {"message_id": self._msg_seq, "data": {"ok": True}}

    def wait_msg(self, predicate, timeout=3.0):
        """等待一条满足 predicate 的 send_msg 入站。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for p in self.inbox:
                    if predicate(p):
                        return p
            time.sleep(0.05)
        return None

    def inject(self, text, user_id=10001, group_id=None, self_id="testbot"):
        data = {"message": text, "user_id": user_id, "self_id": self_id,
                "post_type": "message", "message_type": "group" if group_id else "private",
                "raw_message": text, "font": 0}
        if group_id:
            data["group_id"] = group_id
        self.client.call("debug.inject", {"type": "message", "data": data})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.path.join(IIPC, "go_kernel", "zcbot_kernel.exe"))
    ap.add_argument("--plugins-dir", default=os.path.join(IIPC, "plugins"))
    ap.add_argument("--keep", action="store_true", help="验收后不停内核（调试用）")
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
         "--admin", "10001", "--log-level", "debug"],
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
            for l in klog_ref[-80:]:
                print(l.rstrip())
        else:
            kproc.terminate()
            try:
                kproc.wait(5)
            except Exception:
                kproc.kill()

    failed = [r for r in results if not r[1]]
    print("\n==== %d 项，通过 %d，失败 %d ====" % (len(results), len(results) - len(failed), len(failed)))
    return 1 if failed else 0


def run_checks(a):
    # 等内核监听
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

    # 等两个插件注册（以 commands 表落库为准，确定性等待）
    ok = False
    for _ in range(150):
        try:
            rows = ad.client.call("db.query", {"sql": "SELECT DISTINCT plugin_name FROM commands"})
            names = {r["plugin_name"] for r in rows.get("rows", [])}
            if {"echo", "api_parity"} <= names:
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    check("双插件注册", ok)

    # commands / tasks 落库（1:1 数据面）
    rows = ad.client.call("db.query", {"sql": "SELECT plugin_name, pattern FROM commands ORDER BY plugin_name, pattern"})
    pats = {(r["plugin_name"], r["pattern"]) for r in rows.get("rows", [])}
    check("commands 落库", ("echo", "/ping") in pats and ("api_parity", "/parity") in pats,
          str(sorted(pats)))

    tasks = ad.client.call("db.query", {"sql": "SELECT plugin_name, handler, cron_expression FROM tasks"})
    trows = {(r["plugin_name"], r["handler"], r["cron_expression"]) for r in tasks.get("rows", [])}
    check("tasks 落库(cron)", ("api_parity", "_minutely", "* * * * *") in trows, str(sorted(trows)))

    # ── task.tick → 本地执行全链（对齐框架 scheduler._run_job：handler 零参调用）──
    # 推 task.tick 给 api_parity._minutely：执行后应写 plugin_configs.minutely_tick
    ad.client.call("debug.inject", {"type": "task.tick",
                                    "data": {"handlers": ["_minutely"]}})
    ok = False
    for _ in range(50):
        try:
            rows = ad.client.call("db.query", {
                "sql": "SELECT config_value FROM plugin_configs "
                       "WHERE plugin_name=%s AND config_key=%s",
                "args": ["api_parity", "minutely_tick"]})
            if rows.get("rows"):
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    check("task.tick → _minutely 零参执行(写配置)", ok)

    # ── command.before 扩展点：hook 返回 False → 跳过该命令（框架同语义）──
    ad.inject("/take")
    got = ad.wait_msg(lambda p: p.get("message") == "take ran", timeout=2.0)
    check("command.before 返回 False 跳过 /take", got is None, str(got))

    # ── 基础命令 ──
    ad.inject("/ping")
    check("echo /ping→pong", ad.wait_msg(lambda p: p.get("message") == "pong") is not None)

    ad.inject("hello world")
    check("echo 消息→echo: hello world",
          ad.wait_msg(lambda p: p.get("message") == "echo: hello world") is not None)

    ad.inject("/whoami")
    check("/whoami 身份查询",
          ad.wait_msg(lambda p: "role=" in str(p.get("message"))) is not None)

    # ── 权限命令 ──
    ad.inject("/secret", user_id=10001)   # --admin 10001
    check("/secret 管理员放行",
          ad.wait_msg(lambda p: p.get("message") == "secret ok") is not None)
    ad.inject("/secret", user_id=999)
    check("/secret 非管理员拒绝",
          ad.wait_msg(lambda p: "权限不足" in str(p.get("message"))) is not None)

    # ── DB 全链 + %s 方言翻译 + insert id ──
    ad.inject("/parity")
    m = ad.wait_msg(lambda p: str(p.get("message", "")).startswith("parity ok"), timeout=5)
    check("/parity DB/配置/数据目录/审计", m is not None, m and str(m.get("message")))

    # ── actions 代理（假适配器回 login_info）──
    ad.inject("/acts")
    check("/acts actions.get_login_info",
          ad.wait_msg(lambda p: "login_info=" in str(p.get("message"))) is not None)

    # ── dict 式事件访问 ──
    ad.inject("/data")
    check("/data dict 式访问",
          ad.wait_msg(lambda p: str(p.get("message", "")).startswith("dict: user_id=")) is not None)

    # ── event.emit 广播（parity.ping → api_parity 订阅者收日志）──
    ad.inject("/emit")
    check("/emit emitted", ad.wait_msg(lambda p: p.get("message") == "emitted") is not None)
    time.sleep(0.3)
    check("parity.ping 广播送达",
          any("收到 parity.ping" in l for l in klog_get()))

    # ── on_error 钩子 ──
    ad.inject("/raise")
    time.sleep(0.5)
    check("/raise on_error 钩子",
          any("on_error" in l for l in klog_get()))

    # ── 多轮会话 wait_for ──
    ad.inject("/askss")
    check("wait_for 提示语",
          ad.wait_msg(lambda p: "你叫什么名字" in str(p.get("message"))) is not None)
    ad.inject("小明")
    check("wait_for 回话", ad.wait_msg(lambda p: "你好，小明" in str(p.get("message"))) is not None)

    # ── 群插件开关 ──
    ad.inject("/switch")
    check("/switch 群开关往返",
          ad.wait_msg(lambda p: "echo 开关" in str(p.get("message"))) is not None)

    # 真过滤：群 555 禁用 echo → 该群消息 echo 不应回
    ad.client.call("group_plugin.set", {"group_id": "555", "plugin": "echo", "enabled": False})
    ad.inject("group-silent-test", group_id=555)
    time.sleep(0.8)
    got = any(p.get("message") == "echo: group-silent-test" for p in snapshot_inbox(ad))
    check("群 555 禁用 echo 生效", not got)
    ad.inject("group-loud-test", group_id=666)
    check("群 666 echo 正常",
          ad.wait_msg(lambda p: p.get("message") == "echo: group-loud-test") is not None)
    ad.client.call("group_plugin.set", {"group_id": "555", "plugin": "echo", "enabled": True})

    # ── 权限节点（perm.has_perm 经内核解析）──
    ad.client.call("db.exec", {"sql": "INSERT OR REPLACE INTO users(user_id, role) VALUES(%s,%s)",
                               "args": ["777", "super"]})
    res = ad.client.call("perm.has_perm", {"user_id": "777", "node": "zcbot.role.super", "role": "super"})
    check("perm.has_perm 内置角色节点", res.get("ok") is True, str(res))
    res = ad.client.call("perm.has_perm", {"user_id": "777", "node": "some.undefined.node", "role": "super"})
    check("perm.has_perm 未定义拒绝", res.get("ok") is False, str(res))

    # ── at 机器人触发 ──
    ad.inject("[CQ:at,qq=testbot] 你好", group_id=666)
    check("has_at_bot 回话",
          ad.wait_msg(lambda p: "你好，" in str(p.get("message"))) is not None)


_log_lock = threading.Lock()
klog_ref = []  # 内核日志行（main 的泵线程追加）


def klog_get():
    with _log_lock:
        return list(klog_ref)


def snapshot_inbox(ad):
    with ad._lock:
        return list(ad.inbox)


if __name__ == "__main__":
    sys.exit(main())
