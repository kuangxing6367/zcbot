# -*- coding: utf-8 -*-
"""扩展面验收脚本（tools/ext_check.py）

验收内核扩展面是否真的可用：服务注册表、插件间调用、共享 KV、指标、
能力声明、依赖注入。做法是**真起内核 + 真跑两个插件**，让插件自己
把结果写回内核 KV，测试端再读出来断言——不是 mock 出来的绿灯。

覆盖：
  1) service.register / resolve / list
  2) plugin.call（ext_consumer 调用 ext_provider 暴露的动作）
  3) kv.set / get / incr / keys（含跨插件可见性）
  4) metric.observe / snapshot
  5) capability.declare / query
  6) 依赖注入：handler 签名里声明 logger/kv/http 即可拿到
  7) plugin.list

用法：
    python tools/ext_check.py [--kernel path]
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

ADDR = pick_addr("ZCBOT_EXT_ADDR")
AUTH = "dev-auth"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, ("-- " + detail) if detail else ""))
    return ok


PROVIDER = '''# -*- coding: utf-8 -*-
__plugin_meta__ = {"name": "ext_provider", "version": "1.0.0"}


def register(ctx):
    ctx.provide("translate", version="1.0", caps=["text.translate"],
                meta={"lang": "zh"})
    ctx.declare_caps(["text.translate"])
    ctx.metric("provider.boot", 1)

    @ctx.expose("translate")
    def _t(params):
        return {"text": "[译]" + str(params.get("text", ""))}

    ctx.logger.info("provider ready")
'''

CONSUMER = '''# -*- coding: utf-8 -*-
import json
import threading
import time

__plugin_meta__ = {"name": "ext_consumer", "version": "1.0.0"}


def register(ctx):
    @ctx.command("/di")
    def di(event, logger, kv, http):
        """依赖注入自检：logger / kv / http 三个都应被装配。"""
        got = []
        for nm, obj, attr in (("logger", logger, "info"), ("kv", kv, "set"),
                              ("http", http, "get")):
            got.append(nm if obj is not None and hasattr(obj, attr) else nm + "=MISS")
        try:
            kv.set("check.di", "|".join(got))
        except Exception:
            pass
        logger.info("di done: %s" % got)

    def _selfcheck():
        time.sleep(3)  # 等 provider 注册完
        try:
            r = ctx.call_plugin("ext_provider", "translate", {"text": "hi"})
            ctx.kv.set("check.call", json.dumps(r, ensure_ascii=False))
        except Exception as e:
            ctx.kv.set("check.call", "ERR:%s" % e)
        try:
            s = ctx.resolve_service("translate")
            ctx.kv.set("check.resolve", json.dumps(s, ensure_ascii=False))
        except Exception as e:
            ctx.kv.set("check.resolve", "ERR:%s" % e)
        try:
            ctx.kv.incr("check.counter", 5)
        except Exception:
            pass

    threading.Thread(target=_selfcheck, daemon=True).start()
'''


def build_plugins(base):
    if os.path.isdir(base):
        shutil.rmtree(base)
    for pid, src in (("ext_provider", PROVIDER), ("ext_consumer", CONSUMER)):
        d = os.path.join(base, pid)
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as f:
            f.write(src)
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.path.join(IIPC, "go_kernel", "zcbot_kernel.exe"))
    a = ap.parse_args()

    plugins_dir = build_plugins(os.path.join(IIPC, "_extcheck_plugins"))
    sdk_dir = os.path.join(IIPC, "py_sdk")

    log_ref = []
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["ZCBOT_PYTHON"] = sys.executable

    host, port = ADDR.split(":")
    kproc = subprocess.Popen(
        [a.kernel, "--ipc-addr", ADDR, "--db", "mem",
         "--plugins-dir", plugins_dir, "--data-dir",
         os.path.join(IIPC, "_extcheck_dat")],
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
        # 等内核监听
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
            if "ext_provider" in ids and "ext_consumer" in ids:
                break
            time.sleep(0.5)
        check("两个插件均已注册", "ext_provider" in ids and "ext_consumer" in ids,
              str(ids))

        # 1) 服务注册表
        try:
            r = cli.call("service.list", {}, timeout=5)
            svcs = (r or {}).get("services", [])
            names = [s.get("name") for s in svcs]
            owner = next((s.get("provider") for s in svcs if s.get("name") == "translate"), None)
        except Exception as e:
            svcs, names, owner = [], [], None
            print("   service.list 异常: %s" % e)
        check("服务注册表：translate 已注册", "translate" in names, str(names))
        check("服务归属正确（ext_provider）", owner == "ext_provider", str(owner))

        # 2) 插件间调用（consumer 自测写入 KV）
        call_res = _wait_kv(cli, "check.call", timeout=25)
        check("插件间调用 plugin.call",
              call_res is not None and "[译]hi" in str(call_res), str(call_res)[:120])

        # 3) 服务解析（按名）
        resolve_res = _wait_kv(cli, "check.resolve", timeout=10)
        ok_resolve = False
        try:
            ok_resolve = bool(json.loads(resolve_res or "{}").get("found"))
        except Exception:
            pass
        check("服务解析 service.resolve", ok_resolve, str(resolve_res)[:120])

        # 4) KV incr
        try:
            r = cli.call("kv.get", {"key": "check.counter"}, timeout=5)
            counter = (r or {}).get("value")
        except Exception:
            counter = None
        check("共享 KV incr 累加", str(counter) in ("5", "5.0"), str(counter))

        # 5) 依赖注入（注入消息触发 /di）
        try:
            cli.call("debug.inject", {
                "type": "message",
                "data": {"message": "/di", "user_id": 10001, "group_id": 1,
                         "self_id": "mainpy", "message_type": "group"},
            }, timeout=5)
        except Exception as e:
            print("   注入失败: %s" % e)
        di_res = _wait_kv(cli, "check.di", timeout=20)
        ok_di = di_res is not None and "MISS" not in str(di_res)
        check("依赖注入 logger/kv/http", ok_di, str(di_res)[:120])

        # 6) 能力查询
        try:
            r = cli.call("capability.query", {"cap": "text.translate"}, timeout=5)
            holders = (r or {}).get("plugins", [])
        except Exception:
            holders = []
        check("能力查询 capability.query", holders == ["ext_provider"], str(holders))

        # 7) 指标
        try:
            r = cli.call("metric.snapshot", {}, timeout=5)
            metrics = (r or {}).get("metrics", {})
        except Exception:
            metrics = {}
        check("指标 metric.snapshot", "provider.boot" in metrics, str(list(metrics)[:5]))

        # 8) 生命周期
        try:
            r = cli.call("lifecycle.state", {}, timeout=5)
            nplug = (r or {}).get("plugins")
        except Exception:
            nplug = None
        check("生命周期 lifecycle.state", isinstance(nplug, int) and nplug >= 2, str(nplug))

        # 9) KV keys 列举
        try:
            r = cli.call("kv.keys", {"prefix": "check."}, timeout=5)
            keys = (r or {}).get("keys", [])
        except Exception:
            keys = []
        check("KV 前缀列举", len(keys) >= 3, str(keys))

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
    print("\n==== 扩展面验收: %d/%d 通过 ====" % (ok, len(results)))
    if bad:
        print("失败项: %s" % ", ".join(bad))
    return 0 if ok == len(results) else 1


def _wait_kv(cli, key, timeout=20):
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


if __name__ == "__main__":
    sys.exit(main())
