# -*- coding: utf-8 -*-
"""官方插件 1:1 验收脚本（tools/official_plugin_check.py）

把「真·官方插件」原样拖进 IIPC 跑，验证 SDK 对官方文档承诺能力的覆盖：

  A. docs/指南/编写插件.md「完整的签到插件」—— 逐字照抄
     ctx.create_table / ctx.command(alias=) / ctx.task(cron) / ctx.on(event)
     ctx.db_query_one(%s) / ctx.db_execute(%s) / ctx.send_msg
     且 handler 内裸用 ctx（依赖「框架把 ctx 注入插件模块全局」的既有行为）

  B. plugins/llm_blacklist（真仓库官方插件）—— 仅摘除 host 进程专属的
     ctx._framework 依赖，其余逐字保留
     MySQL 风格 DDL 在 SQLite 的兼容 / 正则命令 / require_superuser 权限链路
     / async handler / ctx.get_config / users 表超管兜底

用法：
    python tools/official_plugin_check.py [--kernel path] [--plugins-dir path] [--keep]
退出码 0 = 全部通过。
"""
import argparse
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
ADDR = pick_addr("ZCBOT_OFFICIAL_ADDR")
AUTH = "dev-auth"

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
    """假 OneBot 适配器：记录 send_msg 入站，回应全部 api.call。"""

    def __init__(self):
        self.client = IIPCClient(ADDR, AUTH)
        self.inbox = []
        self._lock = threading.Lock()
        self._seq = 1000
        self.client.register("api.call", self._on_api_call)

    def _on_api_call(self, params):
        action = params.get("action", "")
        p = params.get("params") or {}
        with self._lock:
            if action == "send_msg":
                self.inbox.append(p)
            self._seq += 1
        return {"message_id": self._seq, "data": {"ok": True}}

    def drain(self):
        """取走并清空当前 inbox（避免历史消息污染下一条断言）。"""
        with self._lock:
            out, self.inbox = list(self.inbox), []
        return out

    def wait_msg(self, predicate, timeout=3.0):
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


def db(ad, sql, args=None):
    res = ad.client.call("db.query", {"sql": sql, "args": list(args or [])})
    return res.get("rows", []) if isinstance(res, dict) else []


def wait_plugins(ad, names, timeout=20.0):
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        try:
            rows = db(ad, "SELECT DISTINCT plugin_name FROM commands")
            seen = {r["plugin_name"] for r in rows}
            if set(names) <= seen:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    print("   已注册插件: %s" % sorted(seen))
    return False


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
            for l in klog_ref[-120:]:
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

    # ── 四个插件全部注册（含两个新增官方插件）──
    ok = wait_plugins(ad, ["official_signin", "official_blacklist"], timeout=25.0)
    check("官方插件注册(official_signin/official_blacklist)", ok)

    # ================================================================
    # A. docs/指南/编写插件.md 签到示例（逐字照抄）
    # ================================================================

    # A1. 建表：ctx.create_table + AUTOINCREMENT 方言
    rows = db(ad, "SELECT name FROM sqlite_master WHERE type='table' AND name='sign_in_records'")
    check("A1 create_table 建表 sign_in_records", bool(rows), str(rows))

    # A2. 命令与 alias 落库
    rows = db(ad, "SELECT pattern, alias, description FROM commands WHERE plugin_name='official_signin'")
    pats = {r["pattern"]: r for r in rows}
    check("A2 /签到 与 /我的积分 落库",
          "/签到" in pats and "/我的积分" in pats, str(sorted(pats)))
    check("A2 alias=/sign 落库",
          any(str(r.get("alias") or "") == "/sign" for r in pats.values()),
          str([r.get("alias") for r in pats.values()]))

    # A3. 定时任务落库（cron 0 0 * * *，handler=reset_daily）
    rows = db(ad, "SELECT handler, cron_expression FROM tasks WHERE plugin_name='official_signin'")
    check("A3 task cron 落库(0 0 * * *)",
          any(str(r.get("cron_expression")) == "0 0 * * *" for r in rows), str(rows))

    # A4. 首次签到 → 随机积分（覆盖 db_query_one(%s) / db_execute(%s) / send_msg / 裸 ctx）
    ad.drain()
    ad.inject("/签到", user_id=20001)
    m = ad.wait_msg(lambda p: str(p.get("message", "")).startswith("签到成功！获得"), timeout=5)
    check("A4 首次签到成功(裸 ctx + %s 方言)", m is not None, m and str(m.get("message")))

    # A5. 重复签到 → 当天已签到（证明 db_query_one 真的读回了上一行）
    ad.drain()
    ad.inject("/签到", user_id=20001)
    m = ad.wait_msg(lambda p: "你今天已经签到过了" in str(p.get("message", "")), timeout=5)
    check("A5 重复签到拦截", m is not None, m and str(m.get("message")))

    # A6. 积分查询（COALESCE(SUM(score),0) + row["total"] 字典取值）
    ad.drain()
    ad.inject("/我的积分", user_id=20001)
    m = ad.wait_msg(lambda p: str(p.get("message", "")).startswith("你当前积分："), timeout=5)
    check("A6 /我的积分 聚合查询", m is not None, m and str(m.get("message")))
    if m:
        try:
            total = int(str(m.get("message")).split("：")[1].strip())
            check("A6 积分 > 0（SUM 真实生效）", total > 0, str(total))
        except Exception as e:
            check("A6 积分 > 0（SUM 真实生效）", False, str(e))

    # A7. 事件订阅 notice.group_increase → 写 welcome 100 分
    ad.drain()
    before = len(db(ad, "SELECT id FROM sign_in_records WHERE day='welcome'"))
    ad.client.call("debug.inject", {
        "type": "notice.group_increase",
        "data": {"user_id": 20002, "group_id": 666, "self_id": "testbot"}})
    ok = False
    for _ in range(60):
        rows = db(ad, "SELECT user_id, score FROM sign_in_records WHERE day='welcome' AND user_id=%s",
                  [20002])
        if rows:
            ok = True
            break
        time.sleep(0.1)
    check("A7 on(notice.group_increase) 写库", ok,
          str(db(ad, "SELECT user_id, day, score FROM sign_in_records WHERE day='welcome'")))
    check("A7 welcome 100 分", ok and int(rows[0].get("score", 0)) == 100, str(rows) if ok else "")

    # A8. 私聊签到（is_group=False 分支：group_id=None 不应报错）
    ad.drain()
    ad.inject("/签到", user_id=20003)
    m = ad.wait_msg(lambda p: str(p.get("message", "")).startswith("签到成功！获得"), timeout=5)
    check("A8 私聊签到(is_group=False)", m is not None, m and str(m.get("message")))

    # ================================================================
    # B. 真·官方插件 plugins/llm_blacklist
    # ================================================================

    # B1. MySQL 风格 DDL（VARCHAR(64) PK + TEXT 无 DEFAULT）在 SQLite 侧建表成功
    rows = db(ad, "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_blacklist'")
    check("B1 MySQL 风格 DDL 建表 llm_blacklist", bool(rows), str(rows))

    # B2. 三条正则命令落库
    rows = db(ad, "SELECT pattern FROM commands WHERE plugin_name='official_blacklist'")
    pats = [str(r["pattern"]) for r in rows]
    check("B2 三条正则命令落库", len(pats) == 3, str(pats))

    # B3. 超管拉黑（async handler + require_superuser 放行 + 裸 ctx）
    ad.drain()
    ad.inject("/插件拉黑 12345 刷对话", user_id=10001)
    m = ad.wait_msg(lambda p: "已将 12345 加入 LLM 对话黑名单" in str(p.get("message", "")), timeout=5)
    check("B3 超管 /插件拉黑 生效", m is not None, m and str(m.get("message")))
    check("B3 原因回显", m is not None and "刷对话" in str(m.get("message")), m and str(m.get("message")))

    # B4. 落库校验
    rows = db(ad, "SELECT user_id, reason, operator FROM llm_blacklist WHERE user_id=%s", ["12345"])
    check("B4 黑名单落库", bool(rows), str(rows))
    check("B4 operator 记录操作者", rows and str(rows[0].get("operator")) == "10001",
          str(rows[0].get("operator")) if rows else "")

    # B5. 非超管被内核权限链路拦下（require_superuser，不进 handler）
    ad.drain()
    ad.inject("/插件拉黑 54321", user_id=8888)
    m = ad.wait_msg(lambda p: "权限不足" in str(p.get("message", "")), timeout=5)
    check("B5 非超管 require_superuser 拒绝", m is not None, m and str(m.get("message")))
    rows = db(ad, "SELECT user_id FROM llm_blacklist WHERE user_id=%s", ["54321"])
    check("B5 非超管未写入黑名单", not rows, str(rows))

    # B6. 黑名单列表（ctx.db_query + ctx.get_config 默认值）
    ad.drain()
    ad.inject("/插件黑名单", user_id=10001)
    m = ad.wait_msg(lambda p: "LLM 对话黑名单（共" in str(p.get("message", "")), timeout=5)
    check("B6 /插件黑名单 列表", m is not None, m and str(m.get("message")))
    check("B6 列表含 12345", m is not None and "12345" in str(m.get("message")),
          m and str(m.get("message")))

    # B7. 取消拉黑 + 删除确认
    ad.drain()
    ad.inject("/插件取消拉黑 12345", user_id=10001)
    m = ad.wait_msg(lambda p: "已解除 12345 的拉黑" in str(p.get("message", "")), timeout=5)
    check("B7 /插件取消拉黑 生效", m is not None, m and str(m.get("message")))
    rows = db(ad, "SELECT user_id FROM llm_blacklist WHERE user_id=%s", ["12345"])
    check("B7 数据库已删除", not rows, str(rows))

    # B8. 空列表提示
    ad.drain()
    ad.inject("/插件黑名单", user_id=10001)
    m = ad.wait_msg(lambda p: "黑名单为空" in str(p.get("message", "")), timeout=5)
    check("B8 空黑名单提示", m is not None, m and str(m.get("message")))

    # B9. 重复拉黑同一人 → 走 UPDATE 分支（插件内 exists 判定，非 INSERT）
    ad.drain()
    ad.inject("/插件拉黑 12345 第一次", user_id=10001)
    m = ad.wait_msg(lambda p: "已将 12345 加入" in str(p.get("message", "")), timeout=5)
    check("B9 首次拉黑(INSERT 分支)", m is not None, m and str(m.get("message")))
    ad.drain()
    ad.inject("/插件拉黑 12345 第二次", user_id=10001)
    m = ad.wait_msg(lambda p: "已将 12345 加入" in str(p.get("message", "")), timeout=5)
    check("B9 重复拉黑(UPDATE 分支)", m is not None, m and str(m.get("message")))
    rows = db(ad, "SELECT user_id, reason FROM llm_blacklist WHERE user_id=%s", ["12345"])
    check("B9 仍只有一行(未重复插入)", len(rows) == 1, str(rows))
    check("B9 原因已更新为第二次",
          rows and str(rows[0].get("reason")) == "第二次", str(rows[0].get("reason")) if rows else "")

    # B10. 非法参数不命中正则 → 无回执且不崩溃（插件存活，后续命令仍响应）
    ad.drain()
    ad.inject("/插件拉黑 abc", user_id=10001)
    time.sleep(1.0)
    ad.drain()
    ad.inject("/插件黑名单", user_id=10001)
    m = ad.wait_msg(lambda p: "LLM 对话黑名单（共" in str(p.get("message", "")), timeout=5)
    check("B10 非法参数不崩溃且插件存活", m is not None, m and str(m.get("message")))

    # ── 全程无插件进程崩溃/重启 ──
    bad = [l for l in klog_get() if "插件进程退出" in l and "code=0" not in l]
    check("无插件异常退出", not bad, str(bad[:3]))


if __name__ == "__main__":
    sys.exit(main())
