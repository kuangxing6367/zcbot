# -*- coding: utf-8 -*-
"""全局变换管线验收（tools/pipeline_check.py）

真起内核 + 真跑两个插件，验证"节点间数据变换"的等价物：
  - tf_plugin  注册一个全局变换节点（ctx.transform）：把消息追加 " [transformed]"
  - echo_plugin 用 on("message") 与 /t 命令记录它实际看到的消息，写回内核 KV

测试端注入一条消息，再读出 echo_plugin 写回的 KV，断言它看到了"变换后"的消息——
证明内核确实在扇出前调用了变换节点，且下游命令匹配仍基于变换后的文本。

覆盖：
  1) pipeline.list 能枚举到 tf_plugin 的变换节点
  2) on("message") 处理器看到变换后的消息
  3) /t 命令基于变换后的消息命中并记录

退出码 0 = 全部通过。
"""
import argparse
import json
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

ADDR = pick_addr("ZCBOT_PIPELINE_ADDR")
AUTH = "dev-auth"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, ("-- " + detail) if detail else ""))
    return ok


TF = '''# -*- coding: utf-8 -*-
__plugin_meta__ = {"name": "tf_plugin", "version": "1.0.0"}


def register(ctx):
    @ctx.transform(priority=10)
    def normalize(params):
        params = dict(params or {})
        msg = params.get("message")
        if isinstance(msg, str):
            params["message"] = msg + " [transformed]"
        return params
    ctx.logger.info("transform node registered")
'''

ECHO = '''# -*- coding: utf-8 -*-
__plugin_meta__ = {"name": "echo_plugin", "version": "1.0.0"}


def register(ctx):
    @ctx.on("message")
    def rec(event):
        ctx.kv.set("echo.seen", event.message)

    @ctx.command("/t")
    def t(event, match):
        ctx.kv.set("echo.cmd", event.message)
'''


def build_plugins(base):
    if os.path.isdir(base):
        shutil.rmtree(base)
    for pid, src in (("tf_plugin", TF), ("echo_plugin", ECHO)):
        d = os.path.join(base, pid)
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as f:
            f.write(src)
    return base


def _wait_kv(cli, key, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = cli.call("kv.get", {"key": key}, timeout=3)
            if r and r.get("found"):
                return r.get("value")
        except Exception:
            pass
        time.sleep(0.4)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.path.join(IIPC, "go_kernel", "zcbot_kernel.exe"))
    a = ap.parse_args()

    plugins_dir = build_plugins(os.path.join(IIPC, "_pipeline_plugins"))
    sdk_dir = os.path.join(IIPC, "py_sdk")

    log_ref = []
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["ZCBOT_PYTHON"] = sys.executable

    kproc = subprocess.Popen(
        [a.kernel, "--ipc-addr", ADDR, "--db", "mem",
         "--plugins-dir", plugins_dir, "--data-dir",
         os.path.join(IIPC, "_pipeline_dat")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        text=True, encoding="utf-8", errors="replace")

    def pump():
        try:
            for line in kproc.stdout:
                log_ref.append(line)
        except Exception:
            pass
    threading.Thread(target=pump, daemon=True).start()

    try:
        ok_listen = False
        for _ in range(200):
            if any("IIPC 服务端监听" in l for l in log_ref):
                ok_listen = True
                break
            if kproc.poll() is not None:
                break
            time.sleep(0.1)
        check("内核启动并监听", ok_listen, ADDR)
        if not ok_listen:
            return 1

        cli = IIPCClient(ADDR, AUTH)

        # 等两个插件都注册完
        ids = []
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                r = cli.call("plugin.list", {}, timeout=3)
                ids = [p["plugin_id"] for p in (r or {}).get("plugins", [])]
            except Exception:
                ids = []
            if "tf_plugin" in ids and "echo_plugin" in ids:
                break
            time.sleep(0.5)
        check("两个插件均已注册", "tf_plugin" in ids and "echo_plugin" in ids, str(ids))

        # 1) pipeline.list 枚举变换节点
        try:
            r = cli.call("pipeline.list", {}, timeout=5)
            xs = (r or {}).get("transforms", [])
            xowners = [x.get("plugin_id") for x in xs]
        except Exception as e:
            xs, xowners = [], []
            print("   pipeline.list 异常: %s" % e)
        check("pipeline.list 枚举到变换节点", "tf_plugin" in xowners, str(xowners))

        # 注入两条消息触发扇出（先清除旧 KV）
        try:
            cli.call("kv.del", {"key": "echo.seen"}, timeout=3)
            cli.call("kv.del", {"key": "echo.cmd"}, timeout=3)
        except Exception:
            pass
        # 2) 命中命令的消息：验证命令基于"变换后"文本命中
        try:
            cli.call("debug.inject", {
                "type": "message",
                "data": {"message": "/t hello", "user_id": 10001, "group_id": 1,
                         "self_id": "mainpy", "message_type": "group"},
            }, timeout=5)
        except Exception as e:
            print("   注入失败: %s" % e)
        # 3) 不命中命令的消息：验证 on("message") 处理器看到"变换后"消息
        #    （命令命中会短路 on 分发，故用一条不匹配任何命令的消息来验证 on 路径）
        try:
            cli.call("debug.inject", {
                "type": "message",
                "data": {"message": "/nope hi", "user_id": 10001, "group_id": 1,
                         "self_id": "mainpy", "message_type": "group"},
            }, timeout=5)
        except Exception as e:
            print("   注入失败: %s" % e)

        # on("message") 看到变换后消息
        seen = _wait_kv(cli, "echo.seen", timeout=20)
        check("全局变换：on(message) 看到变换后消息",
              seen is not None and seen == "/nope hi [transformed]", str(seen))

        # /t 命令基于变换后消息命中
        cmd = _wait_kv(cli, "echo.cmd", timeout=20)
        check("全局变换：命令基于变换后文本命中",
              cmd is not None and cmd == "/t hello [transformed]", str(cmd))

        try:
            cli.close()
        except Exception:
            pass
    finally:
        try:
            kproc.terminate()
            kproc.wait(timeout=10)
        except Exception:
            try:
                kproc.kill()
            except Exception:
                pass

    ok = sum(1 for _, v, _ in results if v)
    bad = [n for n, v, _ in results if not v]
    print("\n==== 全局变换管线验收: %d/%d 通过 ====" % (ok, len(results)))
    if bad:
        print("失败项: %s" % ", ".join(bad))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
