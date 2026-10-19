# -*- coding: utf-8 -*-
"""IIPC 加固验收脚本（tools/iipc_hardening_test.py）

针对已修复的四类缺陷做确定性验收（与 e2e_check.py 互补）：

  1) DB 损坏兜底：文件 DB 损坏 → 内核隔离坏文件 + 明确"持久化已失效"告警，且仍能启动（回退 :memory:）。
  2) config.get "null"：插件配置写入字面量 "null" 视作未设置，回退默认值（杜绝 int(None) 类崩溃）。
  3) 内存监控盲区：debug.proc_stats 返回内核 PID + 各插件 PID，launcher 据此聚合真正的"每插件一解释器"内存。
  4) 崩溃炸弹 / 优雅关闭：启动即崩的插件按上限停止自动重启（不无限拉起吞内存）；SIGTERM 后无孤儿插件进程。

用法：
    python tools/iipc_hardening_test.py [--kernel path] [--plugins-dir path]
退出码 0 = 全部通过。
"""
import argparse
import os
import shutil
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

ADDR = pick_addr("ZCBOT_HARDEN_ADDR")
AUTH = "dev-auth"

results = []  # (name, ok, detail)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, ("-- " + detail) if detail else ""))
    return ok


def _wait_listen(kproc, log_ref, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any("IIPC 服务端监听" in l for l in log_ref):
            return True
        if kproc.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def _pump(kproc, log_ref):
    def fn():
        try:
            for line in kproc.stdout:
                log_ref.append(line)
        except Exception:
            pass
    threading.Thread(target=fn, daemon=True).start()


# ─────────────────────────── 1) DB 损坏兜底 ───────────────────────────
def test_db_corruption(kernel, plugins_dir):
    print("\n--- 用例1: DB 损坏兜底 ---")
    tmp = os.path.join(IIPC, "_harden_db")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    dbfile = os.path.join(tmp, "corrupt.db")
    with open(dbfile, "wb") as f:
        f.write(b"this is not a valid sqlite file" * 100)

    log_ref = []
    kenv = dict(os.environ)
    kenv["PYTHONIOENCODING"] = "utf-8"
    kenv["PYTHONUTF8"] = "1"
    kenv["ZCBOT_PYTHON"] = sys.executable
    kproc = subprocess.Popen(
        [kernel, "--plugins-dir", plugins_dir, "--ipc-addr", ADDR,
         "--db", dbfile, "--data-dir", os.path.join(tmp, "dat"),
         "--log-level", "debug"],
        cwd=os.path.dirname(kernel), env=kenv,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    _pump(kproc, log_ref)
    try:
        ok = _wait_listen(kproc, log_ref)
        check("损坏DB仍能启动(回退:memory:)", ok)
        # 坏文件应被隔离为 .corrupt-<时间戳>
        isolated = [f for f in os.listdir(tmp) if f.startswith("corrupt.db.corrupt-")]
        check("坏文件已隔离(.corrupt-ts)", len(isolated) >= 1, str(isolated))
        # 日志应包含明确的数据丢失告警（而非含糊的"回退真内存引擎"）
        logblob = "".join(log_ref)
        check("日志明确声明持久化已失效", "持久化已失效" in logblob,
              ("含'持久化已失效'" if "持久化已失效" in logblob else "未找到关键告警"))
        # 原坏文件路径不应残留（已被改名隔离）
        check("原坏文件路径已不存在", not os.path.exists(dbfile))
    finally:
        kproc.terminate()
        try:
            kproc.wait(5)
        except Exception:
            kproc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── 2) config.get "null" ───────────────────────────
def test_config_null(kernel, plugins_dir):
    print("\n--- 用例2: config.get 字面量 null 回退默认 ---")
    tmp = os.path.join(IIPC, "_harden_cfg")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    log_ref = []
    kenv = dict(os.environ)
    kenv["PYTHONIOENCODING"] = "utf-8"
    kenv["PYTHONUTF8"] = "1"
    kenv["ZCBOT_PYTHON"] = sys.executable
    kproc = subprocess.Popen(
        [kernel, "--plugins-dir", plugins_dir, "--ipc-addr", ADDR,
         "--db", ":memory:", "--data-dir", os.path.join(tmp, "dat"),
         "--log-level", "debug"],
        cwd=os.path.dirname(kernel), env=kenv,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    _pump(kproc, log_ref)
    try:
        ok = _wait_listen(kproc, log_ref)
        check("内核监听", ok)
        if not ok:
            return
        c = IIPCClient(ADDR, AUTH)
        # 注册为插件（设置 c.pluginID），从而走插件级 plugin_configs 路径
        c.call("plugin.register", {"plugin_id": "nullt", "event_types": [],
                                   "commands": [], "tasks": []})
        # 写入字面量 "null"
        c.call("db.exec", {"sql": "INSERT INTO plugin_configs(plugin_name,config_key,config_value,"
                                  "created_at,updated_at) VALUES(%s,%s,%s,datetime('now'),datetime('now'))",
                           "args": ["nullt", "demo", "null"]})
        res = c.call("config.get", {"key": "demo", "default": "FALLBACK"})
        val = res.get("value") if isinstance(res, dict) else None
        check("插件配置 'null' 回退默认(非None)", val == "FALLBACK", "value=%r" % val)

        # 全局 config 路径同样处理
        c.call("db.exec", {"sql": "INSERT INTO config(k,v) VALUES(%s,%s)", "args": ["g_null", "null"]})
        res = c.call("config.get", {"key": "g_null", "default": "GFB"})
        val = res.get("value") if isinstance(res, dict) else None
        check("全局配置 'null' 回退默认(非None)", val == "GFB", "value=%r" % val)

        # 正常值不受影响
        c.call("db.exec", {"sql": "INSERT INTO plugin_configs(plugin_name,config_key,config_value,"
                                  "created_at,updated_at) VALUES(%s,%s,%s,datetime('now'),datetime('now'))",
                           "args": ["nullt", "real", "REALVAL"]})
        res = c.call("config.get", {"key": "real", "default": "FALLBACK"})
        val = res.get("value") if isinstance(res, dict) else None
        check("正常值不误伤", val == "REALVAL", "value=%r" % val)
    finally:
        kproc.terminate()
        try:
            kproc.wait(5)
        except Exception:
            kproc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── 3) 内存监控盲区：proc_stats ───────────────────────────
def test_proc_stats(kernel, plugins_dir):
    print("\n--- 用例3: debug.proc_stats 暴露插件 PID ---")
    tmp = os.path.join(IIPC, "_harden_ps")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    log_ref = []
    kenv = dict(os.environ)
    kenv["PYTHONIOENCODING"] = "utf-8"
    kenv["PYTHONUTF8"] = "1"
    kenv["ZCBOT_PYTHON"] = sys.executable
    kproc = subprocess.Popen(
        [kernel, "--plugins-dir", plugins_dir, "--ipc-addr", ADDR,
         "--db", ":memory:", "--data-dir", os.path.join(tmp, "dat"),
         "--admin", "10001", "--log-level", "debug"],
        cwd=os.path.dirname(kernel), env=kenv,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    _pump(kproc, log_ref)
    try:
        ok = _wait_listen(kproc, log_ref)
        check("内核监听", ok)
        if not ok:
            return
        # 等插件注册
        c = IIPCClient(ADDR, AUTH)
        c.call("adapter.register", {"self_id": "testbot"})
        reg = False
        for _ in range(150):
            try:
                rows = c.call("db.query", {"sql": "SELECT DISTINCT plugin_name FROM commands"})
                names = {r["plugin_name"] for r in rows.get("rows", [])}
                if "echo" in names:
                    reg = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        check("echo 插件已注册(被内核直拉)", reg)
        # 调 proc_stats
        stats = c.call("debug.proc_stats", {}, timeout=5)
        kpid = stats.get("kernel_pid") if isinstance(stats, dict) else None
        plugins = stats.get("plugins", []) if isinstance(stats, dict) else []
        check("返回 kernel_pid", isinstance(kpid, int) and kpid > 0, "kernel_pid=%r" % kpid)
        # 至少应能看到 echo 插件的 PID（之前 launcher 监控完全看不到它们）
        echo_entry = [p for p in plugins if p.get("plugin_id") == "echo"]
        check("proc_stats 含 echo 插件 PID", len(echo_entry) == 1 and (echo_entry[0].get("pid") or 0) > 0,
              "plugins=%r" % plugins)
        if echo_entry:
            check("echo 插件处于 running", bool(echo_entry[0].get("running")))
    finally:
        kproc.terminate()
        try:
            kproc.wait(5)
        except Exception:
            kproc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── 4) 崩溃炸弹上限 + 优雅关闭 ───────────────────────────
def _make_boom_plugin(dir_path):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "main.py"), "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            "# 启动即崩：模拟有 bug 的插件（如 import 错误 / 顶层异常）\n"
            "raise RuntimeError('boom-on-boot')\n"
        )


def test_crash_bomb_and_shutdown(kernel):
    print("\n--- 用例4: 崩溃炸弹上限 + 优雅关闭无孤儿 ---")
    boom_dir = os.path.join(IIPC, "_harden_boom_plugins")
    if os.path.isdir(boom_dir):
        shutil.rmtree(boom_dir)
    _make_boom_plugin(os.path.join(boom_dir, "boom"))

    tmp = os.path.join(IIPC, "_harden_boom_dat")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    log_ref = []
    kenv = dict(os.environ)
    kenv["PYTHONIOENCODING"] = "utf-8"
    kenv["PYTHONUTF8"] = "1"
    kenv["ZCBOT_PYTHON"] = sys.executable
    kproc = subprocess.Popen(
        [kernel, "--plugins-dir", boom_dir, "--ipc-addr", ADDR,
         "--db", ":memory:", "--data-dir", tmp, "--log-level", "debug"],
        cwd=os.path.dirname(kernel), env=kenv,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    _pump(kproc, log_ref)
    try:
        ok = _wait_listen(kproc, log_ref)
        check("内核监听", ok)
        if not ok:
            return
        # 等待看门狗反复重启 → 最终应打出"停止自动重启"（证明上限生效，非无限炸弹）
        capped = False
        deadline = time.time() + 90
        while time.time() < deadline:
            if any("停止自动重启" in l for l in log_ref):
                capped = True
                break
            time.sleep(0.5)
        check("崩溃炸弹被上限拦停(打印停止自动重启)", capped)
        # 此刻系统内不应存在 boom 插件的 python 进程（已停止拉起）
        orphans_now = _count_plugin_procs(boom_dir)
        check("停止后无 boom 插件进程残留", orphans_now == 0, "残留=%d" % orphans_now)

        # 优雅关闭：SIGTERM 后内核应退出，且不应留下孤儿 python 进程
        kproc.terminate()
        try:
            kproc.wait(8)
        except Exception:
            kproc.kill()
        time.sleep(1)
        orphans_after = _count_plugin_procs(boom_dir)
        check("SIGTERM 后无孤儿插件进程", orphans_after == 0, "孤儿=%d" % orphans_after)
    finally:
        if kproc.poll() is None:
            kproc.kill()
        shutil.rmtree(boom_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _count_plugin_procs(plugin_dir):
    """统计仍以 plugin-dir 指向 boom 插件在跑的 python 进程数。

    进程枚举走 powershell/CIM，机器负载高时可能超时。这里重试若干次再判定，
    仍失败才返回 -1——调用方会把 -1 判为失败，避免"没查到"被误当成"没有残留"。
    """
    needle = os.path.join(plugin_dir, "boom").replace("\\", "/")
    last_err = None
    for _ in range(3):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=30)
            n = 0
            for line in (out.stdout or "").splitlines():
                if needle in line.replace("\\", "/"):
                    n += 1
            return n
        except Exception as e:
            last_err = e
            time.sleep(1)
    print("  (进程枚举失败: %s)" % last_err)
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.path.join(IIPC, "go_kernel", "zcbot_kernel.exe"))
    ap.add_argument("--plugins-dir", default=os.path.join(IIPC, "plugins"))
    a = ap.parse_args()

    if not os.path.exists(a.kernel):
        print("内核不存在: %s（先 go build）" % a.kernel)
        return 2

    test_db_corruption(a.kernel, a.plugins_dir)
    test_config_null(a.kernel, a.plugins_dir)
    test_proc_stats(a.kernel, a.plugins_dir)
    test_crash_bomb_and_shutdown(a.kernel)

    failed = [r for r in results if not r[1]]
    print("\n==== %d 项，通过 %d，失败 %d ====" % (len(results), len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
